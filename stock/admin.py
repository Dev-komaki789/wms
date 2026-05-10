from django.contrib import admin

from .models import StockBalance, StockMovement


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    list_display = ('location', 'sku', 'quantity', 'updated_at')
    search_fields = ('location__location_code', 'sku__sku_code', 'sku__jan_code')
    list_filter = ('location__warehouse', 'location__area')
    autocomplete_fields = ('location', 'sku')


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        'moved_at',
        'movement_type',
        'location',
        'sku',
        'quantity',
        'quantity_before',
        'quantity_after',
        'reference_type',
        'reference_id',
        'created_by',
    )
    search_fields = ('location__location_code', 'sku__sku_code', 'note')
    list_filter = ('movement_type', 'reference_type', 'location__warehouse')
    date_hierarchy = 'moved_at'
    autocomplete_fields = ('location', 'sku', 'created_by')
    readonly_fields = ('moved_at', 'created_at')
