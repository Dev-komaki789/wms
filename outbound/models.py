from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from inbound.models import InboundOrderItem
from masters.models import Area, Customer, Location, Sku, Warehouse
from stock.models import StockMovement


class OutboundOrder(models.Model):
    """出荷指示（OMS自動連携・手動・返品出荷）。"""

    class Status(models.TextChoices):
        PENDING = 'pending', '出荷待ち'
        PICKING = 'picking', 'ピッキング中'
        INSPECTING = 'inspecting', '検品・梱包・送り状貼付中'
        SHIPPED = 'shipped', '出荷済み'
        CANCELLED = 'cancelled', '取消'

    class SourceType(models.TextChoices):
        OMS = 'oms', 'OMS取り込み'
        MANUAL = 'manual', '手動登録'
        RETURN = 'return', '返品出荷'

    class CancelReason(models.TextChoices):
        STOCK_SHORTAGE = 'stock_shortage', '在庫不足'
        DAMAGE = 'damage', '破損'
        MANUAL = 'manual', '運用判断'
        OMS_REQUEST = 'oms_request', 'OMS要求'

    outbound_order_code = models.CharField('出荷指示番号', max_length=30, unique=True)
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='顧客',
    )
    external_order_id = models.CharField(
        'OMS注文番号', max_length=50, blank=True, db_index=True
    )
    delivery_postal_code = models.CharField('配送先郵便番号', max_length=10, blank=True)
    delivery_address = models.CharField('配送先住所', max_length=255, blank=True)
    delivery_name = models.CharField('配送先名称', max_length=100, blank=True)
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    source_type = models.CharField(
        '出荷元種別',
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.OMS,
    )
    priority = models.IntegerField('優先度', default=0, validators=[MinValueValidator(0)])
    deadline_at = models.DateTimeField('出荷期限', null=True, blank=True)
    shipped_at = models.DateTimeField('出荷日時', null=True, blank=True)
    cancelled_at = models.DateTimeField('取消日時', null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_outbound_orders',
        verbose_name='取消実施者',
    )
    cancel_reason = models.CharField(
        '取消理由',
        max_length=20,
        choices=CancelReason.choices,
        blank=True,
        null=True,
    )
    note = models.TextField('備考', blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_outbound_orders',
        verbose_name='登録者',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'outbound_orders'
        verbose_name = '出荷指示'
        verbose_name_plural = '出荷指示'
        indexes = [
            models.Index(fields=['status'], name='idx_outbound_orders_status'),
            models.Index(fields=['deadline_at'], name='idx_outbound_orders_deadline'),
        ]

    def __str__(self):
        return self.outbound_order_code


class StockReservation(models.Model):
    """在庫引き当て（active/released/expired）。出荷確定時にreleasedへ。"""

    class Status(models.TextChoices):
        ACTIVE = 'active', '引き当て中'
        RELEASED = 'released', '解放済み'
        EXPIRED = 'expired', '期限切れ'

    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, verbose_name='ロケーション'
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity = models.IntegerField('引き当て数', validators=[MinValueValidator(1)])
    status = models.CharField(
        'ステータス', max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    order = models.ForeignKey(
        OutboundOrder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reservations',
        verbose_name='出荷指示',
    )
    inbound_order_item = models.ForeignKey(
        InboundOrderItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='紐づく入荷予定明細',
        help_text='ASN引き当ての場合のみセット',
    )
    is_crossdock = models.BooleanField('クロスドック', default=False)
    expires_at = models.DateTimeField('有効期限', null=True, blank=True)
    released_at = models.DateTimeField('解放日時', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        verbose_name='操作ユーザー',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'stock_reservations'
        verbose_name = '在庫引き当て'
        verbose_name_plural = '在庫引き当て'
        indexes = [
            models.Index(
                fields=['location', 'sku', 'status'], name='idx_reservations_lss'
            ),
            models.Index(fields=['status'], name='idx_reservations_status'),
        ]

    def __str__(self):
        return f'RSV {self.sku.sku_code} x{self.quantity} ({self.get_status_display()})'


class OutboundOrderItem(models.Model):
    """出荷指示明細（SKU × ロケーション × 数量）。"""

    outbound_order = models.ForeignKey(
        OutboundOrder,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='出荷指示',
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, verbose_name='ピッキング元ロケーション'
    )
    reservation = models.ForeignKey(
        StockReservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='紐づく引き当て',
    )
    quantity_ordered = models.IntegerField(
        '指示数量', validators=[MinValueValidator(1)]
    )
    quantity_shipped = models.IntegerField(
        '実出荷数', default=0, validators=[MinValueValidator(0)]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'outbound_order_items'
        verbose_name = '出荷指示明細'
        verbose_name_plural = '出荷指示明細'
        constraints = [
            models.UniqueConstraint(
                fields=['outbound_order', 'sku', 'location'],
                name='uk_outbound_order_items_osl',
            ),
        ]

    def __str__(self):
        return f'{self.outbound_order.outbound_order_code} / {self.sku.sku_code}'


class PickingList(models.Model):
    """ピッキングリスト（エリア単位で分割発行）。MVPはorderタイプのみ実装。"""

    class PickingType(models.TextChoices):
        ORDER = 'order', 'オーダーピッキング'
        TOTAL = 'total', '種まき方式（AGV/GTP）'
        ZONE = 'zone', 'ゾーンピッキング'

    class Status(models.TextChoices):
        PENDING = 'pending', '未生成'
        IN_PROGRESS = 'in_progress', '作業中'
        COMPLETED = 'completed', '完了'
        CANCELLED = 'cancelled', '取消'

    picking_list_code = models.CharField(
        'ピッキングリスト番号', max_length=30, unique=True
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    area = models.ForeignKey(Area, on_delete=models.PROTECT, verbose_name='対象エリア')
    picking_type = models.CharField(
        'ピッキング種別',
        max_length=20,
        choices=PickingType.choices,
        default=PickingType.ORDER,
    )
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_picking_lists',
        verbose_name='担当作業者',
    )
    started_at = models.DateTimeField('開始日時', null=True, blank=True)
    completed_at = models.DateTimeField('完了日時', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_picking_lists',
        verbose_name='作成者',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'picking_lists'
        verbose_name = 'ピッキングリスト'
        verbose_name_plural = 'ピッキングリスト'
        indexes = [
            models.Index(fields=['status'], name='idx_picking_lists_status'),
        ]

    def __str__(self):
        return self.picking_list_code


class PickingListItem(models.Model):
    """ピッキングリスト明細。"""

    class Status(models.TextChoices):
        PENDING = 'pending', '未生成'
        PICKING = 'picking', '作業中'
        PICKED = 'picked', '完了'
        SHORT = 'short', '欠品'
        CANCELLED = 'cancelled', 'キャンセル'

    picking_list = models.ForeignKey(
        PickingList,
        on_delete=models.PROTECT,
        related_name='items',
        verbose_name='ピッキングリスト',
    )
    outbound_order_item = models.ForeignKey(
        OutboundOrderItem, on_delete=models.PROTECT, verbose_name='出荷指示明細'
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, verbose_name='ピッキング元'
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity_requested = models.IntegerField(
        '指示数量', validators=[MinValueValidator(1)]
    )
    quantity_picked = models.IntegerField(
        '実ピッキング数', default=0, validators=[MinValueValidator(0)]
    )
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='ピッキング担当者',
    )
    picked_at = models.DateTimeField('完了日時', null=True, blank=True)
    note = models.TextField('備考', blank=True)
    sort_order = models.IntegerField(
        'ソート順',
        null=True,
        blank=True,
        help_text='order タイプのみ有効。棚番順',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'picking_list_items'
        verbose_name = 'ピッキング明細'
        verbose_name_plural = 'ピッキング明細'

    def __str__(self):
        return f'{self.picking_list.picking_list_code} / {self.sku.sku_code}'


class Shipment(models.Model):
    """出荷実績。1出荷指示=1出荷（MVPでは複数箱対応なし）。"""

    class Status(models.TextChoices):
        INSPECTING = 'inspecting', '検品・梱包・送り状貼付中'
        READY = 'ready', '出荷準備完了'
        SHIPPED = 'shipped', '出荷済み'

    shipment_code = models.CharField('出荷番号', max_length=30, unique=True)
    outbound_order = models.OneToOneField(
        OutboundOrder, on_delete=models.PROTECT, verbose_name='出荷指示'
    )
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.INSPECTING,
    )
    carrier_name = models.CharField(
        '運送会社名', max_length=100, blank=True, help_text='例: ヤマト運輸'
    )
    tracking_number = models.CharField('追跡番号', max_length=100, blank=True)
    shipped_at = models.DateTimeField('出荷日時', null=True, blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inspected_shipments',
        verbose_name='検品担当者',
    )
    inspected_at = models.DateTimeField('検品完了日時', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_shipments',
        verbose_name='登録者',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'shipments'
        verbose_name = '出荷実績'
        verbose_name_plural = '出荷実績'
        indexes = [
            models.Index(fields=['status'], name='idx_shipments_status'),
            models.Index(fields=['shipped_at'], name='idx_shipments_shipped_at'),
        ]

    def __str__(self):
        return self.shipment_code


class ShipmentItem(models.Model):
    """出荷実績明細。追記専用。"""

    shipment = models.ForeignKey(
        Shipment, on_delete=models.PROTECT, related_name='items', verbose_name='出荷'
    )
    outbound_order_item = models.ForeignKey(
        OutboundOrderItem, on_delete=models.PROTECT, verbose_name='出荷指示明細'
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity_shipped = models.IntegerField(
        '出荷数量', validators=[MinValueValidator(1)]
    )
    stock_movement = models.ForeignKey(
        StockMovement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='紐づく出庫履歴',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'shipment_items'
        verbose_name = '出荷実績明細'
        verbose_name_plural = '出荷実績明細'
        constraints = [
            models.UniqueConstraint(
                fields=['shipment', 'sku'], name='uk_shipment_items_shipment_sku'
            ),
        ]

    def __str__(self):
        return f'{self.shipment.shipment_code} / {self.sku.sku_code}'


class DeliveryNote(models.Model):
    """納品書（1出荷指示=1納品書）。"""

    delivery_note_code = models.CharField('納品書番号', max_length=30, unique=True)
    outbound_order = models.OneToOneField(
        OutboundOrder, on_delete=models.PROTECT, verbose_name='出荷指示'
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, verbose_name='顧客'
    )
    issued_at = models.DateTimeField('発行日時', auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'delivery_notes'
        verbose_name = '納品書'
        verbose_name_plural = '納品書'

    def __str__(self):
        return self.delivery_note_code


class DeliveryNoteItem(models.Model):
    """納品書明細。商品名・SKUコードはスナップショット保存。"""

    delivery_note = models.ForeignKey(
        DeliveryNote,
        on_delete=models.PROTECT,
        related_name='items',
        verbose_name='納品書',
    )
    outbound_order_item = models.OneToOneField(
        OutboundOrderItem, on_delete=models.PROTECT, verbose_name='出荷指示明細'
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    product_name = models.CharField(
        '商品名（スナップショット）', max_length=200
    )
    sku_code = models.CharField('SKUコード（スナップショット）', max_length=50)
    quantity = models.IntegerField('数量', validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'delivery_note_items'
        verbose_name = '納品書明細'
        verbose_name_plural = '納品書明細'

    def __str__(self):
        return f'{self.delivery_note.delivery_note_code} / {self.sku_code}'
