from django.urls import path

from . import views

app_name = 'masters'

urlpatterns = [
    path('warehouses/', views.WarehouseListView.as_view(), name='warehouse_list'),
    path('warehouses/new/', views.WarehouseCreateView.as_view(), name='warehouse_create'),
    path('warehouses/<int:pk>/edit/', views.WarehouseUpdateView.as_view(), name='warehouse_update'),
    path('warehouses/<int:pk>/delete/', views.WarehouseDeleteView.as_view(), name='warehouse_delete'),

    path('areas/', views.AreaListView.as_view(), name='area_list'),
    path('areas/new/', views.AreaCreateView.as_view(), name='area_create'),
    path('areas/<int:pk>/edit/', views.AreaUpdateView.as_view(), name='area_update'),
    path('areas/<int:pk>/delete/', views.AreaDeleteView.as_view(), name='area_delete'),
]
