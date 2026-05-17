from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from masters.models import Location, Sku


class StockBalance(models.Model):
    """ロケーション×SKU の現在在庫数。入出庫のたびに加減算して更新。"""

    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, verbose_name='ロケーション'
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity = models.IntegerField(
        '在庫数',
        default=0,
        validators=[MinValueValidator(0)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'stock_balances'
        verbose_name = '在庫'
        verbose_name_plural = '在庫'
        constraints = [
            models.UniqueConstraint(
                fields=['location', 'sku'], name='uk_stock_balances_location_sku'
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name='chk_stock_balances_quantity',
            ),
        ]

    def __str__(self):
        return f'{self.location.location_code} / {self.sku.sku_code}: {self.quantity}'


class StockMovement(models.Model):
    """在庫移動の追記専用ログ。updated_at は持たない。"""

    class MovementType(models.TextChoices):
        IN = 'IN', '入庫'
        OUT = 'OUT', '出庫'
        ADJ = 'ADJ', '棚卸調整'

    class ReferenceType(models.TextChoices):
        INBOUND_ORDER = 'inbound_order', '入荷指示'
        OUTBOUND_ORDER = 'outbound_order', '出荷指示'
        STOCK_TRANSFER = 'stock_transfer', '棚間移動'
        STOCKTAKE = 'stocktake', '棚卸'
        MANUAL_IN = 'manual_in', '計画外入庫'
        MANUAL_OUT = 'manual_out', '計画外出庫'

    movement_type = models.CharField(
        '種別', max_length=10, choices=MovementType.choices
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, verbose_name='ロケーション'
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity = models.IntegerField('数量', help_text='OUT/ADJ減の場合は負の値で記録')
    quantity_before = models.IntegerField('変動前在庫数')
    quantity_after = models.IntegerField(
        '変動後在庫数',
        validators=[MinValueValidator(0)],
    )
    reference_type = models.CharField(
        '伝票種別',
        max_length=20,
        choices=ReferenceType.choices,
        blank=True,
        null=True,
    )
    reference_id = models.IntegerField('伝票ID', blank=True, null=True)
    note = models.TextField('備考', blank=True)
    moved_at = models.DateTimeField('移動日時', auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        verbose_name='操作ユーザー',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'stock_movements'
        verbose_name = '入出庫履歴'
        verbose_name_plural = '入出庫履歴'
        constraints = [
            models.CheckConstraint(
                check=~models.Q(quantity=0),
                name='chk_stock_movements_quantity_nonzero',
            ),
            models.CheckConstraint(
                check=models.Q(quantity_after__gte=0),
                name='chk_stock_movements_quantity_after',
            ),
        ]
        indexes = [
            models.Index(fields=['sku', '-moved_at'], name='idx_movements_sku_moved'),
            models.Index(fields=['movement_type'], name='idx_movements_type'),
            models.Index(
                fields=['reference_type', 'reference_id'],
                name='idx_movements_reference',
            ),
        ]

    def __str__(self):
        return f'{self.movement_type} {self.sku.sku_code} {self.quantity:+d}'


class StockTransfer(models.Model):
    """棚間移動（ロケーション間の在庫移動）。

    移動元から移動先へ在庫を移す操作。実行時に移動元 OUT・移動先 IN の
    StockMovement を2本発行し（reference_type=stock_transfer）、両ロケーションの
    StockBalance を増減する。MVP では移動は即時完了（status=completed）。
    """

    class Status(models.TextChoices):
        COMPLETED = 'completed', '完了'
        CANCELLED = 'cancelled', '取消'

    from_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='transfers_out',
        verbose_name='移動元ロケーション',
    )
    to_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='transfers_in',
        verbose_name='移動先ロケーション',
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity = models.IntegerField('移動数', validators=[MinValueValidator(1)])
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.COMPLETED,
    )
    transferred_at = models.DateTimeField('移動日時', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        verbose_name='操作ユーザー',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'stock_transfers'
        verbose_name = '棚間移動'
        verbose_name_plural = '棚間移動'
        indexes = [
            models.Index(
                fields=['-transferred_at'], name='idx_stock_transfers_at'
            ),
        ]

    def __str__(self):
        return (
            f'{self.sku.sku_code} {self.from_location.location_code}'
            f'→{self.to_location.location_code} x{self.quantity}'
        )
