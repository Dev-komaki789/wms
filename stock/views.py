from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import TemplateView, View
from django.views.generic.edit import FormView

from core.utils import parse_query_date
from masters.models import Area, Location, Sku
from masters.utils import get_current_warehouse
from outbound.models import StockReservation

from .forms import StockTransferForm, UnplannedStockInForm, UnplannedStockOutForm
from .models import StockBalance, StockMovement, StockTransfer


class StockInquiryView(LoginRequiredMixin, TemplateView):
    """在庫照会画面。

    StockBalance を SKU × ロケーション粒度で表示する read-only 画面。
    検索-first パターン（マスタ照会と同じ規約）。
    在庫数の編集は入出庫機能経由でのみ行う想定で、ここでは表示のみ。
    """

    template_name = 'a/stock/inquiry.html'

    SEARCH_KEYS = ('q', 'product', 'location', 'area_type', 'state')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        f = {
            'q': g.get('q', '').strip(),
            'product': g.get('product', '').strip(),
            'location': g.get('location', '').strip(),
            'area_type': g.get('area_type', ''),
            'state': g.get('state', ''),
        }

        if searched:
            qs = StockBalance.objects.select_related(
                'location', 'location__area', 'location__warehouse',
                'sku', 'sku__product', 'sku__product__category',
            )
            if f['q']:
                qs = qs.filter(
                    Q(sku__sku_code__icontains=f['q'])
                    | Q(sku__jan_code__icontains=f['q'])
                )
            if f['product']:
                qs = qs.filter(
                    Q(sku__product__product_name__icontains=f['product'])
                    | Q(sku__product__product_code__icontains=f['product'])
                )
            if f['location']:
                qs = qs.filter(location__location_code__icontains=f['location'])
            if f['area_type']:
                qs = qs.filter(location__area__location_type=f['area_type'])
            if f['state'] == 'in_stock':
                qs = qs.filter(quantity__gt=0)
            elif f['state'] == 'zero':
                qs = qs.filter(quantity=0)
            qs = qs.order_by('location__location_code', 'sku__sku_code')
            # 引き当て数 = この (ロケーション × SKU) に紐づく active な
            # StockReservation の合計。出荷可能数 = 在庫数 − 引き当て数。
            reserved_subquery = (
                StockReservation.objects
                .filter(
                    location=OuterRef('location'),
                    sku=OuterRef('sku'),
                    status=StockReservation.Status.ACTIVE,
                )
                .values('location', 'sku')
                .annotate(total=Sum('quantity'))
                .values('total')
            )
            qs = qs.annotate(
                reserved=Coalesce(
                    Subquery(reserved_subquery, output_field=IntegerField()), 0
                ),
            ).annotate(
                available=F('quantity') - F('reserved'),
            )
            balances = list(qs)
            ctx['balances'] = balances
            ctx['stats'] = {
                'rows': len(balances),
                'total_qty': sum(b.quantity for b in balances),
                'total_reserved': sum(b.reserved for b in balances),
                'total_available': sum(b.available for b in balances),
                'sku_count': len({b.sku_id for b in balances}),
                'in_stock': sum(1 for b in balances if b.quantity > 0),
                'zero': sum(1 for b in balances if b.quantity == 0),
            }
        else:
            ctx['balances'] = StockBalance.objects.none()
            ctx['stats'] = None

        ctx['area_types'] = Area.LocationType.choices
        ctx['filters'] = f
        return ctx


class StockMovementInquiryView(LoginRequiredMixin, TemplateView):
    """入出庫履歴照会画面。

    StockMovement（在庫移動の追記専用ログ）を時系列で照会する read-only 画面。
    検索-first パターン（マスタ照会・在庫照会と同じ規約）。入庫 / 出庫 /
    棚卸調整を、SKU・棚番・種別・伝票種別・期間で絞り込む。

    StockMovement は追記専用で件数が無制限に増えるため、テーブルは新しい順
    ROW_LIMIT 件で打ち切る（超過時は画面で警告し、期間での絞り込みを促す）。
    サマリーは打ち切り前の全件を DB 集計するので、件数・数量は常に正確。
    """

    template_name = 'a/stock/movement_inquiry.html'

    SEARCH_KEYS = ('q', 'product', 'location', 'movement_type',
                   'reference_type', 'date_from', 'date_to')
    ROW_LIMIT = 1000

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        MT = StockMovement.MovementType

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        f = {
            'q': g.get('q', '').strip(),
            'product': g.get('product', '').strip(),
            'location': g.get('location', '').strip(),
            'movement_type': g.get('movement_type', ''),
            'reference_type': g.get('reference_type', ''),
            'date_from': g.get('date_from', ''),
            'date_to': g.get('date_to', ''),
        }

        if searched:
            qs = StockMovement.objects.select_related(
                'location', 'location__area',
                'sku', 'sku__product', 'created_by',
            )
            if f['q']:
                qs = qs.filter(
                    Q(sku__sku_code__icontains=f['q'])
                    | Q(sku__jan_code__icontains=f['q'])
                )
            if f['product']:
                qs = qs.filter(
                    Q(sku__product__product_name__icontains=f['product'])
                    | Q(sku__product__product_code__icontains=f['product'])
                )
            if f['location']:
                qs = qs.filter(location__location_code__icontains=f['location'])
            if f['movement_type']:
                qs = qs.filter(movement_type=f['movement_type'])
            if f['reference_type']:
                qs = qs.filter(reference_type=f['reference_type'])
            date_from = parse_query_date(f['date_from'])
            date_to = parse_query_date(f['date_to'])
            if date_from:
                qs = qs.filter(moved_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(moved_at__date__lte=date_to)
            qs = qs.order_by('-moved_at', '-id')

            # サマリーは打ち切り前の全件を集計（件数・数量を正確に出す）。
            agg = qs.aggregate(
                total=Count('id'),
                in_count=Count('id', filter=Q(movement_type=MT.IN)),
                out_count=Count('id', filter=Q(movement_type=MT.OUT)),
                adj_count=Count('id', filter=Q(movement_type=MT.ADJ)),
                in_qty=Sum('quantity', filter=Q(movement_type=MT.IN)),
                out_qty=Sum('quantity', filter=Q(movement_type=MT.OUT)),
            )
            total = agg['total']
            movements = list(qs[:self.ROW_LIMIT])
            ctx['movements'] = movements
            ctx['truncated'] = total > self.ROW_LIMIT
            ctx['stats'] = {
                'total': total,
                'shown': len(movements),
                'in_count': agg['in_count'],
                'out_count': agg['out_count'],
                'adj_count': agg['adj_count'],
                # OUT の quantity は負値で記録されるため符号反転して正の数で表示。
                'in_qty': agg['in_qty'] or 0,
                'out_qty': -(agg['out_qty'] or 0),
            }
        else:
            ctx['movements'] = StockMovement.objects.none()
            ctx['stats'] = None
            ctx['truncated'] = False

        ctx['movement_types'] = MT.choices
        ctx['reference_types'] = StockMovement.ReferenceType.choices
        ctx['row_limit'] = self.ROW_LIMIT
        ctx['filters'] = f
        return ctx


class UnplannedStockInView(LoginRequiredMixin, FormView):
    """計画外入庫画面（handheld 端末用）。

    InboundOrder を介さず StockMovement.IN を直接発行し、StockBalance を加算する。
    入庫成功後は同じ URL に redirect → メッセージ表示 + フォーム初期化で連続入庫に対応。
    """

    template_name = 'a/handheld/stock_in.html'
    form_class = UnplannedStockInForm
    success_url = reverse_lazy('stock:handheld_in')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request'] = self.request  # form 側で現在倉庫スコープを判定するため
        return kwargs

    def form_valid(self, form):
        location = form._location
        sku = form._sku
        quantity = form.cleaned_data['quantity']
        note = form.cleaned_data.get('note', '')

        with transaction.atomic():
            # 在庫行をロックして取得（同時入庫での更新消失=lost update を防ぐ）。
            # 行が無ければ作成し、ロック付きで取り直す。
            StockBalance.objects.get_or_create(
                location=location, sku=sku,
                defaults={'quantity': 0},
            )
            balance = (
                StockBalance.objects.select_for_update()
                .get(location=location, sku=sku)
            )
            quantity_before = balance.quantity
            quantity_after = quantity_before + quantity

            StockMovement.objects.create(
                movement_type=StockMovement.MovementType.IN,
                location=location,
                sku=sku,
                quantity=quantity,  # IN なので正の値
                quantity_before=quantity_before,
                quantity_after=quantity_after,
                reference_type=StockMovement.ReferenceType.MANUAL_IN,
                reference_id=None,
                note=note,
                created_by=self.request.user,
            )
            balance.quantity = quantity_after
            balance.save()

        messages.success(
            self.request,
            f'入庫完了: {sku.sku_code} を {location.location_code} に {quantity} 個 '
            f'(在庫 {quantity_before} → {quantity_after})',
        )
        return super().form_valid(form)


class UnplannedStockOutView(LoginRequiredMixin, FormView):
    """計画外出庫画面（handheld 端末用）。

    OutboundOrder を介さず StockMovement.OUT を直接発行し、StockBalance を減算する。
    出庫成功後は同じ URL に redirect → メッセージ表示 + フォーム初期化で連続出庫に対応。
    """

    template_name = 'a/handheld/stock_out.html'
    form_class = UnplannedStockOutForm
    success_url = reverse_lazy('stock:handheld_out')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request'] = self.request  # form 側で現在倉庫スコープを判定するため
        return kwargs

    def form_valid(self, form):
        location = form._location
        sku = form._sku
        quantity = form.cleaned_data['quantity']
        note = form.cleaned_data.get('note', '')

        with transaction.atomic():
            # 出庫対象の在庫行を行ロックして取得する。
            # form.clean() で在庫数は検証済みだが、検証〜確定の間に他端末が
            # 在庫を減らす可能性に備え、ロック確保後にもう一度残数を確認する。
            balance = (
                StockBalance.objects.select_for_update()
                .filter(location=location, sku=sku)
                .first()
            )
            on_hand = balance.quantity if balance else 0
            if quantity > on_hand:
                form.add_error(
                    'quantity',
                    f'在庫不足です。{location.location_code} の在庫は {on_hand} 個です。',
                )
                return self.form_invalid(form)

            quantity_before = on_hand
            quantity_after = on_hand - quantity

            StockMovement.objects.create(
                movement_type=StockMovement.MovementType.OUT,
                location=location,
                sku=sku,
                quantity=-quantity,  # OUT なので負の値で記録
                quantity_before=quantity_before,
                quantity_after=quantity_after,
                reference_type=StockMovement.ReferenceType.MANUAL_OUT,
                reference_id=None,
                note=note,
                created_by=self.request.user,
            )
            balance.quantity = quantity_after
            balance.save()

        messages.success(
            self.request,
            f'出庫完了: {sku.sku_code} を {location.location_code} から {quantity} 個 '
            f'(在庫 {quantity_before} → {quantity_after})',
        )
        return super().form_valid(form)


class StockTransferView(LoginRequiredMixin, FormView):
    """棚間移動画面（handheld 端末用）。

    移動元棚番・SKU・数量・移動先棚番を入力し、ロケーション間で在庫を移す。
    実行時に StockTransfer を1件、移動元 OUT・移動先 IN の StockMovement を2本
    （reference_type=stock_transfer）発行し、両ロケーションの StockBalance を増減
    する。移動成功後は同じ URL に redirect → メッセージ表示 + フォーム初期化で
    連続作業に対応。
    """

    template_name = 'a/handheld/stock_transfer.html'
    form_class = StockTransferForm
    success_url = reverse_lazy('stock:handheld_transfer')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request'] = self.request  # form 側で現在倉庫スコープを判定するため
        return kwargs

    def form_valid(self, form):
        from_loc = form._from_location
        to_loc = form._to_location
        sku = form._sku
        quantity = form.cleaned_data['quantity']

        with transaction.atomic():
            # 移動元・移動先の在庫行を location_id 昇順でロック（既存行のみ。
            # 同時実行の相互移動でのデッドロックを避けるため一定順で取得する）。
            list(
                StockBalance.objects.select_for_update()
                .filter(sku=sku, location__in=[from_loc, to_loc])
                .order_by('location_id')
            )
            from_balance = (
                StockBalance.objects
                .filter(location=from_loc, sku=sku).first()
            )
            from_on_hand = from_balance.quantity if from_balance else 0
            # form.clean() で検証済みだが、検証〜確定の間に在庫が減る可能性に
            # 備え、ロック確保後にもう一度残数を確認する。
            if quantity > from_on_hand:
                form.add_error(
                    'quantity',
                    f'在庫不足です。{from_loc.location_code} の '
                    f'{sku.sku_code} 在庫は {from_on_hand} 個です。',
                )
                return self.form_invalid(form)
            # 移動先の在庫行（無ければ 0 で作成。新規行はこのトランザクション専有）
            to_balance, _ = StockBalance.objects.get_or_create(
                location=to_loc, sku=sku, defaults={'quantity': 0},
            )

            now = timezone.now()
            transfer = StockTransfer.objects.create(
                from_location=from_loc, to_location=to_loc, sku=sku,
                quantity=quantity, status=StockTransfer.Status.COMPLETED,
                transferred_at=now, created_by=self.request.user,
            )
            # 移動元から OUT
            StockMovement.objects.create(
                movement_type=StockMovement.MovementType.OUT,
                location=from_loc, sku=sku,
                quantity=-quantity,  # OUT なので負の値で記録
                quantity_before=from_on_hand,
                quantity_after=from_on_hand - quantity,
                reference_type=StockMovement.ReferenceType.STOCK_TRANSFER,
                reference_id=transfer.pk,
                note='', created_by=self.request.user,
            )
            from_balance.quantity = from_on_hand - quantity
            from_balance.save()
            # 移動先へ IN
            to_before = to_balance.quantity
            StockMovement.objects.create(
                movement_type=StockMovement.MovementType.IN,
                location=to_loc, sku=sku,
                quantity=quantity,  # IN なので正の値
                quantity_before=to_before,
                quantity_after=to_before + quantity,
                reference_type=StockMovement.ReferenceType.STOCK_TRANSFER,
                reference_id=transfer.pk,
                note='', created_by=self.request.user,
            )
            to_balance.quantity = to_before + quantity
            to_balance.save()

        messages.success(
            self.request,
            f'棚間移動完了: {sku.sku_code} を {from_loc.location_code} '
            f'→ {to_loc.location_code} に {quantity} 個',
        )
        return super().form_valid(form)


class StockCheckAPIView(LoginRequiredMixin, View):
    """ロケーション × SKU の在庫紐づきチェック API（AJAX 用）。

    棚間移動などで、スキャンした SKU が移動元の棚に在庫として実在するかを
    その場で検証するためのエンドポイント。SKU の実在・ロケーションの実在
    （現在倉庫スコープ）・その組み合わせの在庫数（StockBalance.quantity）を返す。
    棚に在庫が無い（on_hand=0）場合、呼び出し側でエラー表示する。
    """

    def get(self, request):
        loc_code = (request.GET.get('location') or '').strip()
        sku_code = (request.GET.get('sku') or '').strip()

        sku = None
        if sku_code:
            sku = (
                Sku.objects.select_related('product')
                .filter(sku_code=sku_code, is_active=True)
                .first()
            )

        location = None
        if loc_code:
            loc_qs = Location.objects.filter(
                location_code=loc_code, is_active=True)
            wh = get_current_warehouse(request)
            if wh is not None:
                loc_qs = loc_qs.filter(warehouse=wh)
            location = loc_qs.first()

        on_hand = 0
        if sku is not None and location is not None:
            balance = (
                StockBalance.objects
                .filter(location=location, sku=sku)
                .first()
            )
            on_hand = balance.quantity if balance else 0

        return JsonResponse({
            'sku_found': sku is not None,
            'location_found': location is not None,
            'on_hand': on_hand,
            'sku_code': sku.sku_code if sku else sku_code,
            'product_name': sku.product.product_name if sku else '',
            'location_code': location.location_code if location else loc_code,
        })
