from django.contrib import admin

from .models import (
    Area,
    Category,
    Customer,
    Location,
    Manufacturer,
    Product,
    Sku,
    Supplier,
    Warehouse,
)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('warehouse_code', 'warehouse_name', 'is_active')
    search_fields = ('warehouse_code', 'warehouse_name')
    list_filter = ('is_active',)


@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ('area_code', 'area_name', 'warehouse', 'is_active')
    search_fields = ('area_code', 'area_name')
    list_filter = ('warehouse', 'is_active')


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('location_code', 'location_name', 'warehouse', 'area', 'location_type', 'is_active')
    search_fields = ('location_code', 'location_name')
    list_filter = ('warehouse', 'area', 'location_type', 'is_active')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('category_code', 'category_name', 'parent')
    search_fields = ('category_code', 'category_name')
    list_filter = ('parent',)


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = ('manufacturer_code', 'manufacturer_name', 'is_active')
    search_fields = ('manufacturer_code', 'manufacturer_name')
    list_filter = ('is_active',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_code', 'product_name', 'category', 'manufacturer', 'is_active')
    search_fields = ('product_code', 'product_name')
    list_filter = ('category', 'manufacturer', 'is_active')


@admin.register(Sku)
class SkuAdmin(admin.ModelAdmin):
    list_display = ('sku_code', 'product', 'jan_code', 'size_info', 'color_info', 'picking_type', 'is_active')
    search_fields = ('sku_code', 'jan_code', 'product__product_name')
    list_filter = ('picking_type', 'is_active')
    autocomplete_fields = ('product',)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('supplier_code', 'supplier_name', 'contact_person', 'phone_number', 'is_active')
    search_fields = ('supplier_code', 'supplier_name')
    list_filter = ('is_active',)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('customer_code', 'customer_name', 'customer_type', 'industry_type', 'is_active')
    search_fields = ('customer_code', 'customer_name')
    list_filter = ('customer_type', 'is_active')
