from django.urls import path

from . import views

app_name = 'stock'

urlpatterns = [
    path('', views.StockInquiryView.as_view(), name='inquiry'),
    # 実行系 (handheld) 画面群
    path('handheld/in/', views.UnplannedStockInView.as_view(), name='handheld_in'),
]
