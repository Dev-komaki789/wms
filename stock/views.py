from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, TemplateView, View
from django.views.generic.edit import FormView

from core.utils import apply_ordering, paginate, parse_query_date
from inbound.models import InboundOrder
from masters.models import Area, Location, Sku
from masters.utils import get_current_warehouse
from outbound.models import OutboundOrder, StockReservation

from .forms import (
    StockTransferForm,
    StocktakeCountForm,
    StocktakeSessionForm,
    UnplannedStockInForm,
    UnplannedStockOutForm,
)
from .models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StocktakeAdjustment,
    StocktakeItem,
    StocktakeSession,
)


class StockInquiryView(LoginRequiredMixin, TemplateView):
    """在庫照会画面。

    StockBalance を SKU × ロケーション粒度で表示する read-only 画面。
    検索-first パターン（マスタ照会と同じ規約）。
    在庫数の編集は入出庫機能経由でのみ行う想定で、ここでは表示のみ。
    """

    template_name = 'a/stock/inquiry.html'

    SEARCH_KEYS = ('q', 'product', 'location', 'area_type', 'state')

    SORTABLE = {
        'location': ['location__location_code'],
        'area_type': ['location__area__location_type'],
        'sku': ['sku__sku_code'],
        'product': ['sku__product__product_name'],
        'jan': ['sku__jan_code'],
        'size': ['sku__size_info'],
        'qty': ['quantity'],
        'reserved': ['reserved'],
        'available': ['available'],
        'updated': ['updated_at'],
    }

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
                'location',
                'location__area',
                'location__warehouse',
                'sku',
                'sku__product',
                'sku__product__category',
            )
            if f['q']:
                qs = qs.filter(
                    Q(sku__sku_code__icontains=f['q']) | Q(sku__jan_code__icontains=f['q'])
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
            # 引き当て数 = この (ロケーション × SKU) に紐づく active な
            # StockReservation の合計。出荷可能数 = 在庫数 − 引き当て数。
            reserved_subquery = (
                StockReservation.objects.filter(
                    location=OuterRef('location'),
                    sku=OuterRef('sku'),
                    status=StockReservation.Status.ACTIVE,
                )
                .values('location', 'sku')
                .annotate(total=Sum('quantity'))
                .values('total')
            )
            qs = qs.annotate(
                reserved=Coalesce(Subquery(reserved_subquery, output_field=IntegerField()), 0),
            ).annotate(
                available=F('quantity') - F('reserved'),
            )
            qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, qs, self.SORTABLE, 'location', 'asc'
            )
            # サマリーは検索結果の全件を集計（ページ送りしても合計は全体ベース）
            agg = qs.aggregate(
                rows=Count('id'),
                total_qty=Sum('quantity'),
                total_reserved=Sum('reserved'),
                sku_count=Count('sku', distinct=True),
                in_stock=Count('id', filter=Q(quantity__gt=0)),
                zero=Count('id', filter=Q(quantity=0)),
            )
            page = paginate(self.request, qs)
            ctx['balances'] = page
            ctx['page_obj'] = page
            total_qty = agg['total_qty'] or 0
            total_reserved = agg['total_reserved'] or 0
            ctx['stats'] = {
                'rows': agg['rows'],
                'total_qty': total_qty,
                'total_reserved': total_reserved,
                'total_available': total_qty - total_reserved,
                'sku_count': agg['sku_count'],
                'in_stock': agg['in_stock'],
                'zero': agg['zero'],
            }
        else:
            ctx['balances'] = StockBalance.objects.none()
            ctx['stats'] = None

        ctx['area_types'] = Area.LocationType.choices
        ctx['filters'] = f
        return ctx


def _attach_reference_codes(movements):
    """入出庫履歴の各行に作業内容（reference_type）の伝票コードを付与する。

    入荷指示/出荷指示は対応する文書コード（IO-…/OO-…）をセット。棚間移動・
    棚卸・計画外入出庫には対応する伝票が無いので None のまま（画面ではコード
    部分が出ない）。現在ページ分のみ pk__in で一括取得するので N+1 にならない。
    """
    RT = StockMovement.ReferenceType
    ib_ids, ob_ids = [], []
    for m in movements:
        if not m.reference_id:
            continue
        if m.reference_type == RT.INBOUND_ORDER:
            ib_ids.append(m.reference_id)
        elif m.reference_type == RT.OUTBOUND_ORDER:
            ob_ids.append(m.reference_id)
    codes = {}
    if ib_ids:
        for pk, code in InboundOrder.objects.filter(pk__in=ib_ids).values_list(
            'pk', 'inbound_order_code'
        ):
            codes[(RT.INBOUND_ORDER, pk)] = code
    if ob_ids:
        for pk, code in OutboundOrder.objects.filter(pk__in=ob_ids).values_list(
            'pk', 'outbound_order_code'
        ):
            codes[(RT.OUTBOUND_ORDER, pk)] = code
    for m in movements:
        m.reference_code = codes.get((m.reference_type, m.reference_id))


class StockMovementInquiryView(LoginRequiredMixin, TemplateView):
    """入出庫履歴照会画面。

    StockMovement（在庫移動の追記専用ログ）を時系列で照会する read-only 画面。
    検索-first パターン（マスタ照会・在庫照会と同じ規約）。入庫 / 出庫 /
    棚卸調整を、SKU・棚番・種別・作業内容・期間で絞り込む。

    StockMovement は追記専用で件数が増え続けるため、テーブルはページネーション
    する。サマリー（件数・数量）は検索結果の全件を DB 集計するので常に正確。
    """

    template_name = 'a/stock/movement_inquiry.html'

    SEARCH_KEYS = (
        'q',
        'product',
        'location',
        'movement_type',
        'reference_type',
        'date_from',
        'date_to',
    )

    SORTABLE = {
        'moved': ['moved_at'],
        'type': ['movement_type'],
        'location': ['location__location_code'],
        'sku': ['sku__sku_code'],
        'product': ['sku__product__product_name'],
        'qty': ['quantity'],
        'after': ['quantity_after'],
        'reference': ['reference_type'],
        'user': ['created_by__username'],
        'note': ['note'],
    }

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
                'location',
                'location__area',
                'sku',
                'sku__product',
                'created_by',
            )
            if f['q']:
                qs = qs.filter(
                    Q(sku__sku_code__icontains=f['q']) | Q(sku__jan_code__icontains=f['q'])
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
            qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, qs, self.SORTABLE, 'moved', 'desc'
            )

            # サマリーは検索結果の全件を集計（ページ送りしても合計は全体ベース）。
            agg = qs.aggregate(
                total=Count('id'),
                in_count=Count('id', filter=Q(movement_type=MT.IN)),
                out_count=Count('id', filter=Q(movement_type=MT.OUT)),
                adj_count=Count('id', filter=Q(movement_type=MT.ADJ)),
                in_qty=Sum('quantity', filter=Q(movement_type=MT.IN)),
                out_qty=Sum('quantity', filter=Q(movement_type=MT.OUT)),
            )
            page = paginate(self.request, qs)
            # ページ分のみ、作業内容（入荷指示/出荷指示）の伝票コードを補完する
            movements_list = list(page.object_list)
            _attach_reference_codes(movements_list)
            page.object_list = movements_list
            ctx['movements'] = page
            ctx['page_obj'] = page
            ctx['stats'] = {
                'total': agg['total'],
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

        ctx['movement_types'] = MT.choices
        ctx['reference_types'] = StockMovement.ReferenceType.choices
        ctx['filters'] = f
        return ctx


class StocktakeLockGuardMixin:
    """全数棚卸ロック中（COUNTING/REVIEW × is_locked=True）は在庫変動を拒否する。

    現在倉庫スコープでアクティブなロックを判定し、ある場合は messages で
    画面通知して同じ URL に redirect する（StockMovement 発行に進ませない）。
    入荷棚入れ・出荷検品・棚間移動・計画外入庫/出庫の各 POST に適用。
    棚卸自身の確定（StocktakeConfirmView）はこのミックスインを当てないので
    ロック中でも ADJ を発行できる。
    """

    def post(self, request, *args, **kwargs):
        warehouse = get_current_warehouse(request)
        locked = StocktakeSession.objects.filter(
            warehouse=warehouse,
            status__in=[
                StocktakeSession.Status.COUNTING,
                StocktakeSession.Status.REVIEW,
            ],
            is_locked=True,
        ).first()
        if locked is not None:
            messages.error(
                request,
                f'全数棚卸 {locked.session_code} の作業中のため、在庫変動は'
                f'できません。棚卸の確定または取消後に再度お試しください。',
            )
            return HttpResponseRedirect(request.path)
        return super().post(request, *args, **kwargs)


class UnplannedStockInView(StocktakeLockGuardMixin, LoginRequiredMixin, FormView):
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
                location=location,
                sku=sku,
                defaults={'quantity': 0},
            )
            balance = StockBalance.objects.select_for_update().get(location=location, sku=sku)
            quantity_before = balance.quantity
            quantity_after = quantity_before + quantity

            movement = StockMovement.objects.create(
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
            # 0 → 正への切り替えで FIFO 用の最古入荷日時をリセットする
            if quantity_before == 0:
                balance.first_received_at = movement.moved_at
            balance.save()

        messages.success(
            self.request,
            f'入庫完了: {sku.sku_code} を {location.location_code} に {quantity} 個 '
            f'(在庫 {quantity_before} → {quantity_after})',
        )
        return super().form_valid(form)


class UnplannedStockOutView(StocktakeLockGuardMixin, LoginRequiredMixin, FormView):
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
                StockBalance.objects.select_for_update().filter(location=location, sku=sku).first()
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


class StockTransferView(StocktakeLockGuardMixin, LoginRequiredMixin, FormView):
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
            from_balance = StockBalance.objects.filter(location=from_loc, sku=sku).first()
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
                location=to_loc,
                sku=sku,
                defaults={'quantity': 0},
            )

            now = timezone.now()
            transfer = StockTransfer.objects.create(
                from_location=from_loc,
                to_location=to_loc,
                sku=sku,
                quantity=quantity,
                status=StockTransfer.Status.COMPLETED,
                transferred_at=now,
                created_by=self.request.user,
            )
            # 移動元から OUT
            StockMovement.objects.create(
                movement_type=StockMovement.MovementType.OUT,
                location=from_loc,
                sku=sku,
                quantity=-quantity,  # OUT なので負の値で記録
                quantity_before=from_on_hand,
                quantity_after=from_on_hand - quantity,
                reference_type=StockMovement.ReferenceType.STOCK_TRANSFER,
                reference_id=transfer.pk,
                note='',
                created_by=self.request.user,
            )
            from_balance.quantity = from_on_hand - quantity
            from_balance.save()
            # 移動先へ IN
            to_before = to_balance.quantity
            to_movement = StockMovement.objects.create(
                movement_type=StockMovement.MovementType.IN,
                location=to_loc,
                sku=sku,
                quantity=quantity,  # IN なので正の値
                quantity_before=to_before,
                quantity_after=to_before + quantity,
                reference_type=StockMovement.ReferenceType.STOCK_TRANSFER,
                reference_id=transfer.pk,
                note='',
                created_by=self.request.user,
            )
            to_balance.quantity = to_before + quantity
            # 0 → 正への切り替えで FIFO 用の最古入荷日時をリセットする。
            # 棚間移動は「元の棚で持っていた在庫が移っただけ」だが、移動先棚に
            # 取って "現在在庫の最古入荷日時" は移動完了時刻が新たな起点となる
            # （古い棚に在庫を持つ別の棚を優先したい時のロジックがブレないよう、
            # 棚単位の時刻で統一する）。
            if to_before == 0:
                to_balance.first_received_at = to_movement.moved_at
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
            loc_qs = Location.objects.filter(location_code=loc_code, is_active=True)
            wh = get_current_warehouse(request)
            if wh is not None:
                loc_qs = loc_qs.filter(warehouse=wh)
            location = loc_qs.first()

        on_hand = 0
        if sku is not None and location is not None:
            balance = StockBalance.objects.filter(location=location, sku=sku).first()
            on_hand = balance.quantity if balance else 0

        return JsonResponse(
            {
                'sku_found': sku is not None,
                'location_found': location is not None,
                'on_hand': on_hand,
                'sku_code': sku.sku_code if sku else sku_code,
                'product_name': sku.product.product_name if sku else '',
                'location_code': location.location_code if location else loc_code,
            }
        )


# ============================================================================
# 棚卸（Stocktake）— PC: 照会・作成・詳細・状態遷移アクション、handheld: カウント
# ============================================================================


class StocktakeInquiryView(LoginRequiredMixin, TemplateView):
    """棚卸セッション照会（検索-first パターン）。"""

    template_name = 'a/stock/stocktake_inquiry.html'

    SEARCH_KEYS = ('q', 'status', 'stocktake_type', 'planned_from', 'planned_to')

    SORTABLE = {
        'code': ['session_code'],
        'type': ['stocktake_type'],
        'target': ['area__area_code'],
        'planned': ['planned_at'],
        'status': ['status'],
        'progress': ['counted_count'],
        'started': ['started_at'],
        'completed': ['completed_at'],
    }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched
        f = {
            'q': g.get('q', '').strip(),
            'status': g.get('status', ''),
            'stocktake_type': g.get('stocktake_type', ''),
            'planned_from': g.get('planned_from', ''),
            'planned_to': g.get('planned_to', ''),
        }
        if searched:
            qs = StocktakeSession.objects.select_related('warehouse', 'area', 'created_by')
            wh = get_current_warehouse(self.request)
            if wh is not None:
                qs = qs.filter(warehouse=wh)
            if f['q']:
                qs = qs.filter(session_code__icontains=f['q'])
            if f['status']:
                qs = qs.filter(status=f['status'])
            if f['stocktake_type']:
                qs = qs.filter(stocktake_type=f['stocktake_type'])
            pf = parse_query_date(f['planned_from'])
            pt = parse_query_date(f['planned_to'])
            if pf:
                qs = qs.filter(planned_at__gte=pf)
            if pt:
                qs = qs.filter(planned_at__lte=pt)
            qs = qs.annotate(
                item_count=Count('items'),
                counted_count=Count(
                    'items',
                    filter=Q(
                        items__status__in=[
                            StocktakeItem.Status.COUNTED,
                            StocktakeItem.Status.ADJUSTED,
                        ]
                    ),
                ),
            )
            qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, qs, self.SORTABLE, 'planned', 'desc'
            )
            ctx['stats'] = {
                'total': qs.count(),
                'planning': qs.filter(status=StocktakeSession.Status.PLANNING).count(),
                'counting': qs.filter(status=StocktakeSession.Status.COUNTING).count(),
                'review': qs.filter(status=StocktakeSession.Status.REVIEW).count(),
                'completed': qs.filter(status=StocktakeSession.Status.COMPLETED).count(),
            }
            page = paginate(self.request, qs)
            ctx['sessions'] = page
            ctx['page_obj'] = page
        else:
            ctx['sessions'] = StocktakeSession.objects.none()
            ctx['stats'] = None
        ctx['status_choices'] = StocktakeSession.Status.choices
        ctx['type_choices'] = StocktakeSession.StocktakeType.choices
        ctx['filters'] = f
        return ctx


class StocktakeCreateView(LoginRequiredMixin, FormView):
    """棚卸セッション作成（PC）。warehouse は現在倉庫に固定。"""

    template_name = 'a/stock/stocktake_form.html'
    form_class = StocktakeSessionForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['warehouse'] = get_current_warehouse(self.request)
        return kwargs

    def form_valid(self, form):
        wh = get_current_warehouse(self.request)
        if wh is None:
            form.add_error(None, '現在の倉庫が特定できません。')
            return self.render_to_response(self.get_context_data(form=form))
        instance = form.save(commit=False)
        instance.warehouse = wh
        instance.created_by = self.request.user
        instance.status = StocktakeSession.Status.PLANNING
        try:
            with transaction.atomic():
                instance.session_code = StocktakeSession.next_code(timezone.localdate())
                instance.save()
        except IntegrityError:
            form.add_error(None, '採番が他の登録と衝突した可能性があります。再度お試しください。')
            return self.render_to_response(self.get_context_data(form=form))
        messages.success(self.request, f'棚卸セッション {instance.session_code} を作成しました。')
        return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[instance.pk]))


class StocktakeDetailView(LoginRequiredMixin, DetailView):
    """棚卸詳細。状態に応じて開始/差異確認へ進む/確定/取消ボタンが出る。"""

    model = StocktakeSession
    template_name = 'a/stock/stocktake_detail.html'
    context_object_name = 'session'

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse', 'area', 'created_by')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = self.object
        items = session.items.select_related(
            'location', 'location__area', 'sku', 'sku__product', 'counted_by'
        ).order_by('location__location_code', 'sku__sku_code')
        # サマリーは全件集計
        agg = items.aggregate(
            total=Count('id'),
            counted=Count(
                'id',
                filter=Q(status__in=[StocktakeItem.Status.COUNTED, StocktakeItem.Status.ADJUSTED]),
            ),
            uncounted=Count('id', filter=Q(status=StocktakeItem.Status.UNCOUNTED)),
            adjusted=Count('id', filter=Q(status=StocktakeItem.Status.ADJUSTED)),
        )
        # 差異あり件数（counted_quantity が NULL でなく system と異なる）
        diff_count = (
            items.filter(
                counted_quantity__isnull=False,
            )
            .exclude(counted_quantity=F('system_quantity'))
            .count()
        )
        ctx['item_stats'] = {
            'total': agg['total'],
            'counted': agg['counted'],
            'uncounted': agg['uncounted'],
            'adjusted': agg['adjusted'],
            'diff_count': diff_count,
        }
        ctx['items'] = paginate(self.request, items)
        ctx['page_obj'] = ctx['items']
        # アクション可否
        st = session.status
        ctx['can_start'] = st == StocktakeSession.Status.PLANNING
        ctx['can_review'] = st == StocktakeSession.Status.COUNTING
        ctx['can_confirm'] = st == StocktakeSession.Status.REVIEW
        ctx['can_cancel'] = st in (
            StocktakeSession.Status.PLANNING,
            StocktakeSession.Status.COUNTING,
            StocktakeSession.Status.REVIEW,
        )
        return ctx


def _get_session_for_action(pk):
    """アクション系ビュー共通: セッションを select_for_update せず取得。

    状態遷移は POST 受付時のチェックで弾く。確定処理（ADJ 発行）の中では
    StockBalance を行ロックする（StocktakeConfirmView 内）。
    """
    return get_object_or_404(StocktakeSession, pk=pk)


class StocktakeStartView(LoginRequiredMixin, View):
    """PLANNING → COUNTING。対象スコープの StockBalance を snapshot 化する。"""

    def post(self, request, pk):
        session = _get_session_for_action(pk)
        if session.status != StocktakeSession.Status.PLANNING:
            messages.error(request, '計画中の棚卸のみ開始できます。')
            return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))
        with transaction.atomic():
            bal_qs = StockBalance.objects.filter(location__warehouse=session.warehouse)
            if session.stocktake_type == StocktakeSession.StocktakeType.CYCLE:
                bal_qs = bal_qs.filter(location__area=session.area)
            balances = list(
                bal_qs.select_related('location', 'sku').order_by(
                    'location__location_code', 'sku__sku_code'
                )
            )
            StocktakeItem.objects.bulk_create(
                [
                    StocktakeItem(
                        session=session,
                        location=b.location,
                        sku=b.sku,
                        system_quantity=b.quantity,
                        status=StocktakeItem.Status.UNCOUNTED,
                    )
                    for b in balances
                ]
            )
            session.status = StocktakeSession.Status.COUNTING
            session.started_at = timezone.now()
            if session.stocktake_type == StocktakeSession.StocktakeType.FULL:
                session.is_locked = True
            session.save()
        messages.success(
            request,
            f'棚卸 {session.session_code} を開始しました'
            f'（{len(balances)} 件の明細をスナップショット）。',
        )
        return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))


class StocktakeReviewView(LoginRequiredMixin, View):
    """COUNTING → REVIEW。"""

    def post(self, request, pk):
        session = _get_session_for_action(pk)
        if session.status != StocktakeSession.Status.COUNTING:
            messages.error(request, 'カウント中の棚卸のみ差異確認へ進めます。')
            return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))
        session.status = StocktakeSession.Status.REVIEW
        session.save(update_fields=['status', 'updated_at'])
        messages.info(request, '差異確認モードへ移行しました。')
        return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))


class StocktakeConfirmView(LoginRequiredMixin, View):
    """REVIEW → COMPLETED。差異 != 0 の明細に StockMovement.ADJ を発行する。"""

    def post(self, request, pk):
        session = _get_session_for_action(pk)
        if session.status != StocktakeSession.Status.REVIEW:
            messages.error(request, '差異確認中の棚卸のみ確定できます。')
            return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))
        adj_count = 0
        with transaction.atomic():
            for item in session.items.select_related('location', 'sku').filter(
                status=StocktakeItem.Status.COUNTED
            ):
                if item.counted_quantity is None or item.counted_quantity == item.system_quantity:
                    item.status = StocktakeItem.Status.ADJUSTED
                    item.save(update_fields=['status', 'updated_at'])
                    continue
                # 行ロックして現在の StockBalance を取得し、実カウント数に合わせる。
                # ADJ 発行〜StockBalance 更新の隙に他の入出庫が走らないようロック。
                StockBalance.objects.get_or_create(
                    location=item.location,
                    sku=item.sku,
                    defaults={'quantity': 0},
                )
                balance = StockBalance.objects.select_for_update().get(
                    location=item.location, sku=item.sku
                )
                before = balance.quantity
                after = item.counted_quantity
                delta = after - before
                if delta == 0:
                    item.status = StocktakeItem.Status.ADJUSTED
                    item.save(update_fields=['status', 'updated_at'])
                    continue
                movement = StockMovement.objects.create(
                    movement_type=StockMovement.MovementType.ADJ,
                    location=item.location,
                    sku=item.sku,
                    quantity=delta,
                    quantity_before=before,
                    quantity_after=after,
                    reference_type=StockMovement.ReferenceType.STOCKTAKE,
                    reference_id=session.pk,
                    note=f'棚卸 {session.session_code} による調整',
                    created_by=request.user,
                )
                balance.quantity = after
                # 棚卸調整で在庫が 0 → 正になった棚は first_received_at をリセット
                # （帳簿差異で発生した在庫は新たな入荷扱いで FIFO 起点とする）
                if before == 0 and after > 0:
                    balance.first_received_at = movement.moved_at
                balance.save()
                StocktakeAdjustment.objects.create(
                    stocktake_item=item,
                    stock_movement=movement,
                    approved_by=request.user,
                )
                item.status = StocktakeItem.Status.ADJUSTED
                item.save(update_fields=['status', 'updated_at'])
                adj_count += 1
            session.status = StocktakeSession.Status.COMPLETED
            session.completed_at = timezone.now()
            session.is_locked = False
            session.save()
        messages.success(
            request,
            f'棚卸 {session.session_code} を確定しました（差異調整 {adj_count} 件を発行）。',
        )
        return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))


class StocktakeCancelView(LoginRequiredMixin, View):
    """* → CANCELLED（COMPLETED と CANCELLED は除く）。"""

    def post(self, request, pk):
        session = _get_session_for_action(pk)
        if session.status in (
            StocktakeSession.Status.COMPLETED,
            StocktakeSession.Status.CANCELLED,
        ):
            messages.error(request, '完了済み・取消済みは状態を変更できません。')
            return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))
        session.status = StocktakeSession.Status.CANCELLED
        session.is_locked = False
        session.save(update_fields=['status', 'is_locked', 'updated_at'])
        messages.info(request, f'棚卸 {session.session_code} を取り消しました。')
        return HttpResponseRedirect(reverse('stock:stocktake_detail', args=[pk]))


class StocktakeCountView(LoginRequiredMixin, View):
    """カウント入口画面（handheld）。COUNTING 中のセッションを選ぶ。"""

    template_name = 'a/handheld/stocktake_count.html'

    def get(self, request):
        wh = get_current_warehouse(request)
        qs = StocktakeSession.objects.filter(status=StocktakeSession.Status.COUNTING)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        sessions = qs.select_related('warehouse', 'area').order_by('-planned_at', '-session_code')
        return render(request, self.template_name, {'sessions': sessions})

    def post(self, request):
        code = (request.POST.get('session_code') or '').strip()
        if not code:
            messages.error(request, '棚卸番号を入力してください。')
            return HttpResponseRedirect(reverse('stock:handheld_stocktake'))
        wh = get_current_warehouse(request)
        qs = StocktakeSession.objects.filter(
            session_code=code, status=StocktakeSession.Status.COUNTING
        )
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        session = qs.first()
        if session is None:
            messages.error(request, f'棚卸番号「{code}」のカウント中セッションが見つかりません。')
            return HttpResponseRedirect(reverse('stock:handheld_stocktake'))
        return HttpResponseRedirect(reverse('stock:handheld_stocktake_work', args=[session.pk]))


class StocktakeCountWorkView(LoginRequiredMixin, FormView):
    """カウント入力画面（handheld）。棚×SKU をスキャン → 実カウント数を入力。"""

    template_name = 'a/handheld/stocktake_count_work.html'
    form_class = StocktakeCountForm

    def dispatch(self, request, *args, **kwargs):
        self.session = get_object_or_404(StocktakeSession, pk=kwargs['pk'])
        if self.session.status != StocktakeSession.Status.COUNTING:
            messages.error(request, 'この棚卸はカウント可能な状態ではありません。')
            return HttpResponseRedirect(reverse('stock:handheld_stocktake'))
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['session'] = self.session
        kwargs['request'] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['session'] = self.session
        items = self.session.items
        ctx['progress'] = {
            'total': items.count(),
            'counted': items.exclude(status=StocktakeItem.Status.UNCOUNTED).count(),
        }
        return ctx

    def get_success_url(self):
        return reverse('stock:handheld_stocktake_work', args=[self.session.pk])

    def form_valid(self, form):
        item = form._item
        counted = form.cleaned_data['counted_quantity']
        with transaction.atomic():
            item.counted_quantity = counted
            item.counted_at = timezone.now()
            item.counted_by = self.request.user
            item.status = StocktakeItem.Status.COUNTED
            item.save()
        diff = counted - item.system_quantity
        diff_label = '差異なし' if diff == 0 else f'差異 +{diff}' if diff > 0 else f'差異 {diff}'
        messages.success(
            self.request,
            f'カウント記録: {item.location.location_code} / '
            f'{item.sku.sku_code} 帳簿 {item.system_quantity} → '
            f'実数 {counted}（{diff_label}）',
        )
        return super().form_valid(form)
