from django.contrib import admin

from .models import (
    DeliveryNote,
    DeliveryNoteItem,
    OutboundOrder,
    OutboundOrderItem,
    PickingList,
    PickingListItem,
    Shipment,
    ShipmentItem,
    StockReservation,
)


class OutboundOrderItemInline(admin.TabularInline):
    model = OutboundOrderItem
    extra = 0
    autocomplete_fields = ('sku', 'location', 'reservation')


@admin.register(OutboundOrder)
class OutboundOrderAdmin(admin.ModelAdmin):
    list_display = (
        'outbound_order_code',
        'warehouse',
        'customer',
        'status',
        'source_type',
        'deadline_at',
        'shipped_at',
        'created_by',
    )
    search_fields = ('outbound_order_code', 'external_order_id', 'delivery_name')
    list_filter = ('status', 'source_type', 'warehouse')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('warehouse', 'customer', 'created_by', 'cancelled_by')
    inlines = [OutboundOrderItemInline]


@admin.register(OutboundOrderItem)
class OutboundOrderItemAdmin(admin.ModelAdmin):
    list_display = ('outbound_order', 'sku', 'location', 'quantity_ordered', 'quantity_shipped')
    search_fields = ('outbound_order__outbound_order_code', 'sku__sku_code')
    autocomplete_fields = ('outbound_order', 'sku', 'location', 'reservation')


@admin.register(StockReservation)
class StockReservationAdmin(admin.ModelAdmin):
    list_display = ('order', 'sku', 'location', 'quantity', 'status', 'is_crossdock', 'expires_at')
    search_fields = ('sku__sku_code', 'location__location_code')
    list_filter = ('status', 'is_crossdock')
    autocomplete_fields = ('location', 'sku', 'order', 'inbound_order_item', 'created_by')


class PickingListItemInline(admin.TabularInline):
    model = PickingListItem
    extra = 0
    autocomplete_fields = ('outbound_order_item', 'location', 'sku', 'picked_by')


@admin.register(PickingList)
class PickingListAdmin(admin.ModelAdmin):
    list_display = (
        'picking_list_code',
        'warehouse',
        'area',
        'picking_type',
        'status',
        'assigned_to',
        'started_at',
        'completed_at',
    )
    search_fields = ('picking_list_code',)
    list_filter = ('status', 'picking_type', 'warehouse')
    autocomplete_fields = ('warehouse', 'area', 'assigned_to', 'created_by')
    inlines = [PickingListItemInline]


@admin.register(PickingListItem)
class PickingListItemAdmin(admin.ModelAdmin):
    list_display = (
        'picking_list',
        'sku',
        'location',
        'quantity_requested',
        'quantity_picked',
        'status',
        'picked_by',
        'picked_at',
    )
    search_fields = ('picking_list__picking_list_code', 'sku__sku_code')
    list_filter = ('status',)
    autocomplete_fields = ('picking_list', 'outbound_order_item', 'location', 'sku', 'picked_by')


class ShipmentItemInline(admin.TabularInline):
    model = ShipmentItem
    extra = 0
    autocomplete_fields = ('outbound_order_item', 'sku', 'stock_movement')


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = (
        'shipment_code',
        'outbound_order',
        'status',
        'carrier_name',
        'tracking_number',
        'shipped_at',
        'inspected_by',
    )
    search_fields = ('shipment_code', 'tracking_number', 'outbound_order__outbound_order_code')
    list_filter = ('status', 'carrier_name')
    date_hierarchy = 'shipped_at'
    autocomplete_fields = ('outbound_order', 'inspected_by', 'created_by')
    inlines = [ShipmentItemInline]


@admin.register(ShipmentItem)
class ShipmentItemAdmin(admin.ModelAdmin):
    list_display = ('shipment', 'sku', 'quantity_shipped', 'stock_movement')
    search_fields = ('shipment__shipment_code', 'sku__sku_code')
    autocomplete_fields = ('shipment', 'outbound_order_item', 'sku', 'stock_movement')


class DeliveryNoteItemInline(admin.TabularInline):
    model = DeliveryNoteItem
    extra = 0
    autocomplete_fields = ('outbound_order_item', 'sku')


@admin.register(DeliveryNote)
class DeliveryNoteAdmin(admin.ModelAdmin):
    list_display = ('delivery_note_code', 'outbound_order', 'customer', 'issued_at')
    search_fields = ('delivery_note_code', 'outbound_order__outbound_order_code')
    date_hierarchy = 'issued_at'
    autocomplete_fields = ('outbound_order', 'customer')
    inlines = [DeliveryNoteItemInline]


@admin.register(DeliveryNoteItem)
class DeliveryNoteItemAdmin(admin.ModelAdmin):
    list_display = ('delivery_note', 'sku_code', 'product_name', 'quantity')
    search_fields = ('delivery_note__delivery_note_code', 'sku_code', 'product_name')
    autocomplete_fields = ('delivery_note', 'outbound_order_item', 'sku')
