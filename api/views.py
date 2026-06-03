"""EC サイト向け API のビュー。

ReadOnlyModelViewSet を使うと、自動で以下の 2 つのエンドポイントが生える:
- GET /api/skus/         一覧（list）
- GET /api/skus/{id}/    詳細（retrieve）

EC backend のマスタ同期で使う想定。書き込みは WMS の業務画面から行うので読み取り専用。
"""

from rest_framework import viewsets

from masters.models import Sku

from .serializers import SkuSerializer


class SkuViewSet(viewsets.ReadOnlyModelViewSet):
    """SKU の一覧と詳細を返す API。

    EC backend のマスタ同期で日次バッチから叩く想定。
    将来 `?updated_since=YYYY-MM-DD` で差分取得をサポート予定。
    """

    # select_related で Product を JOIN（N+1 クエリ防止）
    queryset = Sku.objects.select_related('product').filter(is_active=True).order_by('sku_code')
    serializer_class = SkuSerializer
