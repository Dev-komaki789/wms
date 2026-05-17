from django.urls import path

from . import views

app_name = 'inbound'

urlpatterns = [
    path('orders/', views.InboundOrderInquiryView.as_view(), name='order_inquiry'),
    path('orders/new/', views.InboundOrderCreateView.as_view(), name='order_create'),
    path('orders/<int:pk>/', views.InboundOrderDetailView.as_view(), name='order_detail'),
    path('orders/<int:pk>/edit/', views.InboundOrderUpdateView.as_view(), name='order_update'),
    path('orders/<int:pk>/delete/', views.InboundOrderDeleteView.as_view(), name='order_delete'),
    # 実行系 (handheld) 画面群
    path('handheld/arrival/', views.InboundArrivalView.as_view(), name='handheld_arrival'),
    path('handheld/receiving/<int:pk>/', views.InboundReceivingView.as_view(), name='handheld_receiving'),
    path('handheld/inspection/', views.InboundInspectionView.as_view(), name='handheld_inspection'),
    path('handheld/inspection/<int:pk>/', views.InboundInspectionWorkView.as_view(), name='handheld_inspection_work'),
    path('handheld/putaway/', views.InboundPutawayView.as_view(), name='handheld_putaway'),
    path('handheld/putaway/<int:pk>/', views.InboundPutawayWorkView.as_view(), name='handheld_putaway_work'),
]
