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
    # 棚卸
    path('stocktakes/', views.StocktakeInquiryView.as_view(), name='stocktake_inquiry'),
    path('stocktakes/new/', views.StocktakeCreateView.as_view(), name='stocktake_create'),
    path('stocktakes/<int:pk>/', views.StocktakeDetailView.as_view(), name='stocktake_detail'),
    path('stocktakes/<int:pk>/start/', views.StocktakeStartView.as_view(), name='stocktake_start'),
    path('stocktakes/<int:pk>/review/', views.StocktakeReviewView.as_view(), name='stocktake_review'),
    path('stocktakes/<int:pk>/confirm/', views.StocktakeConfirmView.as_view(), name='stocktake_confirm'),
    path('stocktakes/<int:pk>/cancel/', views.StocktakeCancelView.as_view(), name='stocktake_cancel'),
    path('handheld/stocktake/', views.StocktakeCountView.as_view(), name='handheld_stocktake'),
    path('handheld/stocktake/<int:pk>/', views.StocktakeCountWorkView.as_view(), name='handheld_stocktake_work'),
    # AJAX API
    path('api/stock-check/', views.StockCheckAPIView.as_view(), name='api_stock_check'),
]
