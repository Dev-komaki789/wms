"""入荷指示・出荷指示の CSV 入出力（ヘッダ＋明細のフラット様式）。

様式: 明細1行 ＝ CSV1行。指示ヘッダ項目（倉庫・仕入先など）は同じ指示の
各行で繰り返し、指示番号でグループ化して1指示にまとめる。

インポートは新規作成のみ（アップサートしない）。指示番号が既存と一致したら
エラー。全行を検証し、1行でもエラーがあれば一切確定しない（all-or-nothing）。

このモジュールは対象モデルに依存しない汎用エンジン。入荷/出荷それぞれの
列定義は inbound/csv_io.py・outbound/csv_io.py の OrderCsvSpec で与える。
画面は OrderCsvExportView / OrderCsvImportView を継承して spec を差すだけ。
"""
import csv
import datetime
import io
from dataclasses import dataclass, field

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.generic import TemplateView, View

from core.models import ErrorLog
from masters.utils import get_current_warehouse

# インポートで受け付ける文字コード（先頭から順に試す）
CSV_ENCODINGS = ('utf-8-sig', 'cp932')
# 区切り文字: フォーム/URL のキー → 実際の文字
DELIMITERS = {'comma': ',', 'caret': '^'}

_TRUE_WORDS = {'1', 'true', 'yes', 'y', 'on', 'はい', '有効', '○', '◯'}
_FALSE_WORDS = {'0', 'false', 'no', 'n', 'off', 'いいえ', '無効', '×', '✕'}


class RowError(Exception):
    """CSV の1行（または1指示）の取り込みエラー。line に該当行番号を持てる。"""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


# --- 列定義 -----------------------------------------------------------------

@dataclass
class Field:
    """単純列（文字列 / 数値 / 真偽 / 日付 / 日時）。"""
    header: str
    attr: str
    kind: str = 'str'      # 'str' | 'int' | 'bool' | 'date' | 'datetime'
    required: bool = False
    default: object = None
    note: str = ''         # 取込画面の説明（空なら kind から自動生成）


@dataclass
class FkField:
    """外部キー列。参照先を lookup フィールドのコードで引く。"""
    header: str
    attr: str
    model: object
    lookup: str
    required: bool = True
    active_only: bool = False   # is_active=True のものだけ参照可
    note: str = ''


@dataclass
class OrderCsvSpec:
    """1指示種（入荷指示 / 出荷指示）の CSV 定義。"""
    key: str                   # 'inbound' / 'outbound'
    label: str                 # '入荷指示' / '出荷指示'
    order_model: object
    item_model: object
    code_field: str            # 指示番号フィールド名
    item_order_fk: str         # 明細→指示の FK フィールド名
    list_url: str              # 一覧画面の URL 名
    export_url: str            # エクスポート URL 名
    import_url: str            # インポート URL 名
    header_fields: list        # 指示ヘッダの Field/FkField（先頭は指示番号）
    item_fields: list          # 明細の Field/FkField
    code_to_source_type: object  # callable(code)->source_type。書式不正なら RowError
    initial_status: object       # 新規作成時の status
    items_related: str = 'items'  # 指示→明細の逆アクセサ
    item_sku_attr: str = 'sku'    # 明細の SKU FK 属性（明細内重複検出に使う）
    extra_order_checks: list = field(default_factory=list)  # [(order)->str|None]

    @property
    def all_fields(self):
        return self.header_fields + self.item_fields

    @property
    def headers(self):
        return [f.header for f in self.all_fields]

    @property
    def code_header(self):
        return self.header_fields[0].header


@dataclass
class OrderImportReport:
    ok: bool
    total_rows: int = 0
    orders_created: int = 0
    items_created: int = 0
    errors: list = field(default_factory=list)   # [(line_no, message)]


# --- 値の変換 ---------------------------------------------------------------

def _to_bool(raw, fld):
    s = raw.strip().lower()
    if s == '':
        return bool(fld.default)
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    raise RowError(f'{fld.header}: 「{raw}」は はい/いいえ として解釈できません。')


def _to_int(raw, fld):
    s = raw.strip()
    if s == '':
        if fld.default is not None:
            return fld.default
        raise RowError(f'{fld.header}は必須です。')
    try:
        return int(s)
    except ValueError:
        raise RowError(f'{fld.header}: 「{raw}」は数値ではありません。')


def _to_date(raw, fld):
    s = raw.strip()
    if s == '':
        if fld.required:
            raise RowError(f'{fld.header}は必須です。')
        return None
    try:
        d = parse_date(s)
    except ValueError:
        d = None
    if d is None:
        raise RowError(f'{fld.header}: 「{raw}」は日付（YYYY-MM-DD）として解釈できません。')
    return d


def _to_datetime(raw, fld):
    s = raw.strip()
    if s == '':
        if fld.required:
            raise RowError(f'{fld.header}は必須です。')
        return None
    try:
        dt = parse_datetime(s)
    except ValueError:
        dt = None
    if dt is None:
        # 日付のみ（YYYY-MM-DD）も受け付け、時刻 00:00 とする
        try:
            d = parse_date(s)
        except ValueError:
            d = None
        if d is not None:
            dt = datetime.datetime(d.year, d.month, d.day)
    if dt is None:
        raise RowError(f'{fld.header}: 「{raw}」は日時として解釈できません。')
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _to_str(raw, fld):
    s = raw.strip()
    if fld.required and s == '':
        raise RowError(f'{fld.header}は必須です。')
    return s


_CONVERTERS = {
    'int': _to_int, 'bool': _to_bool, 'date': _to_date, 'datetime': _to_datetime,
}


def _convert(fld, raw):
    return _CONVERTERS.get(fld.kind, _to_str)(raw, fld)


def _resolve_fk(fld, raw):
    value = raw.strip()
    if not value:
        if fld.required:
            raise RowError(f'{fld.header}は必須です。')
        return None
    qs = fld.model.objects.filter(**{fld.lookup: value})
    if fld.active_only:
        qs = qs.filter(is_active=True)
    obj = qs.first()
    if obj is None:
        suffix = '（存在しないか無効化されています）' if fld.active_only else ''
        raise RowError(f'{fld.header}「{value}」が見つかりません{suffix}。')
    return obj


def _export_value(fld, obj):
    if isinstance(fld, FkField):
        rel = getattr(obj, fld.attr, None)
        return getattr(rel, fld.lookup) if rel is not None else ''
    value = getattr(obj, fld.attr)
    if value is None:
        return ''
    if fld.kind == 'bool':
        return 'はい' if value else 'いいえ'
    if fld.kind == 'date':
        return value.isoformat()
    if fld.kind == 'datetime':
        return timezone.localtime(value).strftime('%Y-%m-%d %H:%M')
    return str(value)


# --- エクスポート -----------------------------------------------------------

def build_order_csv(spec, delimiter=',', orders=None):
    """spec の指示を CSV（UTF-8 BOM付）の bytes で返す。

    `orders` に絞り込み済み queryset を渡すと、その範囲だけを出力する。
    省略時は全件出力（マスタ系の従来挙動）。select_related / prefetch_related と
    並び順は本関数内で付与するので、呼び出し側はフィルタだけ済ませて渡せばよい。
    """
    item_fk_attrs = [f.attr for f in spec.item_fields if isinstance(f, FkField)]
    header_fk_attrs = [f.attr for f in spec.header_fields if isinstance(f, FkField)]
    if orders is None:
        orders = spec.order_model.objects.all()
    if header_fk_attrs:
        orders = orders.select_related(*header_fk_attrs)
    item_qs = spec.item_model.objects.order_by('id')
    if item_fk_attrs:
        item_qs = item_qs.select_related(*item_fk_attrs)
    orders = orders.prefetch_related(
        Prefetch(spec.items_related, queryset=item_qs)
    ).order_by(spec.code_field)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(spec.headers)
    blank_item = ['' for _ in spec.item_fields]
    for order in orders:
        header_cells = [_export_value(f, order) for f in spec.header_fields]
        items = list(getattr(order, spec.items_related).all())
        if items:
            for item in items:
                writer.writerow(
                    header_cells + [_export_value(f, item) for f in spec.item_fields]
                )
        else:
            writer.writerow(header_cells + blank_item)
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
    """見出し行から区切り文字を推定する（見出し名は ',' '^' を含まないため確実）。"""
    first_line = next((ln for ln in text.splitlines() if ln.strip()), '')
    return '^' if '^' in first_line else ','


def _fmt_validation(spec, err):
    header_of = {f.attr: f.header for f in spec.all_fields}
    parts = []
    for fname, msgs in err.message_dict.items():
        joined = ' / '.join(msgs)
        label = header_of.get(fname, '' if fname == '__all__' else fname)
        parts.append(f'{label}: {joined}' if label else joined)
    return ' ; '.join(parts)


def _build_order(spec, code, group, warehouse, user):
    """同じ指示番号の行グループから (order, [items]) を組み立てて検証する。"""
    first_line, first_raw = group[0]

    # 新規作成のみ: 指示番号が既存と一致したらエラー
    if spec.order_model.objects.filter(**{spec.code_field: code}).exists():
        raise RowError(
            f'{spec.code_header}「{code}」は既に存在します（新規作成のみ・更新しません）。',
            line=first_line,
        )

    order = spec.order_model()
    source_type = spec.code_to_source_type(code)  # 書式不正なら RowError
    for fld in spec.header_fields:
        if fld.attr == spec.code_field:
            setattr(order, fld.attr, code)
            continue
        raw = first_raw.get(fld.header) or ''
        if isinstance(fld, FkField):
            setattr(order, fld.attr, _resolve_fk(fld, raw))
        else:
            setattr(order, fld.attr, _convert(fld, raw))
    order.warehouse = warehouse
    order.created_by = user
    order.status = spec.initial_status
    order.source_type = source_type

    # フラット様式: 同じ指示の各行はヘッダ項目が同じ値であること
    for line, raw in group[1:]:
        for fld in spec.header_fields:
            if fld.attr == spec.code_field:
                continue
            if (raw.get(fld.header) or '').strip() != (first_raw.get(fld.header) or '').strip():
                raise RowError(
                    f'指示「{code}」: {fld.header} が {first_line} 行目と一致しません。'
                    f'同じ指示の各行はヘッダ項目を同じ値にしてください。',
                    line=line,
                )

    try:
        order.full_clean()
    except ValidationError as e:
        raise RowError(_fmt_validation(spec, e), line=first_line)
    for check in spec.extra_order_checks:
        message = check(order)
        if message:
            raise RowError(message, line=first_line)

    # 明細
    items = []
    seen_sku = set()
    for line, raw in group:
        item = spec.item_model()
        for fld in spec.item_fields:
            raw_val = raw.get(fld.header) or ''
            try:
                if isinstance(fld, FkField):
                    setattr(item, fld.attr, _resolve_fk(fld, raw_val))
                else:
                    setattr(item, fld.attr, _convert(fld, raw_val))
            except RowError as e:
                raise RowError(str(e), line=line)
        sku = getattr(item, spec.item_sku_attr, None)
        if sku is not None:
            if sku.pk in seen_sku:
                raise RowError(
                    f'指示「{code}」内で SKU「{sku.sku_code}」が重複しています。', line=line)
            seen_sku.add(sku.pk)
        try:
            item.full_clean(exclude=[spec.item_order_fk])
        except ValidationError as e:
            raise RowError(_fmt_validation(spec, e), line=line)
        items.append(item)
    return order, items


def import_order_csv(spec, raw_bytes, *, warehouse, user, delimiter=None):
    """ヘッダ＋明細フラット CSV を取り込む。新規作成のみ・all-or-nothing。"""
    try:
        text = _decode(raw_bytes)
    except RowError as e:
        return OrderImportReport(ok=False, errors=[(0, str(e))])

    sep = delimiter or _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    got = {(h or '').strip() for h in (reader.fieldnames or [])}
    missing = [h for h in spec.headers if h not in got]
    if missing:
        return OrderImportReport(
            ok=False, errors=[(0, f'必要な列がありません: {"、".join(missing)}')])

    rows = []
    for i, raw in enumerate(reader):
        if any((v or '').strip() for v in raw.values()):
            rows.append((i + 2, raw))   # 1行目は見出し
    if not rows:
        return OrderImportReport(ok=False, errors=[(0, 'データ行がありません。')])
    if warehouse is None:
        return OrderImportReport(
            ok=False, errors=[(0, '現在の倉庫が特定できません。')])

    # 指示番号でグループ化（先頭出現順を保持）
    code_header = spec.code_header
    groups = {}
    errors = []
    for line, raw in rows:
        code = (raw.get(code_header) or '').strip()
        if not code:
            errors.append(
                (line, f'{code_header}は必須です（明細をまとめるキーになります）。'))
            continue
        groups.setdefault(code, []).append((line, raw))

    built = []
    for code, group in groups.items():
        try:
            built.append(_build_order(spec, code, group, warehouse, user))
        except RowError as e:
            errors.append((e.line or group[0][0], str(e)))

    if errors:
        errors.sort(key=lambda x: x[0])
        return OrderImportReport(ok=False, total_rows=len(rows), errors=errors)

    orders_created = items_created = 0
    with transaction.atomic():
        for order, items in built:
            order.save()
            for item in items:
                setattr(item, spec.item_order_fk, order)
                item.save()
            orders_created += 1
            items_created += len(items)
    return OrderImportReport(
        ok=True, total_rows=len(rows),
        orders_created=orders_created, items_created=items_created)


def order_column_guide(spec, fields):
    """取込画面に表示する列の説明（ヘッダ名 / 必須 / 補足）を組み立てる。"""
    guide = []
    for f in fields:
        is_code = (f.attr == spec.code_field)
        if f.note:
            note = f.note
        elif isinstance(f, FkField):
            note = f'{f.model._meta.verbose_name}のコードで参照'
        elif f.kind == 'bool':
            note = 'はい / いいえ（true/false・1/0 も可）'
        elif f.kind == 'date':
            note = '日付（YYYY-MM-DD）'
        elif f.kind == 'datetime':
            note = '日時（YYYY-MM-DD HH:MM、日付のみも可）'
        elif f.kind == 'int':
            note = '数値' + ('' if f.default is None else f'（空欄は {f.default}）')
        else:
            note = ''
        guide.append({
            'header': f.header,
            'required': True if is_code else f.required,
            'note': note,
        })
    return guide


# --- 画面（継承して spec を差す）-------------------------------------------

class OrderCsvExportView(LoginRequiredMixin, View):
    """指示を CSV（UTF-8 BOM付）でエクスポートする基底ビュー。

    サブクラスで `get_queryset(request)` を override すると、照会画面と同じ
    検索条件を反映した「絞り込みCSV」を出力できる（既定は全件）。
    """
    spec = None

    def get_queryset(self, request):
        """CSV出力対象の queryset を返す。既定は全件。サブクラスで絞り込みを適用する。"""
        return self.spec.order_model.objects.all()

    def get(self, request, *args, **kwargs):
        delimiter = DELIMITERS.get(request.GET.get('delimiter'), ',')
        orders = self.get_queryset(request)
        content = build_order_csv(self.spec, delimiter=delimiter, orders=orders)
        response = HttpResponse(content, content_type='text/csv')
        filename = f'{self.spec.key}_orders_{timezone.localdate():%Y%m%d}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class OrderCsvImportView(LoginRequiredMixin, TemplateView):
    """指示を CSV でインポート（新規作成のみ）する基底ビュー。"""
    spec = None
    template_name = 'a/order_csv_import.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['spec'] = self.spec
        ctx['header_guide'] = order_column_guide(self.spec, self.spec.header_fields)
        ctx['item_guide'] = order_column_guide(self.spec, self.spec.item_fields)
        ctx['delimiter'] = self.request.POST.get('delimiter') or 'auto'
        return ctx

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get('csv_file')
        if upload is None:
            messages.error(request, 'CSV ファイルを選択してください。')
            return self.render_to_response(self.get_context_data())

        delimiter = DELIMITERS.get(request.POST.get('delimiter'))  # auto → None
        report = import_order_csv(
            self.spec, upload.read(),
            warehouse=get_current_warehouse(request), user=request.user,
            delimiter=delimiter,
        )
        if report.ok:
            messages.success(
                request,
                f'{self.spec.label}を取り込みました'
                f'（指示 {report.orders_created} 件 / 明細 {report.items_created} 行）。',
            )
            return HttpResponseRedirect(reverse(self.spec.list_url))

        detail = '\n'.join(
            f'{line}行目: {msg}' if line else msg for line, msg in report.errors)
        ErrorLog.objects.create(
            error_type=ErrorLog.ErrorType.IMPORT,
            summary=(f'CSVインポート失敗: {self.spec.label} '
                     f'{len(report.errors)}件のエラー')[:255],
            detail=f'ファイル名: {upload.name}\n\n{detail}',
            source=f'{self.spec.label}CSVインポート',
            request_method='POST',
            user=request.user,
        )
        messages.error(
            request,
            f'取り込めませんでした。{len(report.errors)} 件のエラーがあります'
            f'（確定された変更はありません）。',
        )
        ctx = self.get_context_data()
        ctx['report'] = report
        return self.render_to_response(ctx)
