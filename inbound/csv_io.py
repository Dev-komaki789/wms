"""入荷指示の CSV 定義（ヘッダ＋明細フラット様式）。

エンジンは core/order_csv.py。指示番号のプレフィックスで入荷元種別を判定する
（IO＝通常入荷 / RT＝返品入荷）。倉庫は取込時の現在倉庫に固定する。
"""

import re

from core.order_csv import Field, FkField, OrderCsvSpec, RowError
from masters.models import Sku, Supplier

from .models import InboundOrder, InboundOrderItem

_CODE_RE = re.compile(r'^(IO|RT)-\d{8}-\d{3}$')


def _source_type(code):
    """入荷指示番号のプレフィックスから入荷元種別を判定する。"""
    m = _CODE_RE.match(code)
    if not m:
        raise RowError(
            f'指示番号「{code}」は IO-YYYYMMDD-NNN（通常入荷）または '
            f'RT-YYYYMMDD-NNN（返品入荷）形式で入力してください。'
        )
    return InboundOrder.SourceType.MANUAL if m.group(1) == 'IO' else InboundOrder.SourceType.RETURN


def _check_manual_required(order):
    """通常入荷（IO-）は仕入先・入荷予定日が必須（フォームの clean と同じ規約）。"""
    if order.source_type == InboundOrder.SourceType.MANUAL:
        if order.supplier_id is None:
            return '通常入荷（IO-）では仕入先コードが必須です。'
        if order.expected_date is None:
            return '通常入荷（IO-）では入荷予定日が必須です。'
    return None


INBOUND_ORDER_SPEC = OrderCsvSpec(
    key='inbound',
    label='入荷指示',
    order_model=InboundOrder,
    item_model=InboundOrderItem,
    code_field='inbound_order_code',
    item_order_fk='inbound_order',
    list_url='inbound:order_inquiry',
    export_url='inbound:order_csv_export',
    import_url='inbound:order_csv_import',
    code_to_source_type=_source_type,
    initial_status=InboundOrder.Status.RECEIVING_WAIT,
    extra_order_checks=[_check_manual_required],
    header_fields=[
        Field(
            '指示番号',
            'inbound_order_code',
            note='IO-YYYYMMDD-NNN（通常）/ RT-YYYYMMDD-NNN（返品）。明細をまとめるキー',
        ),
        FkField(
            '仕入先コード',
            'supplier',
            Supplier,
            'supplier_code',
            required=False,
            active_only=True,
            note='仕入先のコードで参照。通常入荷（IO-）は必須 / 返品（RT-）は任意',
        ),
        Field('入荷予定日', 'expected_date', kind='date', note='YYYY-MM-DD。通常入荷（IO-）は必須'),
        Field('発注番号', 'purchase_order_code'),
        Field('納品書番号', 'supplier_delivery_note_code'),
        Field('備考', 'note'),
    ],
    item_fields=[
        FkField('SKUコード', 'sku', Sku, 'sku_code', active_only=True),
        Field('予定数', 'quantity_expected', kind='int', required=True),
        Field('クロスドック', 'is_crossdock', kind='bool', default=False),
    ],
)
