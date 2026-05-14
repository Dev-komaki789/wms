from django.contrib import admin
from django.urls import include, path

from masters.views import HomeView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('masters/', include('masters.urls')),
    path('stock/', include('stock.urls')),
    path('inbound/', include('inbound.urls')),
    # メニュー画面（KPI サマリー + 機能カテゴリ別カードグリッド）。全画面のハブ
    path('', HomeView.as_view(), name='home'),
]
