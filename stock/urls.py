from django.urls import path

from . import views

app_name = 'stock'

urlpatterns = [
    path('', views.StockInquiryView.as_view(), name='inquiry'),
    path('movements/', views.StockMovementInquiryView.as_view(), name='movement_inquiry'),
    # 実行系 (handheld) 画面群
    path('handheld/in/', views.UnplannedStockInView.as_view(), name='handheld_in'),
    path('handheld/out/', views.UnplannedStockOutView.as_view(), name='handheld_out'),
    path('handheld/transfer/', views.StockTransferView.as_view(), name='handheld_transfer'),
    # AJAX API
    path('api/stock-check/', views.StockCheckAPIView.as_view(), name='api_stock_check'),
]
