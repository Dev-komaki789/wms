from django.urls import path

from . import views

app_name = 'masters'

urlpatterns = [
    path('area-locations/', views.MasterInquiryView.as_view(), name='master_inquiry'),

    # 倉庫 CRUD は Django admin (/admin/masters/warehouse/) に集約

    path('areas/', views.AreaListView.as_view(), name='area_list'),
    path('areas/new/', views.AreaCreateView.as_view(), name='area_create'),
    path('areas/<int:pk>/edit/', views.AreaUpdateView.as_view(), name='area_update'),
    path('areas/<int:pk>/delete/', views.AreaDeleteView.as_view(), name='area_delete'),

    path('locations/', views.LocationListView.as_view(), name='location_list'),
    path('locations/new/', views.LocationCreateView.as_view(), name='location_create'),
    path('locations/<int:pk>/edit/', views.LocationUpdateView.as_view(), name='location_update'),
    path('locations/<int:pk>/delete/', views.LocationDeleteView.as_view(), name='location_delete'),

    path('categories/', views.CategoryListView.as_view(), name='category_list'),
    path('categories/new/', views.CategoryCreateView.as_view(), name='category_create'),
    path('categories/<int:pk>/edit/', views.CategoryUpdateView.as_view(), name='category_update'),
    path('categories/<int:pk>/delete/', views.CategoryDeleteView.as_view(), name='category_delete'),

    path('manufacturers/', views.ManufacturerListView.as_view(), name='manufacturer_list'),
    path('manufacturers/new/', views.ManufacturerCreateView.as_view(), name='manufacturer_create'),
    path('manufacturers/<int:pk>/edit/', views.ManufacturerUpdateView.as_view(), name='manufacturer_update'),
    path('manufacturers/<int:pk>/delete/', views.ManufacturerDeleteView.as_view(), name='manufacturer_delete'),

    path('products/', views.ProductInquiryView.as_view(), name='product_inquiry'),
    path('products/new/', views.ProductCreateView.as_view(), name='product_create'),
    path('products/<int:pk>/edit/', views.ProductUpdateView.as_view(), name='product_update'),
    path('products/<int:pk>/delete/', views.ProductDeleteView.as_view(), name='product_delete'),

    path('skus/', views.SkuInquiryView.as_view(), name='sku_inquiry'),
    path('skus/new/', views.SkuCreateView.as_view(), name='sku_create'),
    path('skus/<int:pk>/edit/', views.SkuUpdateView.as_view(), name='sku_update'),
    path('skus/<int:pk>/delete/', views.SkuDeleteView.as_view(), name='sku_delete'),
]
