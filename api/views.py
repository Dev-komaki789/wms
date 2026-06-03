"""EC サイト向け API のビュー。

ReadOnlyModelViewSet を使うと、自動で以下の 2 つのエンドポイントが生える:
- GET /api/skus/         一覧（list）
- GET /api/skus/{id}/    詳細（retrieve）

EC backend のマスタ同期で使う想定。書き込みは WMS の業務画面から行うので読み取り専用。
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from masters.models import Category, Product, Sku, Warehouse
from outbound.models import OutboundOrder, OutboundOrderItem
from outbound.views import _try_launch_order
from stock.models import StockBalance

from .serializers import (
    CategorySerializer,
    OutboundOrderCreateSerializer,
    ProductSerializer,
    SkuSerializer,
)


class SkuViewSet(viewsets.ReadOnlyModelViewSet):
    """SKU の一覧と詳細を返す API。

    EC backend のマスタ同期で日次バッチから叩く想定。
    将来 `?updated_since=YYYY-MM-DD` で差分取得をサポート予定。
    """

    # select_related で Product を JOIN（N+1 クエリ防止）
    queryset = Sku.objects.select_related('product').filter(is_active=True).order_by('sku_code')
    serializer_class = SkuSerializer


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """Product の一覧と詳細を返す API。

    Category と Manufacturer を select_related で同時取得（N+1 クエリ防止）。
    EC backend のマスタ同期で日次バッチから叩く想定。
    """

    queryset = (
        Product.objects.select_related('category', 'manufacturer')
        .filter(is_active=True)
        .order_by('product_code')
    )
    serializer_class = ProductSerializer


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Category の一覧と詳細を返す API。

    階層構造（最大 4 階層）は parent_id で表現。EC 側で再構築する。
    sort_order, category_code 順で並べて返す。
    """

    queryset = Category.objects.filter(is_active=True).order_by('sort_order', 'category_code')
    serializer_class = CategorySerializer


class StockBySkuView(APIView):
    """指定 SKU の現在在庫数（全ロケーション合計）を返す。

    GET /api/stock/{sku_code}/

    マスタ同期 API（list/retrieve）と違い、リアルタイム性が必要なため EC backend が
    表示の都度叩く想定。将来は Redis キャッシュ層を挟む可能性あり（現状は段階1: 都度 DB SELECT）。

    Router を使わず APIView を直接書いている理由:
    - URL パターンが {sku_code}（文字列）で {id}（整数）ではない
    - 集計（SUM）のみで、list/retrieve のような標準パターンに乗らない
    """

    def get(self, request, sku_code):
        sku = get_object_or_404(Sku, sku_code=sku_code, is_active=True)
        total = StockBalance.objects.filter(sku=sku).aggregate(total=Sum('quantity'))['total'] or 0
        return Response({
            'sku_code': sku.sku_code,
            'stock': total,
            'as_of': timezone.now().isoformat(),
        })


class OrderCreateView(APIView):
    """EC からの注文を WMS の出荷指示として登録し、在庫引き当てまで自動実行する。

    POST /api/orders/

    フロー:
      1. リクエスト検証（external_order_id、配送先、items）
      2. SKU 存在チェック（不在なら 404）
      3. OutboundOrder 作成（source_type='oms'、outbound_order_code は OMS-YYYYMMDD-NNN）
      4. OutboundOrderItem 作成
      5. _try_launch_order を呼んで在庫引き当て + ピッキングリスト生成
      6. 在庫不足なら全体ロールバックして 409 を返す
      7. 成功なら 201 で出荷指示番号を返す

    全体を @transaction.atomic で囲み、_try_launch_order が ok=False の時に
    transaction.set_rollback(True) でロールバックを明示する。
    """

    @extend_schema(
        request=OutboundOrderCreateSerializer,
        summary='EC からの注文を出荷指示として登録',
        description='EC backend からの POST。OutboundOrder を作成し、'
                    '在庫引き当てとピッキングリスト生成まで自動実行する。',
    )
    @transaction.atomic
    def post(self, request):
        # 1. バリデーション
        serializer = OutboundOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 2. SKU 存在チェック
        sku_codes = [item['sku_code'] for item in data['items']]
        skus = {
            s.sku_code: s
            for s in Sku.objects.filter(sku_code__in=sku_codes, is_active=True)
        }
        missing = [c for c in sku_codes if c not in skus]
        if missing:
            return Response(
                {
                    'error': 'not_found',
                    'message': f'存在しない SKU があります: {", ".join(missing)}',
                    'details': {'missing_sku_codes': missing},
                },
                status=drf_status.HTTP_404_NOT_FOUND,
            )

        # 3. created_by: 認証クラス (APIKeyAuthentication) で紐づけた API ユーザーを使う
        # 認証は DRF の DEFAULT_AUTHENTICATION_CLASSES + IsAuthenticated で 401 が
        # ここに到達する前に弾かれているため、request.user は確実に存在する。
        # TODO: 認証クラスを修正し、専用システムユーザー (ec_system) を紐づける
        api_user = request.user

        # 4. 倉庫: 暫定で最初の有効倉庫
        # TODO: 配送先住所から最寄り倉庫を選定するロジックを検討
        warehouse = Warehouse.objects.filter(is_active=True).order_by('id').first()
        if warehouse is None:
            return Response(
                {'error': 'internal_error', 'message': '利用可能な倉庫がありません'},
                status=drf_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # 5. OutboundOrder 作成（source_type='oms', customer=None で個人情報を持たない）
        today = timezone.localdate()
        order = OutboundOrder.objects.create(
            outbound_order_code=OutboundOrder.next_code(today, 'oms'),
            warehouse=warehouse,
            customer=None,
            external_order_id=data['external_order_id'],
            delivery_postal_code=data.get('delivery_postal_code', ''),
            delivery_address=data.get('delivery_address', ''),
            delivery_name=data.get('delivery_name', ''),
            source_type='oms',
            note=data.get('note', ''),
            created_by=api_user,
        )

        # 6. OutboundOrderItem 作成
        for item_data in data['items']:
            sku = skus[item_data['sku_code']]
            OutboundOrderItem.objects.create(
                outbound_order=order,
                sku=sku,
                quantity_ordered=item_data['quantity'],
            )

        # 7. _try_launch_order: 在庫引き当て + ピッキングリスト生成
        result = _try_launch_order(order, api_user)

        if not result['ok']:
            # 在庫不足等: トランザクションをロールバック
            transaction.set_rollback(True)
            return Response(
                {
                    'error': 'stock_shortage',
                    'message': result.get('reason', '在庫引き当てに失敗しました'),
                },
                status=drf_status.HTTP_409_CONFLICT,
            )

        # 8. 成功
        order.refresh_from_db()
        return Response(
            {
                'outbound_order_code': order.outbound_order_code,
                'status': order.status,
                'external_order_id': order.external_order_id,
            },
            status=drf_status.HTTP_201_CREATED,
        )
