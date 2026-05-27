"""マスタ系 CSV 入出力。

エクスポート（全件 → CSV）とインポート（CSV → アップサート）の共通エンジンと、
8マスタ（エリア / ロケーション / カテゴリ / メーカー / 商品 / SKU / 仕入先 / 顧客）
の列定義（スペック）。画面側は masters/views.py の MasterCsvExportView /
MasterCsvImportView。

方針:
- エクスポート: 全件を UTF-8（BOM付）で出力（Excel で文字化けしない）。
- インポート: コードが既存なら更新・新規なら追加（アップサート）。文字コードは
  UTF-8 / Shift_JIS を自動判定。全行を検証し、1行でもエラーがあれば一切確定
  しない（all-or-nothing）。検証は各モデルの full_clean を使う。
"""
import csv
import io
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import (Area, Category, Customer, Location, Manufacturer,
                     Product, Sku, Supplier, Warehouse)

# インポートで受け付ける文字コード（先頭から順に試す）
CSV_ENCODINGS = ('utf-8-sig', 'cp932')

# 区切り文字: フォーム / URL のキー → 実際の文字
DELIMITERS = {'comma': ',', 'caret': '^'}

# 真偽値セルとして受け付ける表記（小文字で比較）
_TRUE_WORDS = {'1', 'true', 'yes', 'y', 'on', '有効', 'はい', '○', '◯'}
_FALSE_WORDS = {'0', 'false', 'no', 'n', 'off', '無効', 'いいえ', '×', '✕', '-'}


class RowError(Exception):
    """CSV 1行分の取り込みエラー。メッセージはそのまま画面に表示する。"""


# --- 列定義 -----------------------------------------------------------------

@dataclass
class Col:
    """単純列（文字列 / 数値 / 真偽 / 区分）。"""
    header: str            # CSV のヘッダ名
    field: str             # モデルのフィールド名
    kind: str = 'str'      # 'str' | 'int' | 'bool' | 'choice'
    required: bool = False  # インポート時、空セルを許さない
    choices: object = None  # kind='choice' のとき TextChoices/IntegerChoices クラス
    default: object = None  # 空セル時に使う既定値（kind=int/bool/choice 用）
    is_code: bool = False  # 自然キーのコード列
    true_label: str = 'はい'
    false_label: str = 'いいえ'
    transform: object = None  # 取り込み時に値へ適用する関数（例: str.upper）


@dataclass
class Fk:
    """外部キー列。参照先を lookup フィールドのコードで引く。"""
    header: str
    field: str             # instance に set する FK 属性名
    model: object          # 参照先モデル
    lookup: str            # 参照先の検索フィールド
    required: bool = True
    scope: str = None      # 同じ行で先に解決済みの FK 属性で絞り込む（例: area→warehouse）


@dataclass
class CsvSpec:
    """1マスタの CSV 定義。"""
    key: str               # URL スラッグ
    model: object
    label: str             # 画面名（例: 'メーカー'）
    list_url: str          # 一覧画面の URL 名
    code_field: str        # 自然キーのコードフィールド
    columns: list          # Col / Fk の並び（CSV の列順）
    warehouse_scoped: bool = False  # 自然キーが (warehouse, code) の複合キー
    auto_number: object = None      # (instance)->code。None なら手動コード必須
    extra_checks: list = field(default_factory=list)  # [(instance)->str|None]
    # 一覧/照会画面の検索条件を CSV にも反映するためのコールバック。
    # signature: (request, qs) -> qs。None なら絞り込みなし（全件出力）。
    list_filter: object = None

    @property
    def headers(self):
        return [c.header for c in self.columns]

    @property
    def code_col(self):
        return next(c for c in self.columns if isinstance(c, Col) and c.is_code)


@dataclass
class ImportReport:
    """インポート結果。ok=False のとき errors に (行番号, メッセージ) を持つ。"""
    ok: bool
    total: int = 0
    created: int = 0
    updated: int = 0
    errors: list = field(default_factory=list)


# --- 値の変換 ---------------------------------------------------------------

def _to_bool(raw, col):
    s = raw.strip().lower()
    if s == '':
        return col.default if col.default is not None else False
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    raise RowError(f'{col.header}: 「{raw}」は有効/無効として解釈できません。')


def _to_int(raw, col):
    s = raw.strip()
    if s == '':
        if col.default is not None:
            return col.default
        raise RowError(f'{col.header}は必須です。')
    try:
        return int(s)
    except ValueError:
        raise RowError(f'{col.header}: 「{raw}」は数値ではありません。')


def _to_choice(raw, col):
    s = raw.strip()
    if s == '':
        if col.default is not None:
            return col.default
        raise RowError(f'{col.header}は必須です。')
    for value, label in col.choices.choices:
        if s == str(value) or s == label:
            return value
    allowed = ' / '.join(label for _, label in col.choices.choices)
    raise RowError(f'{col.header}: 「{raw}」は不正な値です（{allowed}）。')


def _convert(col, raw):
    """Col の種別に応じて CSV セル文字列をモデル値へ変換する。"""
    if col.transform and raw:
        raw = col.transform(raw)
    if col.kind == 'bool':
        return _to_bool(raw, col)
    if col.kind == 'int':
        return _to_int(raw, col)
    if col.kind == 'choice':
        return _to_choice(raw, col)
    # str
    s = raw.strip()
    if col.required and s == '':
        raise RowError(f'{col.header}は必須です。')
    return s


def _export_value(col, obj):
    """モデルインスタンスから CSV セル文字列を取り出す。"""
    if isinstance(col, Fk):
        rel = getattr(obj, col.field, None)
        return getattr(rel, col.lookup) if rel is not None else ''
    value = getattr(obj, col.field)
    if col.kind == 'bool':
        return col.true_label if value else col.false_label
    if col.kind == 'choice':
        return getattr(obj, f'get_{col.field}_display')()
    if value is None:
        return ''
    return str(value)


# --- エクスポート -----------------------------------------------------------

def build_csv(spec, delimiter=',', qs=None):
    """spec のマスタを CSV（UTF-8 BOM付）の bytes で返す。

    `qs` に絞り込み済み queryset を渡すと、その範囲だけを出力する。
    省略時は全件出力。delimiter で区切り文字を選べる（',' または '^'）。
    """
    if qs is None:
        qs = spec.model.objects.all()
    fk_fields = [c.field for c in spec.columns if isinstance(c, Fk)]
    if fk_fields:
        qs = qs.select_related(*fk_fields)
    if spec.warehouse_scoped:
        qs = qs.order_by('warehouse__warehouse_code', spec.code_field)
    else:
        qs = qs.order_by(spec.code_field)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(spec.headers)
    for obj in qs:
        writer.writerow([_export_value(c, obj) for c in spec.columns])
    return buf.getvalue().encode('utf-8-sig')


# --- インポート -------------------------------------------------------------

def _decode(raw_bytes):
    for enc in CSV_ENCODINGS:
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    raise RowError('文字コードを判別できません。UTF-8 または Shift_JIS で保存してください。')


def _detect_delimiter(text):
    """見出し行から区切り文字を推定する。

    見出し名は固定の日本語で ',' も '^' も含まないため、先頭行に '^' が
    あればキャレット区切り、無ければカンマ区切りと確実に判定できる。
    """
    first_line = next((ln for ln in text.splitlines() if ln.strip()), '')
    return '^' if '^' in first_line else ','


def _fmt_validation(spec, err):
    """ValidationError をヘッダ名つきの読みやすい文字列にする。"""
    header_of = {c.field: c.header for c in spec.columns}
    parts = []
    for fname, msgs in err.message_dict.items():
        joined = ' / '.join(msgs)
        label = header_of.get(fname, '' if fname == '__all__' else fname)
        parts.append(f'{label}: {joined}' if label else joined)
    return ' ; '.join(parts)


def _process_row(spec, raw, seen):
    """CSV 1行を検証し (instance, is_new, auto_pending) を返す。

    auto_pending は「自動採番待ち（コード空欄の新規行）」。コードの確定は
    確定フェーズ（トランザクション内）で行う。
    """
    def cell(header):
        return (raw.get(header) or '').strip()

    # 1. FK を定義順に解決（warehouse → area の依存順になるよう列順を定義する）
    fk_vals = {}
    for c in spec.columns:
        if not isinstance(c, Fk):
            continue
        value = cell(c.header)
        if not value:
            if c.required:
                raise RowError(f'{c.header}は必須です。')
            fk_vals[c.field] = None
            continue
        lookup = {c.lookup: value}
        if c.scope:
            lookup[c.scope] = fk_vals.get(c.scope)
        obj = c.model.objects.filter(**lookup).first()
        if obj is None:
            raise RowError(f'{c.header}「{value}」が見つかりません。')
        fk_vals[c.field] = obj

    # 2. コードと既存レコードの判定
    code_col = spec.code_col
    code = cell(code_col.header)
    if code_col.transform and code:
        code = code_col.transform(code)
    auto_pending = False

    if spec.warehouse_scoped:
        if not code:
            raise RowError(f'{code_col.header}は必須です。')
        warehouse = fk_vals.get('warehouse')
        existing = spec.model.objects.filter(
            warehouse=warehouse, **{spec.code_field: code}).first()
        key = (warehouse.pk, code)
    elif not code:
        if spec.auto_number is None:
            raise RowError(f'{code_col.header}は必須です。')
        auto_pending = True
        existing = None
        key = None
    else:
        existing = spec.model.objects.filter(**{spec.code_field: code}).first()
        key = code

    # CSV ファイル内でのコード重複を検出
    if key is not None:
        if key in seen:
            raise RowError(f'{code_col.header}「{code}」が CSV 内で重複しています。')
        seen.add(key)

    is_new = existing is None
    instance = existing if existing is not None else spec.model()

    # 3. 値の設定（FK → コード → 単純列）
    for fname, obj in fk_vals.items():
        setattr(instance, fname, obj)
    if not auto_pending:
        setattr(instance, spec.code_field, code)
    for c in spec.columns:
        if isinstance(c, Col) and not c.is_code:
            setattr(instance, c.field, _convert(c, cell(c.header)))

    # 4. モデル検証（自動採番待ちのコード列は除外）
    exclude = [spec.code_field] if auto_pending else None
    instance.full_clean(exclude=exclude)

    # 5. マスタ固有の追加チェック
    for check in spec.extra_checks:
        message = check(instance)
        if message:
            raise RowError(message)

    return instance, is_new, auto_pending


def import_csv(spec, raw_bytes, delimiter=None):
    """CSV バイト列を取り込む。全行検証し、エラーが無ければ一括アップサートする。

    delimiter が None のときは見出し行から区切り文字（',' / '^'）を自動判定する。
    """
    try:
        text = _decode(raw_bytes)
    except RowError as e:
        return ImportReport(ok=False, errors=[(0, str(e))])

    sep = delimiter or _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    got = {(h or '').strip() for h in (reader.fieldnames or [])}
    missing = [h for h in spec.headers if h not in got]
    if missing:
        return ImportReport(
            ok=False,
            errors=[(0, f'必要な列がありません: {"、".join(missing)}')],
        )

    rows = [r for r in reader
            if any((v or '').strip() for v in r.values())]
    if not rows:
        return ImportReport(ok=False, errors=[(0, 'データ行がありません。')])

    outcomes = []
    errors = []
    seen = set()
    for offset, raw in enumerate(rows):
        line_no = offset + 2  # 1行目はヘッダ
        try:
            outcomes.append(_process_row(spec, raw, seen))
        except RowError as e:
            errors.append((line_no, str(e)))
        except ValidationError as e:
            errors.append((line_no, _fmt_validation(spec, e)))

    if errors:
        return ImportReport(ok=False, total=len(rows), errors=errors)

    created = updated = 0
    with transaction.atomic():
        for instance, is_new, auto_pending in outcomes:
            if auto_pending:
                setattr(instance, spec.code_field, spec.auto_number(instance))
            instance.save()
            if is_new:
                created += 1
            else:
                updated += 1
    return ImportReport(ok=True, total=len(rows), created=created, updated=updated)


def column_guide(spec):
    """インポート画面に表示する列の説明（ヘッダ / 必須 / 補足）を組み立てる。"""
    guide = []
    for c in spec.columns:
        if isinstance(c, Fk):
            note = f'{c.model._meta.verbose_name}のコードで参照（既存のみ）'
            required = c.required
        elif c.is_code:
            if spec.auto_number is not None:
                note = '空欄なら新規として自動採番。コード指定で既存を更新'
                required = False
            else:
                note = '自然キー。既存と一致すれば更新、無ければ新規'
                required = True
        elif c.kind == 'choice':
            note = '値: ' + ' / '.join(
                label for _, label in c.choices.choices)
            required = c.required
        elif c.kind == 'bool':
            note = '有効 / 無効（true/false・1/0 も可）'
            required = False
        elif c.kind == 'int':
            note = '数値' + ('' if c.default is None else f'（空欄は {c.default}）')
            required = c.required
        else:
            note = ''
            required = c.required
        guide.append({'header': c.header, 'required': required, 'note': note})
    return guide


# --- 8マスタの定義 ----------------------------------------------------------

def _active_col():
    return Col('有効', 'is_active', kind='bool', default=True,
               true_label='有効', false_label='無効')


def _check_area_code(instance):
    if not re.fullmatch(r'[A-Z]', instance.area_code or ''):
        return (f'エリアコードは英大文字1文字（A〜Z）で入力してください'
                f'（「{instance.area_code}」）。')
    return None


def _check_category_depth(instance):
    parent = instance.parent
    if parent is not None and parent.depth + 1 >= Category.MAX_DEPTH:
        return f'カテゴリの階層が深すぎます（最大 {Category.MAX_DEPTH} 階層）。'
    if instance.is_leaf and instance.pk and instance.children.exists():
        return '子カテゴリを持つため「商品の登録先」にはできません。'
    return None


# --- 一覧/照会画面の検索条件を CSV にも反映するフィルタ関数 ---
# 各 list / inquiry ビューと同じキー・同じ意味で絞り込む（WYSIWYG）。
# 一覧画面に絞り込みUIが無いマスタ（Area / Location）は list_filter 未設定＝全件。


def _apply_q_status(qs, request, code_field, name_field, extra_q_fields=None):
    """共通: キーワード(q) と 有効/無効(status) で絞る。extra_q_fields は追加の検索対象。"""
    g = request.GET
    q = g.get('q', '').strip()
    if q:
        cond = Q(**{f'{code_field}__icontains': q}) | Q(**{f'{name_field}__icontains': q})
        for f in (extra_q_fields or []):
            cond |= Q(**{f'{f}__icontains': q})
        qs = qs.filter(cond)
    status = g.get('status', '')
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    return qs


def _filter_manufacturer(request, qs):
    return _apply_q_status(qs, request, 'manufacturer_code', 'manufacturer_name')


def _filter_supplier(request, qs):
    return _apply_q_status(qs, request, 'supplier_code', 'supplier_name',
                           extra_q_fields=['contact_person'])


def _filter_customer(request, qs):
    qs = _apply_q_status(qs, request, 'customer_code', 'customer_name')
    ctype = request.GET.get('customer_type', '')
    if ctype.isdigit():
        qs = qs.filter(customer_type=int(ctype))
    return qs


def _filter_category(request, qs):
    # CategoryListView と同じ: q（コード/名称）+ 有効/無効
    return _apply_q_status(qs, request, 'category_code', 'category_name')


def _filter_product(request, qs):
    # ProductInquiryView と同じ: p_q / p_category / p_manufacturer / p_status
    g = request.GET
    p_q = g.get('p_q', '').strip()
    if p_q:
        qs = qs.filter(Q(product_code__icontains=p_q) | Q(product_name__icontains=p_q))
    p_category = g.get('p_category', '')
    if p_category:
        qs = qs.filter(category_id=p_category)
    p_manufacturer = g.get('p_manufacturer', '').strip()
    if p_manufacturer:
        qs = qs.filter(
            Q(manufacturer__manufacturer_name__icontains=p_manufacturer)
            | Q(manufacturer__manufacturer_code__icontains=p_manufacturer)
        )
    p_status = g.get('p_status', '')
    if p_status == 'active':
        qs = qs.filter(is_active=True)
    elif p_status == 'inactive':
        qs = qs.filter(is_active=False)
    return qs


def _filter_location(request, qs):
    """MasterInquiryView（エリア・ロケーション照会）の検索条件を反映する。

    LocationListView 側はフィルタUIを持たないが、エリア・ロケーション照会では
    loc_* プレフィックスで絞り込みが行えるので、その条件を CSV にも反映する。
    """
    g = request.GET
    loc_q = g.get('loc_q', '').strip()
    if loc_q:
        qs = qs.filter(location_code__icontains=loc_q)
    loc_warehouse = g.get('loc_warehouse', '')
    if loc_warehouse:
        qs = qs.filter(warehouse_id=loc_warehouse)
    loc_area = g.get('loc_area', '')
    if loc_area:
        qs = qs.filter(area_id=loc_area)
    loc_type = g.get('loc_type', '')
    if loc_type:
        qs = qs.filter(area__location_type=loc_type)
    loc_status = g.get('loc_status', '')
    if loc_status == 'active':
        qs = qs.filter(is_active=True)
    elif loc_status == 'inactive':
        qs = qs.filter(is_active=False)
    return qs


def _filter_sku(request, qs):
    # SkuInquiryView と同じ: s_q / s_product / s_picking / s_status
    g = request.GET
    s_q = g.get('s_q', '').strip()
    if s_q:
        qs = qs.filter(Q(sku_code__icontains=s_q) | Q(jan_code__icontains=s_q))
    s_product = g.get('s_product', '').strip()
    if s_product:
        qs = qs.filter(
            Q(product__product_name__icontains=s_product)
            | Q(product__product_code__icontains=s_product)
        )
    s_picking = g.get('s_picking', '')
    if s_picking:
        qs = qs.filter(picking_type=s_picking)
    s_status = g.get('s_status', '')
    if s_status == 'active':
        qs = qs.filter(is_active=True)
    elif s_status == 'inactive':
        qs = qs.filter(is_active=False)
    return qs


_SPECS = [
    CsvSpec(
        key='area', model=Area, label='エリア', list_url='masters:area_list',
        code_field='area_code', warehouse_scoped=True,
        extra_checks=[_check_area_code],
        columns=[
            Fk('倉庫コード', 'warehouse', Warehouse, 'warehouse_code'),
            Col('エリアコード', 'area_code', is_code=True, transform=str.upper),
            Col('エリア名', 'area_name'),
            Col('区分', 'location_type', kind='choice', choices=Area.LocationType,
                default=Area.LocationType.STORAGE),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='location', model=Location, label='ロケーション',
        list_url='masters:location_list',
        code_field='location_code', warehouse_scoped=True,
        list_filter=_filter_location,
        columns=[
            Fk('倉庫コード', 'warehouse', Warehouse, 'warehouse_code'),
            Fk('エリアコード', 'area', Area, 'area_code', scope='warehouse'),
            Col('棚番コード', 'location_code', is_code=True),
            Col('表示名', 'location_name'),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='category', model=Category, label='カテゴリ',
        list_url='masters:category_list',
        code_field='category_code',
        auto_number=lambda inst: (
            Category.next_child_code(inst.parent) if inst.parent_id
            else Category.next_root_code()),
        extra_checks=[_check_category_depth],
        list_filter=_filter_category,
        columns=[
            Col('カテゴリコード', 'category_code', is_code=True),
            Col('カテゴリ名', 'category_name', required=True),
            Fk('親カテゴリコード', 'parent', Category, 'category_code',
               required=False),
            Col('表示順', 'sort_order', kind='int', default=10),
            Col('商品の登録先', 'is_leaf', kind='bool', default=False),
            Col('説明', 'description'),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='manufacturer', model=Manufacturer, label='メーカー',
        list_url='masters:manufacturer_list',
        code_field='manufacturer_code',
        list_filter=_filter_manufacturer,
        columns=[
            Col('メーカーコード', 'manufacturer_code', is_code=True),
            Col('メーカー名', 'manufacturer_name', required=True),
            Col('URL', 'url'),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='product', model=Product, label='商品',
        list_url='masters:product_inquiry',
        code_field='product_code',
        auto_number=lambda inst: Product.next_product_code(),
        list_filter=_filter_product,
        columns=[
            Col('商品コード', 'product_code', is_code=True),
            Col('商品名', 'product_name', required=True),
            Fk('カテゴリコード', 'category', Category, 'category_code'),
            Fk('メーカーコード', 'manufacturer', Manufacturer,
               'manufacturer_code', required=False),
            Col('説明', 'description'),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='sku', model=Sku, label='SKU', list_url='masters:sku_inquiry',
        code_field='sku_code',
        auto_number=lambda inst: Sku.next_sku_code(),
        list_filter=_filter_sku,
        columns=[
            Col('SKUコード', 'sku_code', is_code=True),
            Fk('商品コード', 'product', Product, 'product_code'),
            Col('JANコード', 'jan_code'),
            Col('サイズ', 'size_info'),
            Col('カラー', 'color_info'),
            Col('入数（ケース）', 'quantity_per_unit', kind='int', default=1),
            Col('ピッキング種別', 'picking_type', kind='choice',
                choices=Sku.PickingType, default=Sku.PickingType.TOTAL),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='supplier', model=Supplier, label='仕入先',
        list_url='masters:supplier_list',
        code_field='supplier_code',
        list_filter=_filter_supplier,
        columns=[
            Col('仕入先コード', 'supplier_code', is_code=True),
            Col('仕入先名', 'supplier_name', required=True),
            Col('担当者', 'contact_person'),
            Col('電話番号', 'phone_number'),
            Col('メールアドレス', 'email'),
            Col('郵便番号', 'postal_code'),
            Col('住所', 'address'),
            _active_col(),
        ],
    ),
    CsvSpec(
        key='customer', model=Customer, label='顧客',
        list_url='masters:customer_list',
        code_field='customer_code',
        list_filter=_filter_customer,
        columns=[
            Col('顧客コード', 'customer_code', is_code=True),
            Col('顧客名', 'customer_name', required=True),
            Col('顧客種別', 'customer_type', kind='choice', choices=Customer.Type,
                default=Customer.Type.CORPORATE),
            Col('業種', 'industry_type'),
            Col('郵便番号', 'postal_code'),
            Col('住所', 'address'),
            _active_col(),
        ],
    ),
]

MASTER_SPECS = {spec.key: spec for spec in _SPECS}
