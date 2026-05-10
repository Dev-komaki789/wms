from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from masters.models import Location, Sku, Supplier, Warehouse
from stock.models import StockMovement


class InboundOrder(models.Model):
    """入荷指示（ASN・手動登録・発注アラート起点・返品など）。"""

    class Status(models.TextChoices):
        PENDING = 'pending', '入荷待ち'
        ARRIVED = 'arrived', '到着済み'
        RECEIVING = 'receiving', '入荷中'
        PUTAWAY = 'putaway', '格納待ち'
        COMPLETED = 'completed', '完了'
        CANCELLED = 'cancelled', '取消'

    class SourceType(models.TextChoices):
        ASN = 'asn', 'ASN自動連携'
        MANUAL = 'manual', '手動登録'
        REORDER_ALERT = 'reorder_alert', '発注アラート起点'
        RETURN = 'return', '返品'

    inbound_order_code = models.CharField('入荷指示番号', max_length=30, unique=True)
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='仕入先',
    )
    status = models.CharField(
        'ステータス', max_length=20, choices=Status.choices, default=Status.PENDING
    )
    expected_date = models.DateField('入荷予定日', null=True, blank=True)
    arrived_at = models.DateTimeField('到着確認日時', null=True, blank=True)
    purchase_order_code = models.CharField(
        'SCMラベル発注番号', max_length=50, blank=True
    )
    supplier_delivery_note_code = models.CharField(
        '仕入先納品書番号', max_length=50, blank=True
    )
    received_at = models.DateTimeField('入荷完了日時', null=True, blank=True)
    source_type = models.CharField(
        '入荷元種別',
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.ASN,
    )
    note = models.TextField('備考', blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        verbose_name='登録者',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'inbound_orders'
        verbose_name = '入荷指示'
        verbose_name_plural = '入荷指示'
        indexes = [
            models.Index(fields=['status'], name='idx_inbound_orders_status'),
            models.Index(
                fields=['expected_date'], name='idx_inbound_orders_expected'
            ),
        ]

    def __str__(self):
        return self.inbound_order_code


class InboundOrderItem(models.Model):
    """入荷指示明細（SKU × 予定数量）。"""

    inbound_order = models.ForeignKey(
        InboundOrder,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='入荷指示',
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity_expected = models.IntegerField(
        '予定入荷数', validators=[MinValueValidator(1)]
    )
    quantity_received = models.IntegerField(
        '実入荷数', default=0, validators=[MinValueValidator(0)]
    )
    is_crossdock = models.BooleanField(
        'クロスドック',
        default=False,
        help_text='true=入荷後そのまま出荷エリアへ（棚入れスキップ）',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'inbound_order_items'
        verbose_name = '入荷指示明細'
        verbose_name_plural = '入荷指示明細'
        constraints = [
            models.UniqueConstraint(
                fields=['inbound_order', 'sku'],
                name='uk_inbound_order_items_order_sku',
            ),
        ]

    def __str__(self):
        return f'{self.inbound_order.inbound_order_code} / {self.sku.sku_code}'


class InboundReceipt(models.Model):
    """入荷検品結果（明細1件 = レコード1件）。差異種別を記録。"""

    class DiscrepancyType(models.TextChoices):
        NONE = 'none', '差異なし'
        SHORTAGE = 'shortage', '数量不足'
        EXCESS = 'excess', '数量超過'
        DAMAGED = 'damaged', '破損'

    inbound_order_item = models.OneToOneField(
        InboundOrderItem,
        on_delete=models.PROTECT,
        verbose_name='入荷指示明細',
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name='格納先ロケーション',
        help_text='検品時はNULL、格納完了時に更新',
    )
    quantity_expected = models.IntegerField(
        '予定入荷数', validators=[MinValueValidator(1)]
    )
    quantity_received = models.IntegerField(
        '実入荷数', validators=[MinValueValidator(0)]
    )
    discrepancy_type = models.CharField(
        '差異種別',
        max_length=20,
        choices=DiscrepancyType.choices,
        default=DiscrepancyType.NONE,
    )
    discrepancy_note = models.TextField('差異理由', blank=True)
    stock_movement = models.ForeignKey(
        StockMovement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='紐づく入庫履歴',
        help_text='damaged の場合は NULL（入庫しないため）',
    )
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='inspected_receipts',
        verbose_name='検品担当者',
    )
    inspected_at = models.DateTimeField('検品完了日時', auto_now_add=True)
    putaway_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='putaway_receipts',
        verbose_name='棚入れ担当者',
    )
    putaway_at = models.DateTimeField('棚入れ完了日時', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'inbound_receipts'
        verbose_name = '入荷検品結果'
        verbose_name_plural = '入荷検品結果'

    def __str__(self):
        return f'{self.inbound_order_item} ({self.get_discrepancy_type_display()})'
