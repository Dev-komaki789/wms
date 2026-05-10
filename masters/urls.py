from django.urls import path

from . import views

app_name = 'masters'

urlpatterns = [
    path('warehouses/', views.WarehouseListView.as_view(), name='warehouse_list'),
    path('warehouses/new/', views.WarehouseCreateView.as_view(), name='warehouse_create'),
    path('warehouses/<int:pk>/edit/', views.WarehouseUpdateView.as_view(), name='warehouse_update'),
    path('warehouses/<int:pk>/delete/', views.WarehouseDeleteView.as_view(), name='warehouse_delete'),
]
