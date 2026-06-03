"""EC サイト向け API の URL ルーティング。

DRF の Router を使うと、ViewSet から URL を自動生成できる。
register('skus', SkuViewSet) と書くと以下 2 つが自動で作られる:
- /skus/         → SkuViewSet.list()
- /skus/{id}/    → SkuViewSet.retrieve()

これらは config/urls.py の `path('api/', include('api.urls'))` 経由で
/api/skus/ と /api/skus/{id}/ として公開される。
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import CategoryViewSet, OrderCreateView, ProductViewSet, SkuViewSet, StockBySkuView

router = DefaultRouter()
router.register(r'skus', SkuViewSet, basename='sku')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'categories', CategoryViewSet, basename='category')

urlpatterns = [
    path('', include(router.urls)),
    # Router の標準パターン（list/retrieve）に乗らない集計エンドポイントは直接定義
    path('stock/<str:sku_code>/', StockBySkuView.as_view(), name='stock-by-sku'),
    # EC からの注文を出荷指示として登録 + 在庫引き当てまで自動実行する POST エンドポイント
    path('orders/', OrderCreateView.as_view(), name='order-create'),
]
