"""出荷指示の CSV 定義（ヘッダ＋明細フラット様式）。

エンジンは core/order_csv.py。出荷指示番号は OO-YYYYMMDD-NNN（通常出荷）形式。
返品出荷（RO-）は画面と同じくスコープ外。倉庫は取込時の現在倉庫に固定する。
"""

import re

from core.order_csv import Field, FkField, OrderCsvSpec, RowError
from masters.models import Customer, Sku

from .models import OutboundOrder, OutboundOrderItem

_CODE_RE = re.compile(r'^OO-\d{8}-\d{3}$')


def _source_type(code):
    """出荷指示番号の書式を検証する（通常出荷 OO- のみ）。"""
    if not _CODE_RE.match(code):
        raise RowError(f'指示番号「{code}」は OO-YYYYMMDD-NNN 形式で入力してください。')
    return OutboundOrder.SourceType.MANUAL


OUTBOUND_ORDER_SPEC = OrderCsvSpec(
    key='outbound',
    label='出荷指示',
    order_model=OutboundOrder,
    item_model=OutboundOrderItem,
    code_field='outbound_order_code',
    item_order_fk='outbound_order',
    list_url='outbound:order_inquiry',
    export_url='outbound:order_csv_export',
    import_url='outbound:order_csv_import',
    code_to_source_type=_source_type,
    initial_status=OutboundOrder.Status.ALLOCATION_WAIT,
    header_fields=[
        Field('指示番号', 'outbound_order_code', note='OO-YYYYMMDD-NNN。明細をまとめるキー'),
        FkField(
            '顧客コード',
            'customer',
            Customer,
            'customer_code',
            required=True,
            active_only=True,
            note='顧客のコードで参照（必須）',
        ),
        Field('OMS注文番号', 'external_order_id'),
        Field('出荷期限', 'deadline_at', kind='datetime'),
        Field('配送先郵便番号', 'delivery_postal_code'),
        Field('配送先住所', 'delivery_address'),
        Field('配送先名称', 'delivery_name'),
        Field('備考', 'note'),
    ],
    item_fields=[
        FkField('SKUコード', 'sku', Sku, 'sku_code', active_only=True),
        Field('受注数', 'quantity_ordered', kind='int', required=True),
    ],
)
