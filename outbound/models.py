import re

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from inbound.models import InboundOrderItem
from masters.models import Area, Customer, Location, Sku, Warehouse
from stock.models import StockMovement


class OutboundOrder(models.Model):
    """出荷指示（OMS自動連携・手動・返品出荷）。"""

    class Status(models.TextChoices):
        # ステータス = 「次にやる作業」で統一（出荷起動→ピッキング→出荷検品→出荷完了）
        # 「作業中」はステータスにせず、各工程の作業成果物のロックで表現する
        # （ピッキング: PickingList.assigned_to / 検品梱包: Shipment.in_progress_by）
        ALLOCATION_WAIT = 'allocation_wait', '出荷起動待ち'
        PICKING_WAIT = 'picking_wait', 'ピッキング作業待ち'
        INSPECTION_WAIT = 'inspection_wait', '出荷検品作業待ち'
        SHIPPED = 'shipped', '出荷完了'
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
        default=Status.ALLOCATION_WAIT,
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

    # 画面登録の出荷指示番号は種別ごとに prefix を変えて一目で識別できるようにする
    # （OMS連携は上位システムの番号をそのまま使うので採番対象外）
    CODE_PREFIX_BY_SOURCE_TYPE = {
        'manual': 'OO',
        'return': 'RO',
    }

    @classmethod
    def next_code(cls, date, source_type):
        """date × source_type 別に次の出荷指示番号を生成（{prefix}-YYYYMMDD-NNN）。

        - 通常出荷 (manual): OO-YYYYMMDD-NNN
        - 返品出荷 (return): RO-YYYYMMDD-NNN
        連番は種別ごとに独立してカウントする。
        """
        try:
            code_prefix = cls.CODE_PREFIX_BY_SOURCE_TYPE[source_type]
        except KeyError as e:
            raise ValueError(
                f'next_code は manual / return のみサポートします (got: {source_type})'
            ) from e
        prefix = f'{code_prefix}-{date.strftime("%Y%m%d")}-'
        max_n = 0
        for code in cls.objects.filter(
            outbound_order_code__startswith=prefix
        ).values_list('outbound_order_code', flat=True):
            m = re.match(rf'^{re.escape(prefix)}(\d{{3}})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'{prefix}{max_n + 1:03d}'


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
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name='ピッキング元ロケーション',
        help_text='出荷起動の在庫引き当てで確定する（登録時は NULL）。',
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
    # 出荷検品の per-item コミット用。inspected_at に値が入っているとその明細は
    # 検品完了済み（再入場時のフィルタに使う）。skip（picked=0）の明細も
    # inspected_at をセットすることで「処理済み」を記録する。
    inspected_at = models.DateTimeField('検品完了日時', null=True, blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='inspected_outbound_order_items',
        verbose_name='検品担当者',
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
        PENDING = 'pending', '未着手'
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

    @classmethod
    def next_code(cls, date):
        """次のピッキングリスト番号（PL-YYYYMMDD-NNN）を生成する。"""
        prefix = f'PL-{date.strftime("%Y%m%d")}-'
        max_n = 0
        for code in cls.objects.filter(
            picking_list_code__startswith=prefix
        ).values_list('picking_list_code', flat=True):
            m = re.match(rf'^{re.escape(prefix)}(\d{{3}})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'{prefix}{max_n + 1:03d}'


class PickingListItem(models.Model):
    """ピッキングリスト明細。"""

    class Status(models.TextChoices):
        PENDING = 'pending', '未着手'
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
        # READY（出荷準備完了）は MVP では未使用。出荷検品・梱包完了をもって出荷完了
        # (shipped) とみなす簡略化を採っており、トラック積込・出発（出荷バース管理）が
        # スコープ外のため。将来この工程を足すなら INSPECTING → READY → SHIPPED とする。
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
    in_progress_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='shipments_in_progress',
        verbose_name='作業担当者',
        help_text='検品・梱包の作業中の担当者。NULL=作業中でない（開始で設定、完了で解除）。',
    )
    in_progress_at = models.DateTimeField('作業開始日時', null=True, blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inspected_shipments',
        verbose_name='検品担当者',
        help_text='検品・梱包を完了した担当者の記録（完了時に設定）。',
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

    @classmethod
    def next_code(cls, date):
        """次の出荷番号（SH-YYYYMMDD-NNN）を生成する。"""
        prefix = f'SH-{date.strftime("%Y%m%d")}-'
        max_n = 0
        for code in cls.objects.filter(
            shipment_code__startswith=prefix
        ).values_list('shipment_code', flat=True):
            m = re.match(rf'^{re.escape(prefix)}(\d{{3}})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'{prefix}{max_n + 1:03d}'


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
    """出荷明細書（1出荷指示=1出荷明細書）。

    出荷検品完了時に実出荷数で発行する（出荷起動時点では予定数量しか分からず、
    ピッキング欠品などで実出荷数と異なりうるため）。発行した出荷明細書は箱に同梱して
    顧客へ届ける書類。出荷検品の開始バーコードはピッキングリストに印字した
    出荷指示番号を用いる（出荷明細書は検品完了後に発行されるため開始時には存在しない）。

    ※「対顧客の納品書（金額入り）」は OMS/基幹側の責務。本書は WMS が実出荷数を
    確定して発行する出荷明細（金額なし・梱包同梱用）。クラス名 DeliveryNote は
    内部の歴史的名称として残置（テーブル/コードは流用）。
    """

    delivery_note_code = models.CharField('出荷明細書番号', max_length=30, unique=True)
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
        verbose_name = '出荷明細書'
        verbose_name_plural = '出荷明細書'

    def __str__(self):
        return self.delivery_note_code

    @classmethod
    def next_code(cls, date):
        """次の出荷明細書番号（DN-YYYYMMDD-NNN）を生成する。"""
        prefix = f'DN-{date.strftime("%Y%m%d")}-'
        max_n = 0
        for code in cls.objects.filter(
            delivery_note_code__startswith=prefix
        ).values_list('delivery_note_code', flat=True):
            m = re.match(rf'^{re.escape(prefix)}(\d{{3}})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'{prefix}{max_n + 1:03d}'


class DeliveryNoteItem(models.Model):
    """出荷明細書の明細行。商品名・SKUコードはスナップショット保存。"""

    delivery_note = models.ForeignKey(
        DeliveryNote,
        on_delete=models.PROTECT,
        related_name='items',
        verbose_name='出荷明細書',
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
        verbose_name = '出荷明細書明細'
        verbose_name_plural = '出荷明細書明細'

    def __str__(self):
        return f'{self.delivery_note.delivery_note_code} / {self.sku_code}'
