"""EC サイト向け API のビュー。

ReadOnlyModelViewSet を使うと、自動で以下の 2 つのエンドポイントが生える:
- GET /api/skus/         一覧（list）
- GET /api/skus/{id}/    詳細（retrieve）

EC backend のマスタ同期で使う想定。書き込みは WMS の業務画面から行うので読み取り専用。
"""

from rest_framework import viewsets

from masters.models import Category, Product, Sku

from .serializers import CategorySerializer, ProductSerializer, SkuSerializer


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
