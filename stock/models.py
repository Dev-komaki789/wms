import re

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from masters.models import Area, Location, Sku, Warehouse


class StockBalance(models.Model):
    """ロケーション×SKU の現在在庫数。入出庫のたびに加減算して更新。

    `first_received_at` は「現在棚に乗っている在庫の最古入荷日時」。
    複数棚に同 SKU がある時の引き当てで FIFO に近い順序を取るために使う
    （詳細は outbound.views._try_launch_order 参照）。
    棚の在庫が一旦 0 になった後で再充填されたタイミングでリセットする
    （= "現在の在庫期間" の起点）。在庫が残っている棚への追加充填では
    更新しない（古い在庫がまだ残っているため）。
    """

    location = models.ForeignKey(Location, on_delete=models.PROTECT, verbose_name='ロケーション')
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity = models.IntegerField(
        '在庫数',
        default=0,
        validators=[MinValueValidator(0)],
    )
    first_received_at = models.DateTimeField(
        '現在在庫の最古入荷日時',
        null=True,
        blank=True,
        db_index=True,
        help_text='quantity が 0 → 正に切り替わった時刻。FIFO 引き当ての並び替えに使う。',
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

    movement_type = models.CharField('種別', max_length=10, choices=MovementType.choices)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, verbose_name='ロケーション')
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    quantity = models.IntegerField('数量', help_text='OUT/ADJ減の場合は負の値で記録')
    quantity_before = models.IntegerField('変動前在庫数')
    quantity_after = models.IntegerField(
        '変動後在庫数',
        validators=[MinValueValidator(0)],
    )
    reference_type = models.CharField(
        '作業内容',
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
            models.Index(fields=['-transferred_at'], name='idx_stock_transfers_at'),
        ]

    def __str__(self):
        return (
            f'{self.sku.sku_code} {self.from_location.location_code}'
            f'→{self.to_location.location_code} x{self.quantity}'
        )


class StocktakeSession(models.Model):
    """棚卸セッション。1回の棚卸作業の単位を管理する。

    全数棚卸（warehouse 配下の全ロケーション×SKU）と循環棚卸（特定エリア）の
    どちらかを扱う。状態遷移: PLANNING（作成直後）→ COUNTING（開始時に
    StockBalance のスナップショットを stocktake_items に展開）→ REVIEW
    （差異確認）→ COMPLETED（差異 != 0 の各明細に StockMovement.ADJ を発行）。
    取消は PLANNING/COUNTING/REVIEW のいずれからでも CANCELLED へ。
    """

    class StocktakeType(models.TextChoices):
        FULL = 'full', '全数棚卸'
        CYCLE = 'cycle', '循環棚卸'

    class Status(models.TextChoices):
        PLANNING = 'planning', '計画中'
        COUNTING = 'counting', 'カウント中'
        REVIEW = 'review', '差異確認中'
        COMPLETED = 'completed', '完了'
        CANCELLED = 'cancelled', '取消'

    session_code = models.CharField('棚卸番号', max_length=30, unique=True)
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        verbose_name='倉庫',
    )
    stocktake_type = models.CharField(
        '種別',
        max_length=20,
        choices=StocktakeType.choices,
    )
    area = models.ForeignKey(
        Area,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name='対象エリア',
        help_text='循環棚卸のときの対象エリア（全数棚卸では空）',
    )
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.PLANNING,
    )
    planned_at = models.DateField('棚卸予定日')
    started_at = models.DateTimeField('開始日時', null=True, blank=True)
    completed_at = models.DateTimeField('完了日時', null=True, blank=True)
    is_locked = models.BooleanField(
        '入出庫ロック中',
        default=False,
        help_text='全数棚卸の間に入出庫をロックする想定のフラグ'
        '（MVP では値の保持のみ。他の在庫変動画面では未参照）',
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
        db_table = 'stocktake_sessions'
        verbose_name = '棚卸セッション'
        verbose_name_plural = '棚卸セッション'
        indexes = [
            models.Index(fields=['status'], name='idx_stocktake_sessions_st'),
            models.Index(fields=['planned_at'], name='idx_stocktake_sessions_at'),
        ]

    def __str__(self):
        return self.session_code

    @classmethod
    def next_code(cls, date):
        """次の棚卸番号（ST-YYYYMMDD-NNN）を採番する。"""
        prefix = f'ST-{date.strftime("%Y%m%d")}-'
        max_n = 0
        for code in cls.objects.filter(session_code__startswith=prefix).values_list(
            'session_code', flat=True
        ):
            m = re.match(rf'^{re.escape(prefix)}(\d{{3}})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'{prefix}{max_n + 1:03d}'


class StocktakeItem(models.Model):
    """棚卸明細。ロケーション×SKU 単位の帳簿数（snapshot）と実カウント数を持つ。"""

    class Status(models.TextChoices):
        UNCOUNTED = 'uncounted', '未カウント'
        COUNTED = 'counted', 'カウント済'
        ADJUSTED = 'adjusted', '調整済'

    session = models.ForeignKey(
        StocktakeSession,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='棚卸セッション',
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        verbose_name='ロケーション',
    )
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, verbose_name='SKU')
    system_quantity = models.IntegerField(
        '帳簿数',
        validators=[MinValueValidator(0)],
        help_text='棚卸開始時点の StockBalance.quantity のスナップショット',
    )
    counted_quantity = models.IntegerField(
        '実カウント数',
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    status = models.CharField(
        'ステータス',
        max_length=20,
        choices=Status.choices,
        default=Status.UNCOUNTED,
    )
    counted_at = models.DateTimeField('カウント日時', null=True, blank=True)
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='counted_stocktake_items',
        verbose_name='カウント担当者',
    )
    note = models.TextField('備考', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'stocktake_items'
        verbose_name = '棚卸明細'
        verbose_name_plural = '棚卸明細'
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'location', 'sku'],
                name='uk_stocktake_items_session_location_sku',
            ),
        ]
        indexes = [
            models.Index(fields=['status'], name='idx_stocktake_items_status'),
        ]

    def __str__(self):
        return f'{self.session.session_code} / {self.location.location_code} / {self.sku.sku_code}'

    @property
    def difference(self):
        """実カウント数 − 帳簿数。カウント前は None。"""
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.system_quantity


class StocktakeAdjustment(models.Model):
    """棚卸で差異が出た明細と StockMovement.ADJ の紐付け（差異 != 0 のときのみ）。"""

    stocktake_item = models.OneToOneField(
        StocktakeItem,
        on_delete=models.CASCADE,
        related_name='adjustment',
        verbose_name='棚卸明細',
    )
    stock_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        related_name='stocktake_adjustment',
        verbose_name='在庫変動',
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        verbose_name='承認者',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'stocktake_adjustments'
        verbose_name = '棚卸調整'
        verbose_name_plural = '棚卸調整'

    def __str__(self):
        return f'adjust {self.stocktake_item}'
