from django.urls import path

from . import views

app_name = 'outbound'

urlpatterns = [
    path('orders/', views.OutboundOrderInquiryView.as_view(), name='order_inquiry'),
    path('launch/', views.OutboundLaunchView.as_view(), name='launch'),
    path('picking-lists/', views.PickingListInquiryView.as_view(), name='picking_list_inquiry'),
    path('picking-lists/<int:pk>/print/', views.PickingListPrintView.as_view(), name='picking_list_print'),
    # 出荷明細書印刷（出荷検品完了時に実出荷数で発行された DeliveryNote の帳票印刷）。
    # 箱に同梱して顧客へ届ける書類。
    path('delivery-notes/<int:pk>/print/', views.DeliveryNotePrintView.as_view(), name='delivery_note_print'),
    path('orders/new/', views.OutboundOrderCreateView.as_view(), name='order_create'),
    path('orders/csv/export/', views.OutboundOrderCsvExportView.as_view(), name='order_csv_export'),
    path('orders/csv/import/', views.OutboundOrderCsvImportView.as_view(), name='order_csv_import'),
    path('orders/<int:pk>/', views.OutboundOrderDetailView.as_view(), name='order_detail'),
    path('orders/<int:pk>/edit/', views.OutboundOrderUpdateView.as_view(), name='order_update'),
    path('orders/<int:pk>/delete/', views.OutboundOrderDeleteView.as_view(), name='order_delete'),
    # 実行系 (handheld) 画面群
    path('handheld/picking/', views.OutboundPickingView.as_view(), name='handheld_picking'),
    path('handheld/picking/<int:pk>/', views.OutboundPickingWorkView.as_view(), name='handheld_picking_work'),
    # 1 明細ごとの即時コミット API（fetch から呼ばれる）。
    # 全明細が確定したらリストを COMPLETED に進める。
    path('handheld/picking/<int:list_pk>/item/<int:item_pk>/',
         views.OutboundPickingItemView.as_view(), name='handheld_picking_item'),
    path('handheld/inspection/', views.OutboundInspectionView.as_view(), name='handheld_inspection'),
    path('handheld/inspection/<int:pk>/', views.OutboundInspectionWorkView.as_view(), name='handheld_inspection_work'),
    # 1 明細ごとの即時コミット API（fetch から呼ばれる）。
    # 全明細が確定したら ShipmentItem を作成して出荷確定する。
    path('handheld/inspection/<int:order_pk>/item/<int:item_pk>/',
         views.OutboundInspectionItemView.as_view(), name='handheld_inspection_item'),
]
