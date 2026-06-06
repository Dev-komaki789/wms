from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from masters.views import HomeView

# ログイン画面(/admin/login/)や管理サイトの見出しを「倉庫管理システム」に変更する
# （既定の「Django 管理サイト」表記を上書き）
admin.site.site_header = '倉庫管理システム'
admin.site.site_title = '倉庫管理システム'
admin.site.index_title = '倉庫管理システム'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('masters/', include('masters.urls')),
    path('stock/', include('stock.urls')),
    path('inbound/', include('inbound.urls')),
    path('outbound/', include('outbound.urls')),
    path('core/', include('core.urls')),
    # EC サイト連携用 API
    path('api/', include('api.urls')),
    # 上記 API の OpenAPI スキーマと Swagger UI
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path(
        'api/schema/swagger-ui/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui',
    ),
    # メニュー画面（KPI サマリー + 機能カテゴリ別カードグリッド）。全画面のハブ
    path('', HomeView.as_view(), name='home'),
]
