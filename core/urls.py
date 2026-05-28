from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('errors/', views.ErrorLogInquiryView.as_view(), name='error_log_inquiry'),
    path('errors/<int:pk>/', views.ErrorLogDetailView.as_view(), name='error_log_detail'),
    # デモ用：棚番／SKU／ピッキングNo／出荷No を Code128 でカード型 A4 印刷
    path('barcodes/', views.BarcodePrintView.as_view(), name='barcode_print'),
]
