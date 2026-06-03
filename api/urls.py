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

from .views import CategoryViewSet, ProductViewSet, SkuViewSet

router = DefaultRouter()
router.register(r'skus', SkuViewSet, basename='sku')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'categories', CategoryViewSet, basename='category')

urlpatterns = [
    path('', include(router.urls)),
]
