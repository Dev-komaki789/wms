from django.urls import path

from . import views

app_name = 'outbound'

urlpatterns = [
    path('orders/', views.OutboundOrderInquiryView.as_view(), name='order_inquiry'),
    path('launch/', views.OutboundLaunchView.as_view(), name='launch'),
    path('picking-lists/', views.PickingListInquiryView.as_view(), name='picking_list_inquiry'),
    path('picking-lists/<int:pk>/print/', views.PickingListPrintView.as_view(), name='picking_list_print'),
    path('orders/new/', views.OutboundOrderCreateView.as_view(), name='order_create'),
    path('orders/csv/export/', views.OutboundOrderCsvExportView.as_view(), name='order_csv_export'),
    path('orders/csv/import/', views.OutboundOrderCsvImportView.as_view(), name='order_csv_import'),
    path('orders/<int:pk>/', views.OutboundOrderDetailView.as_view(), name='order_detail'),
    path('orders/<int:pk>/edit/', views.OutboundOrderUpdateView.as_view(), name='order_update'),
    path('orders/<int:pk>/delete/', views.OutboundOrderDeleteView.as_view(), name='order_delete'),
    # 実行系 (handheld) 画面群
    path('handheld/picking/', views.OutboundPickingView.as_view(), name='handheld_picking'),
    path('handheld/picking/<int:pk>/', views.OutboundPickingWorkView.as_view(), name='handheld_picking_work'),
    path('handheld/inspection/', views.OutboundInspectionView.as_view(), name='handheld_inspection'),
    path('handheld/inspection/<int:pk>/', views.OutboundInspectionWorkView.as_view(), name='handheld_inspection_work'),
]
