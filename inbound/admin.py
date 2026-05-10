from django.contrib import admin

from .models import InboundOrder, InboundOrderItem, InboundReceipt


class InboundOrderItemInline(admin.TabularInline):
    model = InboundOrderItem
    extra = 0
    autocomplete_fields = ('sku',)


@admin.register(InboundOrder)
class InboundOrderAdmin(admin.ModelAdmin):
    list_display = (
        'inbound_order_code',
        'warehouse',
        'supplier',
        'status',
        'source_type',
        'expected_date',
        'received_at',
        'created_by',
    )
    search_fields = ('inbound_order_code', 'purchase_order_code', 'supplier_delivery_note_code')
    list_filter = ('status', 'source_type', 'warehouse')
    date_hierarchy = 'expected_date'
    autocomplete_fields = ('warehouse', 'supplier', 'created_by')
    inlines = [InboundOrderItemInline]


@admin.register(InboundOrderItem)
class InboundOrderItemAdmin(admin.ModelAdmin):
    list_display = ('inbound_order', 'sku', 'quantity_expected', 'quantity_received', 'is_crossdock')
    search_fields = ('inbound_order__inbound_order_code', 'sku__sku_code')
    list_filter = ('is_crossdock',)
    autocomplete_fields = ('inbound_order', 'sku')


@admin.register(InboundReceipt)
class InboundReceiptAdmin(admin.ModelAdmin):
    list_display = (
        'inbound_order_item',
        'location',
        'quantity_expected',
        'quantity_received',
        'discrepancy_type',
        'inspected_by',
        'inspected_at',
        'putaway_at',
    )
    search_fields = ('inbound_order_item__inbound_order__inbound_order_code',)
    list_filter = ('discrepancy_type',)
    date_hierarchy = 'inspected_at'
    autocomplete_fields = ('inbound_order_item', 'location', 'stock_movement', 'inspected_by', 'putaway_by')
    readonly_fields = ('inspected_at', 'created_at')
