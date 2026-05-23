import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, ProtectedError, Q, Sum
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView, DeleteView, DetailView, TemplateView, UpdateView, View,
)

from core.order_csv import OrderCsvExportView, OrderCsvImportView
from core.utils import paginate, parse_query_date
from masters.models import Location
from stock.views import StocktakeLockGuardMixin
from masters.utils import get_current_warehouse
from stock.models import StockBalance, StockMovement

from .csv_io import INBOUND_ORDER_SPEC
from .forms import InboundOrderForm, InboundOrderItemFormSet
from .models import InboundOrder, InboundOrderItem, InboundReceipt


class CurrentWarehouseScopedMixin:
    """現在ログイン中の倉庫に絞り込むミックスイン（Update/Delete 用）。"""

    def get_queryset(self):
        qs = super().get_queryset()
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs


class ProtectedErrorMixin:
    """DeleteView で PROTECT FK エラーをキャッチしてメッセージで通知。"""

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except ProtectedError as e:
            count = len(e.protected_objects)
            sample = ', '.join(str(o) for o in list(e.protected_objects)[:3])
            messages.error(
                request,
                f'削除できません: 関連データが {count} 件紐づいています（{sample}{"..." if count > 3 else ""}）。',
            )
            return HttpResponseRedirect(self.success_url)


class PendingOnlyMixin:
    """受入れ作業待ち（未着手）の入荷指示だけ編集・削除を許可する。

    受け入れ・検品が始まった指示には実績（ステータス遷移・検品結果など）が
    紐づくため、指示そのものの変更・削除を禁止する。一覧でもボタンを出さないが、
    URL 直打ち対策の保険としてビュー側でも弾く。
    """

    def dispatch(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != InboundOrder.Status.RECEIVING_WAIT:
            messages.error(
                request,
                f'入荷指示 {order.inbound_order_code} は'
                f'「{order.get_status_display()}」のため、編集・削除できません。',
            )
            return HttpResponseRedirect(reverse('inbound:order_inquiry'))
        if order.in_progress_by_id:
            messages.error(
                request,
                f'入荷指示 {order.inbound_order_code} は受入れ作業中のため、'
                f'編集・削除できません。',
            )
            return HttpResponseRedirect(reverse('inbound:order_inquiry'))
        return super().dispatch(request, *args, **kwargs)


class InboundOrderInquiryView(LoginRequiredMixin, TemplateView):
    """入荷指示の照会＋一覧。検索-first パターン。"""

    template_name = 'a/inbound/order_inquiry.html'

    SEARCH_KEYS = ('q', 'supplier', 'status', 'source_type',
                   'expected_from', 'expected_to')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        f = {
            'q': g.get('q', '').strip(),
            'supplier': g.get('supplier', '').strip(),
            'status': g.get('status', ''),
            'source_type': g.get('source_type', ''),
            'expected_from': g.get('expected_from', ''),
            'expected_to': g.get('expected_to', ''),
        }

        if searched:
            qs = InboundOrder.objects.select_related(
                'warehouse', 'supplier', 'created_by', 'in_progress_by')
            wh = get_current_warehouse(self.request)
            if wh is not None:
                qs = qs.filter(warehouse=wh)
            if f['q']:
                qs = qs.filter(
                    Q(inbound_order_code__icontains=f['q'])
                    | Q(purchase_order_code__icontains=f['q'])
                    | Q(supplier_delivery_note_code__icontains=f['q'])
                )
            if f['supplier']:
                qs = qs.filter(
                    Q(supplier__supplier_name__icontains=f['supplier'])
                    | Q(supplier__supplier_code__icontains=f['supplier'])
                )
            if f['status']:
                qs = qs.filter(status=f['status'])
            if f['source_type']:
                qs = qs.filter(source_type=f['source_type'])
            expected_from = parse_query_date(f['expected_from'])
            expected_to = parse_query_date(f['expected_to'])
            if expected_from:
                qs = qs.filter(expected_date__gte=expected_from)
            if expected_to:
                qs = qs.filter(expected_date__lte=expected_to)
            qs = qs.annotate(
                item_count=Count('items'),
                total_expected=Sum('items__quantity_expected'),
            ).order_by('-created_at', '-inbound_order_code')
            page = paginate(self.request, qs)
            ctx['orders'] = page
            ctx['page_obj'] = page
            ctx['stats'] = {
                'total': qs.count(),
                'receiving_wait': qs.filter(
                    status=InboundOrder.Status.RECEIVING_WAIT).count(),
                'inspection_wait': qs.filter(
                    status=InboundOrder.Status.INSPECTION_WAIT).count(),
                'putaway_wait': qs.filter(
                    status=InboundOrder.Status.PUTAWAY_WAIT).count(),
                'completed': qs.filter(
                    status=InboundOrder.Status.COMPLETED).count(),
            }
        else:
            ctx['orders'] = InboundOrder.objects.none()
            ctx['stats'] = None

        ctx['status_choices'] = InboundOrder.Status.choices
        ctx['source_type_choices'] = InboundOrder.SourceType.choices
        ctx['filters'] = f
        return ctx


class InboundOrderDetailView(
    CurrentWarehouseScopedMixin, LoginRequiredMixin, DetailView
):
    """入荷指示の詳細（読み取り専用）。入荷指示照会の「表示」から遷移。

    指示の概要・明細に加え、受け入れ／検品／棚入れの実績（実入荷数・検品結果・
    格納先）も、工程が進んだぶんだけ表示する。状態を問わず閲覧できる。
    """

    model = InboundOrder
    template_name = 'a/inbound/order_detail.html'
    context_object_name = 'order'

    def get_queryset(self):
        # 明細は SKU 順に固定し、検品結果（格納先・担当者）まで一括取得する
        items_qs = InboundOrderItem.objects.select_related(
            'sku__product',
            'inboundreceipt__location__area',
            'inboundreceipt__inspected_by',
            'inboundreceipt__putaway_by',
        ).order_by('sku__sku_code')
        return (
            super().get_queryset()
            .select_related('warehouse', 'supplier', 'created_by',
                            'in_progress_by')
            .prefetch_related(Prefetch('items', queryset=items_qs))
        )


class _OrderFormMixin:
    """Create/Update で共通の処理: 明細 formset の取り回し + 倉庫/作成者の自動設定。"""

    template_name = 'a/inbound/order_form.html'
    form_class = InboundOrderForm

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if self.request.POST:
            ctx['items'] = InboundOrderItemFormSet(self.request.POST, instance=self.object)
        else:
            ctx['items'] = InboundOrderItemFormSet(instance=self.object)

        # 新規登録時のみ: 種別切替で JS が伝票番号を差し替えるための候補値を渡す
        if not self.object:
            today = timezone.localdate()
            ctx['next_codes_json'] = json.dumps({
                InboundOrder.SourceType.MANUAL.value: InboundOrder.next_code(
                    today, InboundOrder.SourceType.MANUAL.value
                ),
                InboundOrder.SourceType.RETURN.value: InboundOrder.next_code(
                    today, InboundOrder.SourceType.RETURN.value
                ),
            })
        return ctx

    def form_valid(self, form):
        ctx = self.get_context_data()
        items = ctx['items']
        if not items.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        try:
            with transaction.atomic():
                # 倉庫は現在ログイン中に固定（マスタと同じスコープ規約）
                wh = get_current_warehouse(self.request)
                if wh is not None and not form.instance.warehouse_id:
                    form.instance.warehouse = wh
                # 新規時のみ created_by を設定（source_type はフォームの絞り込み選択肢で扱う）
                if not form.instance.pk:
                    form.instance.created_by = self.request.user
                self.object = form.save()
                items.instance = self.object
                items.save()
        except IntegrityError:
            # 入荷指示番号は一意。フォーム検証〜保存の隙に他の登録と採番が衝突した
            # 場合は 500 にせず、画面にエラーを出して再操作を促す。
            form.add_error(
                None,
                '登録に失敗しました（入荷指示番号が他の登録と重複した可能性が'
                'あります）。お手数ですが再度お試しください。',
            )
            return self.render_to_response(self.get_context_data(form=form))
        return HttpResponseRedirect(self.get_success_url())


class InboundOrderCreateView(_OrderFormMixin, LoginRequiredMixin, CreateView):
    model = InboundOrder
    success_url = reverse_lazy('inbound:order_inquiry')


class InboundOrderUpdateView(
    _OrderFormMixin, CurrentWarehouseScopedMixin, LoginRequiredMixin,
    PendingOnlyMixin, UpdateView
):
    model = InboundOrder
    success_url = reverse_lazy('inbound:order_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse', 'supplier')


class InboundOrderDeleteView(
    CurrentWarehouseScopedMixin, LoginRequiredMixin, PendingOnlyMixin,
    ProtectedErrorMixin, DeleteView
):
    model = InboundOrder
    template_name = 'a/inbound/order_confirm_delete.html'
    success_url = reverse_lazy('inbound:order_inquiry')

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related('warehouse', 'supplier', 'created_by')
            .prefetch_related('items__sku__product')
        )


class InboundArrivalView(LoginRequiredMixin, View):
    """入荷受け入れ（受入れ作業の入口）画面 — handheld 端末用。

    入荷指示番号をスキャン → 指示内容を表示 → 「受入れ作業開始」で対象指示を
    作業担当者（ログインユーザー）に紐づけてロックし、受入れ作業画面へ進む。
    別の担当者が作業中の指示は開始できない。
    """

    template_name = 'a/inbound/handheld/arrival.html'

    def _scoped_orders(self):
        """現在ログイン中の倉庫スコープに絞った InboundOrder クエリセット。"""
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code', '').strip()
        ctx = {'code': code}
        if code:
            order = (
                self._scoped_orders()
                .prefetch_related('items__sku__product')
                .filter(inbound_order_code=code)
                .first()
            )
            if order is None:
                ctx['lookup_error'] = f'入荷指示番号「{code}」は見つかりません。'
            else:
                ctx['order'] = order
        return render(request, self.template_name, ctx)

    def post(self, request, *args, **kwargs):
        """「受入れ作業開始」「引き継いで開始」: 指示を担当者に紐づけて受入れ作業へ。"""
        order_id = request.POST.get('order_id', '')
        takeover = request.POST.get('takeover') == '1'
        with transaction.atomic():
            order = None
            if order_id.isdigit():
                order = (
                    self._scoped_orders()
                    .select_for_update(of=('self',))
                    .filter(pk=order_id)
                    .first()
                )
            if order is None:
                messages.error(request, '入荷指示が見つかりません。')
                return HttpResponseRedirect(reverse('inbound:handheld_arrival'))
            if order.status != InboundOrder.Status.RECEIVING_WAIT:
                messages.info(
                    request,
                    f'入荷指示 {order.inbound_order_code} は現在'
                    f'「{order.get_status_display()}」のため、受入れ作業の対象外です。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_arrival'))
            # 別の担当者が作業中: 引き継ぎ(takeover)指定がなければ開始不可
            prev = order.in_progress_by
            blocked = bool(prev) and prev.pk != request.user.pk
            if blocked and not takeover:
                worker = prev.display_name or prev.username
                messages.error(
                    request,
                    f'入荷指示 {order.inbound_order_code} は {worker} が'
                    f'受入れ作業中です。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_arrival'))
            # 担当者として確保。担当者が変わるとき（新規・引き継ぎ）は開始時刻を更新
            if order.in_progress_by_id != request.user.pk:
                order.in_progress_at = timezone.now()
            order.in_progress_by = request.user
            order.save()
            if blocked and takeover:
                worker = prev.display_name or prev.username
                messages.info(
                    request,
                    f'{worker} から受入れ作業を引き継ぎました。',
                )
        return HttpResponseRedirect(
            reverse('inbound:handheld_receiving', args=[order.pk]))


class InboundReceivingView(LoginRequiredMixin, View):
    """受入れ作業（数量確認）画面 — handheld 端末用。

    入荷受け入れ画面から遷移。指示明細の SKU を1商品ずつ JAN スキャンで確認し、
    実入荷数（予定数で初期表示）を入力していく。全商品の確認後「受け入れ完了」で
    全明細の quantity_received を保存し、ステータスを PENDING → ARRIVED へ進める。
    在庫計上は後工程「棚入れ」で行うため、この画面では在庫を動かさない。
    """

    template_name = 'a/inbound/handheld/receiving.html'

    def _scoped_orders(self):
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def _guard(self, request, order):
        """受入れ作業が可能か（存在・PENDING・担当者）を検査。不可なら redirect。"""
        if order is None:
            messages.error(request, '入荷指示が見つかりません。')
            return HttpResponseRedirect(reverse('inbound:handheld_arrival'))
        if order.status != InboundOrder.Status.RECEIVING_WAIT:
            messages.info(
                request,
                f'入荷指示 {order.inbound_order_code} は現在'
                f'「{order.get_status_display()}」のため、受入れ作業の対象外です。',
            )
            return HttpResponseRedirect(reverse('inbound:handheld_arrival'))
        if (order.in_progress_by_id
                and order.in_progress_by_id != request.user.pk):
            worker = (order.in_progress_by.display_name
                      or order.in_progress_by.username)
            messages.error(
                request,
                f'入荷指示 {order.inbound_order_code} は {worker} が'
                f'受入れ作業中です。',
            )
            return HttpResponseRedirect(reverse('inbound:handheld_arrival'))
        return None

    def get(self, request, pk, *args, **kwargs):
        order = (
            self._scoped_orders()
            .prefetch_related('items__sku__product')
            .filter(pk=pk)
            .first()
        )
        guard = self._guard(request, order)
        if guard:
            return guard
        # 1商品ずつ処理する handheld ウィザード用に、明細を JSON で渡す
        items = [
            {
                'id': it.pk,
                'sku': it.sku.sku_code,
                'jan': it.sku.jan_code or '',
                'name': it.sku.product.product_name,
                'expected': it.quantity_expected,
            }
            for it in order.items.all()
        ]
        return render(request, self.template_name,
                      {'order': order, 'items': items})

    def post(self, request, pk, *args, **kwargs):
        with transaction.atomic():
            # 確定対象の指示行をロックして取得（同時確定の二重処理を防ぐ）
            order = (
                self._scoped_orders()
                .select_for_update(of=('self',))
                .prefetch_related('items__sku')
                .filter(pk=pk)
                .first()
            )
            guard = self._guard(request, order)
            if guard:
                return guard

            # 各明細の実入荷数（ウィザードが hidden input に格納した値）を検証
            items = list(order.items.all())
            parsed = {}
            for it in items:
                raw = request.POST.get(f'qty_{it.pk}', '').strip()
                try:
                    val = int(raw)
                except ValueError:
                    val = -1
                if val < 0:
                    messages.error(
                        request,
                        f'実数が未確認の商品があります（{it.sku.sku_code}）。'
                        f'受入れ作業をやり直してください。',
                    )
                    return HttpResponseRedirect(
                        reverse('inbound:handheld_receiving', args=[pk])
                    )
                parsed[it.pk] = val

            for it in items:
                it.quantity_received = parsed[it.pk]
                it.save()
            order.status = InboundOrder.Status.INSPECTION_WAIT
            order.arrived_at = timezone.now()
            # 受入れ作業の担当者ロックを解除
            order.in_progress_by = None
            order.in_progress_at = None
            order.save()

        messages.success(
            request,
            f'入荷指示 {order.inbound_order_code} の受け入れを確定しました。'
            f'（次工程: 入荷検品）',
        )
        return HttpResponseRedirect(reverse('inbound:handheld_arrival'))


class InboundInspectionView(LoginRequiredMixin, View):
    """入荷検品（検品作業の入口）画面 — handheld 端末用。

    入荷指示番号をスキャン → 受け入れ済み(ARRIVED)の指示を表示 → 「検品開始」で
    対象指示を作業担当者（ログインユーザー）に紐づけてロックし、検品作業画面へ進む。
    別の担当者が作業中の指示は開始できない。
    """

    template_name = 'a/inbound/handheld/inspection.html'

    def _scoped_orders(self):
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code', '').strip()
        ctx = {'code': code}
        if code:
            order = (
                self._scoped_orders()
                .prefetch_related('items__sku__product')
                .filter(inbound_order_code=code)
                .first()
            )
            if order is None:
                ctx['lookup_error'] = f'入荷指示番号「{code}」は見つかりません。'
            else:
                ctx['order'] = order
        return render(request, self.template_name, ctx)

    def post(self, request, *args, **kwargs):
        """「検品開始」「引き継いで開始」: 指示を担当者に紐づけて検品作業へ。"""
        order_id = request.POST.get('order_id', '')
        takeover = request.POST.get('takeover') == '1'
        with transaction.atomic():
            order = None
            if order_id.isdigit():
                order = (
                    self._scoped_orders()
                    .select_for_update(of=('self',))
                    .filter(pk=order_id)
                    .first()
                )
            if order is None:
                messages.error(request, '入荷指示が見つかりません。')
                return HttpResponseRedirect(reverse('inbound:handheld_inspection'))
            if order.status != InboundOrder.Status.INSPECTION_WAIT:
                messages.info(
                    request,
                    f'入荷指示 {order.inbound_order_code} は現在'
                    f'「{order.get_status_display()}」のため、検品の対象外です。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_inspection'))
            # 別の担当者が作業中: 引き継ぎ(takeover)指定がなければ開始不可
            prev = order.in_progress_by
            blocked = bool(prev) and prev.pk != request.user.pk
            if blocked and not takeover:
                worker = prev.display_name or prev.username
                messages.error(
                    request,
                    f'入荷指示 {order.inbound_order_code} は {worker} が'
                    f'検品作業中です。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_inspection'))
            # 担当者として確保。担当者が変わるとき（新規・引き継ぎ）は開始時刻を更新
            if order.in_progress_by_id != request.user.pk:
                order.in_progress_at = timezone.now()
            order.in_progress_by = request.user
            order.save()
            if blocked and takeover:
                worker = prev.display_name or prev.username
                messages.info(
                    request,
                    f'{worker} から検品作業を引き継ぎました。',
                )
        return HttpResponseRedirect(
            reverse('inbound:handheld_inspection_work', args=[order.pk]))


class InboundInspectionWorkView(LoginRequiredMixin, View):
    """検品作業（数量・良否確認）画面 — handheld 端末用。

    入荷検品画面から遷移。受け入れ済み指示の SKU を1商品ずつ JAN スキャンで
    検品し、検品数（良品数）と良品/破損を記録する。「検品完了」で明細ごとに
    InboundReceipt を作成し、ステータスを ARRIVED → PUTAWAY（格納待ち）へ進める。
    在庫計上・格納先ロケーションは後工程「棚入れ」で行う。
    """

    template_name = 'a/inbound/handheld/inspection_work.html'

    def _scoped_orders(self):
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def _guard(self, request, order):
        """検品作業が可能か（存在・ARRIVED・担当者）を検査。不可なら redirect。"""
        if order is None:
            messages.error(request, '入荷指示が見つかりません。')
            return HttpResponseRedirect(reverse('inbound:handheld_inspection'))
        if order.status != InboundOrder.Status.INSPECTION_WAIT:
            messages.info(
                request,
                f'入荷指示 {order.inbound_order_code} は現在'
                f'「{order.get_status_display()}」のため、検品の対象外です。',
            )
            return HttpResponseRedirect(reverse('inbound:handheld_inspection'))
        if (order.in_progress_by_id
                and order.in_progress_by_id != request.user.pk):
            worker = (order.in_progress_by.display_name
                      or order.in_progress_by.username)
            messages.error(
                request,
                f'入荷指示 {order.inbound_order_code} は {worker} が'
                f'検品作業中です。',
            )
            return HttpResponseRedirect(reverse('inbound:handheld_inspection'))
        return None

    def get(self, request, pk, *args, **kwargs):
        order = (
            self._scoped_orders()
            .prefetch_related('items__sku__product')
            .filter(pk=pk)
            .first()
        )
        guard = self._guard(request, order)
        if guard:
            return guard
        # 1商品ずつ処理する handheld ウィザード用に、明細を JSON で渡す
        items = [
            {
                'id': it.pk,
                'sku': it.sku.sku_code,
                'jan': it.sku.jan_code or '',
                'name': it.sku.product.product_name,
                'expected': it.quantity_expected,
                'received': it.quantity_received,
            }
            for it in order.items.all()
        ]
        return render(request, self.template_name,
                      {'order': order, 'items': items})

    @staticmethod
    def _to_int(raw):
        """POST 値を非負整数に変換。未入力・不正値は -1（検証で弾く）。"""
        try:
            return int((raw or '').strip())
        except ValueError:
            return -1

    def post(self, request, pk, *args, **kwargs):
        with transaction.atomic():
            # 確定対象の指示行をロックして取得（同時検品の二重処理を防ぐ）
            order = (
                self._scoped_orders()
                .select_for_update(of=('self',))
                .prefetch_related('items__sku')
                .filter(pk=pk)
                .first()
            )
            guard = self._guard(request, order)
            if guard:
                return guard

            # 各明細の良品数・不良品数・不良理由（ウィザードが hidden input に
            # 格納した値）を検証
            items = list(order.items.all())
            parsed = {}
            for it in items:
                good = self._to_int(request.POST.get(f'good_{it.pk}'))
                defective = self._to_int(request.POST.get(f'defective_{it.pk}'))
                reason = request.POST.get(f'reason_{it.pk}', '').strip()
                if good < 0 or defective < 0:
                    messages.error(
                        request,
                        f'検品数が未確認の商品があります（{it.sku.sku_code}）。'
                        f'検品作業をやり直してください。',
                    )
                    return HttpResponseRedirect(
                        reverse('inbound:handheld_inspection_work', args=[pk])
                    )
                parsed[it.pk] = (good, defective, reason)

            # 明細ごとに InboundReceipt（検品結果）を作成
            for it in items:
                good, defective, reason = parsed[it.pk]
                total = good + defective   # 検品した入荷総数
                if total < it.quantity_expected:
                    dtype = InboundReceipt.DiscrepancyType.SHORTAGE
                elif total > it.quantity_expected:
                    dtype = InboundReceipt.DiscrepancyType.EXCESS
                else:
                    dtype = InboundReceipt.DiscrepancyType.NONE
                InboundReceipt.objects.update_or_create(
                    inbound_order_item=it,
                    defaults={
                        'quantity_expected': it.quantity_expected,
                        'quantity_received': total,
                        'quantity_defective': defective,
                        'discrepancy_type': dtype,
                        'discrepancy_note': reason if defective > 0 else '',
                        'inspected_by': request.user,
                    },
                )
            order.status = InboundOrder.Status.PUTAWAY_WAIT
            # 検品作業の担当者ロックを解除
            order.in_progress_by = None
            order.in_progress_at = None
            order.save()

        messages.success(
            request,
            f'入荷指示 {order.inbound_order_code} の検品が完了しました。'
            f'（次工程: 棚入れ）',
        )
        return HttpResponseRedirect(reverse('inbound:handheld_inspection'))


def _receipt_of(item):
    """InboundOrderItem に紐づく検品結果（InboundReceipt）を返す。未検品なら None。"""
    try:
        return item.inboundreceipt
    except InboundReceipt.DoesNotExist:
        return None


def _putaway_item_rows(order):
    """棚入れ入口画面の明細表示用に、検品結果（計上数・不良数）を行リスト化する。"""
    rows = []
    for it in order.items.select_related('sku__product', 'inboundreceipt').all():
        receipt = _receipt_of(it)
        rows.append({
            'sku': it.sku.sku_code,
            'name': it.sku.product.product_name,
            'received': receipt.quantity_received if receipt else None,
            'defective': receipt.quantity_defective if receipt else 0,
            # 計上数（在庫に積む数）= 実入荷数 − 不良品数。未検品は None
            'good': receipt.quantity_good if receipt else None,
        })
    return rows


class InboundPutawayView(LoginRequiredMixin, View):
    """入荷棚入れ（棚入れ作業の入口）画面 — handheld 端末用。

    入荷指示番号をスキャン → 検品済み(PUTAWAY_WAIT)の指示を表示 → 「棚入れ開始」で
    対象指示を作業担当者（ログインユーザー）に紐づけてロックし、棚入れ作業画面へ進む。
    別の担当者が作業中の指示は開始できない。
    """

    template_name = 'a/inbound/handheld/putaway.html'

    def _scoped_orders(self):
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code', '').strip()
        ctx = {'code': code}
        if code:
            order = (
                self._scoped_orders()
                .filter(inbound_order_code=code)
                .first()
            )
            if order is None:
                ctx['lookup_error'] = f'入荷指示番号「{code}」は見つかりません。'
            else:
                ctx['order'] = order
                ctx['item_rows'] = _putaway_item_rows(order)
        return render(request, self.template_name, ctx)

    def post(self, request, *args, **kwargs):
        """「棚入れ開始」「引き継いで開始」: 指示を担当者に紐づけて棚入れ作業へ。"""
        order_id = request.POST.get('order_id', '')
        takeover = request.POST.get('takeover') == '1'
        with transaction.atomic():
            order = None
            if order_id.isdigit():
                order = (
                    self._scoped_orders()
                    .select_for_update(of=('self',))
                    .filter(pk=order_id)
                    .first()
                )
            if order is None:
                messages.error(request, '入荷指示が見つかりません。')
                return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
            if order.status != InboundOrder.Status.PUTAWAY_WAIT:
                messages.info(
                    request,
                    f'入荷指示 {order.inbound_order_code} は現在'
                    f'「{order.get_status_display()}」のため、棚入れの対象外です。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
            # 別の担当者が作業中: 引き継ぎ(takeover)指定がなければ開始不可
            prev = order.in_progress_by
            blocked = bool(prev) and prev.pk != request.user.pk
            if blocked and not takeover:
                worker = prev.display_name or prev.username
                messages.error(
                    request,
                    f'入荷指示 {order.inbound_order_code} は {worker} が'
                    f'棚入れ作業中です。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
            # 担当者として確保。担当者が変わるとき（新規・引き継ぎ）は開始時刻を更新
            if order.in_progress_by_id != request.user.pk:
                order.in_progress_at = timezone.now()
            order.in_progress_by = request.user
            order.save()
            if blocked and takeover:
                worker = prev.display_name or prev.username
                messages.info(
                    request,
                    f'{worker} から棚入れ作業を引き継ぎました。',
                )
        return HttpResponseRedirect(
            reverse('inbound:handheld_putaway_work', args=[order.pk]))


class InboundPutawayWorkView(StocktakeLockGuardMixin, LoginRequiredMixin, View):
    """棚入れ作業（格納先ロケーション登録）画面 — handheld 端末用。

    入荷棚入れ画面から遷移。検品済み指示の SKU を1商品ずつスキャンし、格納先
    ロケーションをスキャンして InboundPutawayItemView へ即時 POST する
    （明細単位コミット = 中断耐性あり）。
    1明細ごとに計上数ぶんの StockMovement.IN を発行して在庫を加算し、
    InboundReceipt に格納先・担当者・入庫履歴を記録する。全明細が完了したら
    指示のステータスを PUTAWAY_WAIT → COMPLETED（入荷完了）へ進める。
    不良品数は在庫計上しない。

    GET 時は putaway_at がまだ未設定の明細のみを画面に出す（途中離脱後の
    再入場で「続きから」できる）。確定済みの明細は再編集不可（業務 WMS の
    慣習に合わせ、間違いは棚間移動など別フローで物理修正する）。
    """

    template_name = 'a/inbound/handheld/putaway_work.html'

    def _scoped_orders(self):
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def _guard(self, request, order):
        """棚入れ作業が可能か（存在・PUTAWAY_WAIT・担当者）を検査。不可なら redirect。"""
        if order is None:
            messages.error(request, '入荷指示が見つかりません。')
            return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
        if order.status != InboundOrder.Status.PUTAWAY_WAIT:
            messages.info(
                request,
                f'入荷指示 {order.inbound_order_code} は現在'
                f'「{order.get_status_display()}」のため、棚入れの対象外です。',
            )
            return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
        if (order.in_progress_by_id
                and order.in_progress_by_id != request.user.pk):
            worker = (order.in_progress_by.display_name
                      or order.in_progress_by.username)
            messages.error(
                request,
                f'入荷指示 {order.inbound_order_code} は {worker} が'
                f'棚入れ作業中です。',
            )
            return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
        return None

    def get(self, request, pk, *args, **kwargs):
        order = self._scoped_orders().filter(pk=pk).first()
        if order is None:
            messages.error(request, '入荷指示が見つかりません。')
            return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
        # COMPLETED の場合は最終結果サマリを読み取り専用で表示
        if order.status == InboundOrder.Status.COMPLETED:
            summary_items = []
            for it in order.items.select_related(
                    'sku__product', 'inboundreceipt').all():
                receipt = _receipt_of(it)
                if receipt is None:
                    continue
                summary_items.append({
                    'sku': it.sku.sku_code,
                    'name': it.sku.product.product_name,
                    'good': receipt.quantity_good,
                    'location': (receipt.location.location_code
                                 if receipt.location else ''),
                    'skip': receipt.quantity_good <= 0,
                })
            return render(request, self.template_name, {
                'mode': 'summary',
                'order': order,
                'summary_items': summary_items,
            })
        guard = self._guard(request, order)
        if guard:
            return guard
        # 1商品ずつ処理する handheld ウィザード用に、未棚入れの明細だけを JSON で渡す
        items = []
        done_count = 0
        total = 0
        for it in order.items.select_related(
                'sku__product', 'inboundreceipt').all():
            receipt = _receipt_of(it)
            if receipt is None:
                # 検品結果が無い = データ不整合。入口へ差し戻す
                messages.error(
                    request,
                    f'入荷指示 {order.inbound_order_code} に未検品の明細があります。',
                )
                return HttpResponseRedirect(reverse('inbound:handheld_putaway'))
            total += 1
            if receipt.putaway_at is not None:
                # 既に確定済み（前回の作業セッションで処理済み）→ 画面に出さない
                done_count += 1
                continue
            good = receipt.quantity_good
            items.append({
                'id': it.pk,
                'sku': it.sku.sku_code,
                'jan': it.sku.jan_code or '',
                'name': it.sku.product.product_name,
                'received': receipt.quantity_received,
                'defective': receipt.quantity_defective,
                'good': good,
                # 格納可能なエリア区分（ピッキング種別から決まる）
                'area_type': it.sku.required_location_type,
                'area_type_label': it.sku.required_location_type.label,
            })
        return render(request, self.template_name, {
            'order': order, 'items': items,
            'done_count': done_count, 'total': total,
        })


class InboundPutawayItemView(StocktakeLockGuardMixin, LoginRequiredMixin, View):
    """棚入れの 1 明細を即時確定する API（fetch 用）。

    POST 時に格納先ロケーションコードを受け取り、検品結果（InboundReceipt）に
    格納先・担当者・入庫履歴を記録し、計上数ぶんの StockMovement.IN を発行して
    在庫を加算する。全明細が完了したら指示のステータスを COMPLETED に進めて
    作業担当者ロックを解除する。

    レスポンス (JSON):
      - ok=True  -> {'ok': True, 'all_done': bool, 'remaining': int, 'location': str}
      - ok=False -> {'ok': False, 'error': str}（HTTP は 200 のまま。ステータスは body で判断）
    """

    http_method_names = ['post']

    def _scoped_orders(self, request):
        qs = InboundOrder.objects.select_related(
            'supplier', 'warehouse', 'in_progress_by')
        wh = get_current_warehouse(request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def post(self, request, order_pk, item_pk, *args, **kwargs):
        location_code = (request.POST.get('location_code') or '').strip()
        with transaction.atomic():
            order = (
                self._scoped_orders(request)
                .select_for_update(of=('self',))
                .filter(pk=order_pk)
                .first()
            )
            if order is None:
                return JsonResponse({
                    'ok': False, 'error': '入荷指示が見つかりません。',
                })
            if order.status != InboundOrder.Status.PUTAWAY_WAIT:
                return JsonResponse({
                    'ok': False,
                    'error': f'入荷指示 {order.inbound_order_code} は'
                             f'「{order.get_status_display()}」のため棚入れできません。',
                })
            if (order.in_progress_by_id
                    and order.in_progress_by_id != request.user.pk):
                worker = (order.in_progress_by.display_name
                          or order.in_progress_by.username)
                return JsonResponse({
                    'ok': False,
                    'error': f'{worker} が棚入れ作業中です。',
                })

            item = (
                order.items.select_related('sku__product')
                .filter(pk=item_pk).first()
            )
            if item is None:
                return JsonResponse({
                    'ok': False, 'error': '対象の明細が見つかりません。',
                })
            receipt = _receipt_of(item)
            if receipt is None:
                return JsonResponse({
                    'ok': False,
                    'error': f'{item.sku.sku_code} は未検品です。',
                })
            if receipt.putaway_at is not None:
                # 既に確定済み（重複 POST の防御）
                return JsonResponse({
                    'ok': False,
                    'error': f'{item.sku.sku_code} は既に棚入れ済みです。',
                })

            good = receipt.quantity_good
            location = None
            if good > 0:
                if not location_code:
                    return JsonResponse({
                        'ok': False, 'error': '格納先の棚番を入力してください。',
                    })
                location = (
                    Location.objects.select_related('area')
                    .filter(location_code=location_code, is_active=True,
                            warehouse=order.warehouse)
                    .first()
                )
                if location is None:
                    return JsonResponse({
                        'ok': False,
                        'error': f'棚番「{location_code}」は存在しないか無効です。',
                    })
                required = item.sku.required_location_type
                if location.area.location_type != required:
                    return JsonResponse({
                        'ok': False,
                        'error': f'{item.sku.sku_code} は「{required.label}」エリアに'
                                 f'格納してください（「{location_code}」は'
                                 f'「{location.area.get_location_type_display()}」エリア）。',
                    })

            # 在庫加算 + StockMovement.IN 発行（不良のみ＝good==0 のときは何もしない）
            movement = None
            if good > 0:
                StockBalance.objects.get_or_create(
                    location=location, sku=item.sku,
                    defaults={'quantity': 0},
                )
                balance = (
                    StockBalance.objects.select_for_update()
                    .get(location=location, sku=item.sku)
                )
                quantity_before = balance.quantity
                quantity_after = quantity_before + good
                movement = StockMovement.objects.create(
                    movement_type=StockMovement.MovementType.IN,
                    location=location,
                    sku=item.sku,
                    quantity=good,
                    quantity_before=quantity_before,
                    quantity_after=quantity_after,
                    reference_type=StockMovement.ReferenceType.INBOUND_ORDER,
                    reference_id=order.pk,
                    note='',
                    created_by=request.user,
                )
                balance.quantity = quantity_after
                # 0 → 正への切り替えで FIFO 用の最古入荷日時をリセット
                if quantity_before == 0:
                    balance.first_received_at = movement.moved_at
                balance.save()

            now = timezone.now()
            receipt.location = location
            receipt.stock_movement = movement
            receipt.putaway_by = request.user
            receipt.putaway_at = now
            receipt.save()

            # 全明細が putaway_at を持ったら入荷指示を COMPLETED に
            remaining = (
                order.items
                .filter(inboundreceipt__putaway_at__isnull=True)
                .count()
            )
            all_done = (remaining == 0)
            if all_done:
                order.status = InboundOrder.Status.COMPLETED
                order.received_at = now
                order.in_progress_by = None
                order.in_progress_at = None
                order.save()

        return JsonResponse({
            'ok': True, 'all_done': all_done, 'remaining': remaining,
            'location': location.location_code if location else '',
            'location_name': location.location_name if location else '',
            'area_name': location.area.area_name if location else '',
        })


# ---- 入荷指示 CSV 入出力 ----

class InboundOrderCsvExportView(OrderCsvExportView):
    """入荷指示を CSV（ヘッダ＋明細フラット様式）でエクスポートする。"""
    spec = INBOUND_ORDER_SPEC


class InboundOrderCsvImportView(OrderCsvImportView):
    """入荷指示を CSV でインポートする（新規作成のみ）。"""
    spec = INBOUND_ORDER_SPEC
