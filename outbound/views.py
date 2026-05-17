from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, ProtectedError, Q, Sum
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import (
    CreateView, DeleteView, DetailView, TemplateView, UpdateView, View,
)

from masters.models import Area, Sku
from masters.utils import get_current_warehouse
from stock.models import StockBalance, StockMovement

from .forms import OutboundOrderForm, OutboundOrderItemFormSet
from .models import (
    OutboundOrder, OutboundOrderItem, PickingList, PickingListItem,
    Shipment, ShipmentItem, StockReservation,
)
from .utils import code39_svg, create_with_retry


class CurrentWarehouseScopedMixin:
    """現在ログイン中の倉庫に絞り込むミックスイン（Update/Delete/Detail 用）。"""

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


class EditableOnlyMixin:
    """出荷起動前（出荷起動待ち）の出荷指示だけ編集・削除を許可する。

    出荷起動で在庫引き当て・ピッキングリスト生成が走った後は実績が紐づくため、
    指示そのものの変更・削除を禁止する。一覧でもボタンを出さないが、URL 直打ち
    対策の保険としてビュー側でも弾く。
    """

    def dispatch(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != OutboundOrder.Status.ALLOCATION_WAIT:
            messages.error(
                request,
                f'出荷指示 {order.outbound_order_code} は'
                f'「{order.get_status_display()}」のため、編集・削除できません。',
            )
            return HttpResponseRedirect(reverse('outbound:order_inquiry'))
        return super().dispatch(request, *args, **kwargs)


class OutboundOrderInquiryView(LoginRequiredMixin, TemplateView):
    """出荷指示の照会＋一覧。検索-first パターン。"""

    template_name = 'a/outbound/order_inquiry.html'

    SEARCH_KEYS = ('q', 'customer', 'status', 'source_type',
                   'deadline_from', 'deadline_to')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        f = {
            'q': g.get('q', '').strip(),
            'customer': g.get('customer', '').strip(),
            'status': g.get('status', ''),
            'source_type': g.get('source_type', ''),
            'deadline_from': g.get('deadline_from', ''),
            'deadline_to': g.get('deadline_to', ''),
        }

        if searched:
            qs = OutboundOrder.objects.select_related(
                'warehouse', 'customer', 'created_by')
            wh = get_current_warehouse(self.request)
            if wh is not None:
                qs = qs.filter(warehouse=wh)
            if f['q']:
                qs = qs.filter(
                    Q(outbound_order_code__icontains=f['q'])
                    | Q(external_order_id__icontains=f['q'])
                    | Q(delivery_name__icontains=f['q'])
                )
            if f['customer']:
                qs = qs.filter(
                    Q(customer__customer_name__icontains=f['customer'])
                    | Q(customer__customer_code__icontains=f['customer'])
                )
            if f['status']:
                qs = qs.filter(status=f['status'])
            if f['source_type']:
                qs = qs.filter(source_type=f['source_type'])
            if f['deadline_from']:
                qs = qs.filter(deadline_at__date__gte=f['deadline_from'])
            if f['deadline_to']:
                qs = qs.filter(deadline_at__date__lte=f['deadline_to'])
            qs = qs.annotate(
                item_count=Count('items'),
                total_ordered=Sum('items__quantity_ordered'),
            ).order_by('-priority', 'deadline_at', '-outbound_order_code')
            ctx['orders'] = qs
            ctx['stats'] = {
                'total': qs.count(),
                'allocation_wait': qs.filter(
                    status=OutboundOrder.Status.ALLOCATION_WAIT).count(),
                'picking_wait': qs.filter(
                    status=OutboundOrder.Status.PICKING_WAIT).count(),
                'inspection_wait': qs.filter(
                    status=OutboundOrder.Status.INSPECTION_WAIT).count(),
                'shipped': qs.filter(
                    status=OutboundOrder.Status.SHIPPED).count(),
            }
        else:
            ctx['orders'] = OutboundOrder.objects.none()
            ctx['stats'] = None

        ctx['status_choices'] = OutboundOrder.Status.choices
        # 出荷元種別フィルタは oms / manual のみ。返品出荷は画面スコープ外のため出さない
        ctx['source_type_choices'] = [
            c for c in OutboundOrder.SourceType.choices
            if c[0] != OutboundOrder.SourceType.RETURN
        ]
        ctx['filters'] = f
        return ctx


class OutboundOrderDetailView(
    CurrentWarehouseScopedMixin, LoginRequiredMixin, DetailView
):
    """出荷指示の詳細（読み取り専用）。出荷指示照会の指示番号リンクから遷移。

    指示の概要・明細（需要）に加え、出荷起動で確定するピッキング元ロケーション・
    実出荷数も、工程が進んだぶんだけ表示する。状態を問わず閲覧できる。
    """

    model = OutboundOrder
    template_name = 'a/outbound/order_detail.html'
    context_object_name = 'order'

    def get_queryset(self):
        # 明細は SKU 順に固定し、ピッキング元ロケーションまで一括取得する
        items_qs = OutboundOrderItem.objects.select_related(
            'sku__product', 'location__area',
        ).order_by('sku__sku_code')
        return (
            super().get_queryset()
            .select_related('warehouse', 'customer', 'created_by',
                            'cancelled_by')
            .prefetch_related(Prefetch('items', queryset=items_qs))
        )


class _OrderFormMixin:
    """Create/Update で共通の処理: 明細 formset の取り回し + 倉庫/作成者の自動設定。"""

    template_name = 'a/outbound/order_form.html'
    form_class = OutboundOrderForm

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if self.request.POST:
            ctx['items'] = OutboundOrderItemFormSet(self.request.POST, instance=self.object)
        else:
            ctx['items'] = OutboundOrderItemFormSet(instance=self.object)
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
                # 新規時のみ: 画面登録は通常出荷に固定（source_type はフォーム外）
                if not form.instance.pk:
                    form.instance.source_type = OutboundOrder.SourceType.MANUAL
                    form.instance.created_by = self.request.user
                self.object = form.save()
                items.instance = self.object
                items.save()
        except IntegrityError:
            # 出荷指示番号は一意。フォーム検証〜保存の隙に他の登録と採番が衝突した
            # 場合は 500 にせず、画面にエラーを出して再操作を促す。
            form.add_error(
                None,
                '登録に失敗しました（出荷指示番号が他の登録と重複した可能性が'
                'あります）。お手数ですが再度お試しください。',
            )
            return self.render_to_response(self.get_context_data(form=form))
        return HttpResponseRedirect(self.get_success_url())


class OutboundOrderCreateView(_OrderFormMixin, LoginRequiredMixin, CreateView):
    model = OutboundOrder
    success_url = reverse_lazy('outbound:order_inquiry')


class OutboundOrderUpdateView(
    _OrderFormMixin, CurrentWarehouseScopedMixin, LoginRequiredMixin,
    EditableOnlyMixin, UpdateView
):
    model = OutboundOrder
    success_url = reverse_lazy('outbound:order_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse', 'customer')


class OutboundOrderDeleteView(
    CurrentWarehouseScopedMixin, LoginRequiredMixin, EditableOnlyMixin,
    ProtectedErrorMixin, DeleteView
):
    model = OutboundOrder
    template_name = 'a/outbound/order_confirm_delete.html'
    success_url = reverse_lazy('outbound:order_inquiry')

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related('warehouse', 'customer', 'created_by')
            .prefetch_related('items__sku__product')
        )


def _available_locations(sku, warehouse):
    """SKU の引き当て可能な棚を「引き当て可能数の多い順」で返す。

    引き当て可能数 = StockBalance.quantity − 有効な引き当て(active)の合計。
    呼び出し側のトランザクション内で StockBalance 行をロックする。
    戻り値: [(Location, available_qty), ...]（available の多い順）
    """
    balances = list(
        StockBalance.objects
        .select_for_update(of=('self',))
        .filter(sku=sku, location__warehouse=warehouse, quantity__gt=0)
        .select_related('location__area')
    )
    # この SKU の有効な引き当てをロケーション別に集計
    reserved = (
        StockReservation.objects
        .filter(sku=sku, status=StockReservation.Status.ACTIVE,
                location__warehouse=warehouse)
        .values('location')
        .annotate(total=Sum('quantity'))
    )
    reserved_map = {row['location']: row['total'] for row in reserved}
    result = []
    for b in balances:
        available = b.quantity - reserved_map.get(b.location_id, 0)
        if available > 0:
            result.append((b.location, available))
    # 在庫数の多い棚から引く（複数棚への分割を最小化）
    result.sort(key=lambda pair: pair[1], reverse=True)
    return result


def _generate_picking_lists(order, items, user, *, picking_type, completed):
    """同一ピッキング種別の引き当て済み明細をエリア単位で PickingList 化する。

    completed=True（種まき/AGV の自動仕分け）のときは、ピッキングリスト・明細を
    完了状態で作る — 実倉庫では自動ピッキング・自動仕分け設備が担う工程を、本
    システムでは出荷起動の内部処理として即時に完了させ、検品工程へ「仕分け済み
    データ」を渡す。戻り値: 生成したピッキングリスト数。
    """
    if not items:
        return 0
    now = timezone.now()
    today = timezone.localdate()
    by_area = {}  # area_id -> (Area, [item, ...])
    for item in items:
        area = item.location.area
        by_area.setdefault(area.pk, (area, []))[1].append(item)
    for area, area_items in by_area.values():
        # picking_list_code は採番衝突に備えてリトライ付きで作成する
        picking_list = create_with_retry(lambda area=area: PickingList.objects.create(
            picking_list_code=PickingList.next_code(today),
            warehouse=order.warehouse, area=area,
            picking_type=picking_type,
            status=(PickingList.Status.COMPLETED if completed
                    else PickingList.Status.PENDING),
            started_at=now if completed else None,
            completed_at=now if completed else None,
            created_by=user,
        ))
        # 巡回順は棚番順
        area_items.sort(key=lambda it: it.location.location_code)
        for sort_idx, item in enumerate(area_items, start=1):
            PickingListItem.objects.create(
                picking_list=picking_list, outbound_order_item=item,
                location=item.location, sku=item.sku,
                quantity_requested=item.quantity_ordered,
                quantity_picked=(item.quantity_ordered if completed else 0),
                status=(PickingListItem.Status.PICKED if completed
                        else PickingListItem.Status.PENDING),
                picked_at=now if completed else None,
                sort_order=sort_idx,
            )
    return len(by_area)


def _try_launch_order(order, user):
    """出荷指示1件を起動する（在庫引き当て＋ピッキングリスト生成）。

    A案: 指示単位の全量引き当て。全明細を引き当てられた場合のみ確定し、1明細でも
    在庫不足なら何もせず allocation_wait のまま残す。引き当て（reservation）は全明細
    に対して行う。オーダーピッキング対象は通常のピッキングリスト（pending）を発行し、
    AGV/GTP 対象は自動ピッキング・自動仕分け済みとして完了状態のピッキングリストを
    即時生成する（検品工程に仕分け済みデータを渡す）。呼び出し側の
    transaction.atomic() 内で、order をロック済み・status=ALLOCATION_WAIT で渡すこと。
    戻り値: {'order', 'ok', 'reason'（NG時）, 'pl_count'/'auto_count'（OK時）}
    """
    items = list(order.items.select_related('sku').all())

    # --- 1パス目: 全明細の引き当て計画を作る（1つでも在庫不足なら中断） ---
    plan = []  # [(item, [(location, qty), ...]), ...]
    for item in items:
        need = item.quantity_ordered
        alloc = []
        for location, available in _available_locations(item.sku, order.warehouse):
            if need <= 0:
                break
            take = min(need, available)
            alloc.append((location, take))
            need -= take
        if need > 0:
            return {'order': order, 'ok': False,
                    'reason': f'{item.sku.sku_code} の在庫が不足しています'}
        plan.append((item, alloc))

    # --- 2パス目: 引き当てを確定（StockReservation 作成・明細を棚別に分割） ---
    located_items = []
    for item, alloc in plan:
        for idx, (location, qty) in enumerate(alloc):
            reservation = StockReservation.objects.create(
                location=location, sku=item.sku, quantity=qty,
                status=StockReservation.Status.ACTIVE,
                order=order, created_by=user,
            )
            if idx == 0:
                # 1棚目は需要明細をそのまま更新（location=NULL → 棚を確定）
                item.location = location
                item.reservation = reservation
                item.quantity_ordered = qty
                item.save()
                located_items.append(item)
            else:
                # 2棚目以降は明細を分割（1棚 = 1明細。uk(order,sku,location) を満たす）
                located_items.append(OutboundOrderItem.objects.create(
                    outbound_order=order, sku=item.sku, location=location,
                    reservation=reservation, quantity_ordered=qty,
                ))

    # --- ピッキングリスト生成（指示 × エリア単位） ---
    # オーダーピッキング対象は通常のピッキングリスト（pending）を発行する。
    # AGV/GTP 対象は実倉庫では自動ピッキング・自動仕分け設備が処理する工程を、
    # 出荷起動の内部処理として完了状態のピッキングリストとして即時生成する。
    order_pick_items = [
        it for it in located_items
        if it.sku.picking_type == Sku.PickingType.ORDER
    ]
    total_pick_items = [
        it for it in located_items
        if it.sku.picking_type == Sku.PickingType.TOTAL
    ]
    pl_count = _generate_picking_lists(
        order, order_pick_items, user,
        picking_type=PickingList.PickingType.ORDER, completed=False)
    auto_count = _generate_picking_lists(
        order, total_pick_items, user,
        picking_type=PickingList.PickingType.TOTAL, completed=True)

    # オーダーピッキング対象があればピッキング工程へ。AGV/GTP のみの指示は
    # 自動仕分けまで完了済みなので出荷検品工程へ直接進める。
    if order_pick_items:
        order.status = OutboundOrder.Status.PICKING_WAIT
    else:
        order.status = OutboundOrder.Status.INSPECTION_WAIT
    order.save()
    return {'order': order, 'ok': True,
            'pl_count': pl_count, 'auto_count': auto_count}


class OutboundLaunchView(LoginRequiredMixin, View):
    """出荷起動画面。

    出荷起動待ち（allocation_wait）の指示を選んで「出荷起動」すると、選択した指示に
    対して在庫引き当て（StockReservation 作成）とピッキングリスト生成を一括実行し、
    ステータスを allocation_wait → picking_wait へ進める。全量引き当てできない指示は
    起動せずスキップし、結果メッセージで報告する。
    """

    template_name = 'a/outbound/launch.html'

    def _candidates(self):
        """出荷起動待ちの指示（現在倉庫スコープ、優先度順）。"""
        qs = OutboundOrder.objects.filter(
            status=OutboundOrder.Status.ALLOCATION_WAIT
        ).select_related('customer')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs.annotate(
            item_count=Count('items'),
            total_ordered=Sum('items__quantity_ordered'),
        ).order_by('-priority', 'deadline_at', 'outbound_order_code')

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name,
                      {'orders': self._candidates()})

    def post(self, request, *args, **kwargs):
        ids = [i for i in request.POST.getlist('order_ids') if i.isdigit()]
        if not ids:
            messages.error(request, '出荷起動する指示を選択してください。')
            return HttpResponseRedirect(reverse('outbound:launch'))

        launched, skipped = [], []
        with transaction.atomic():
            # 確定対象の指示行をロックして取得（同時起動の二重処理を防ぐ）
            qs = (
                OutboundOrder.objects
                .select_for_update(of=('self',))
                .filter(pk__in=ids, status=OutboundOrder.Status.ALLOCATION_WAIT)
            )
            wh = get_current_warehouse(request)
            if wh is not None:
                qs = qs.filter(warehouse=wh)
            # 優先度の高い指示から引き当て（限られた在庫を先に確保）
            for order in qs.order_by('-priority', 'deadline_at',
                                     'outbound_order_code'):
                result = _try_launch_order(order, request.user)
                (launched if result['ok'] else skipped).append(result)

        if launched:
            pl_total = sum(r['pl_count'] for r in launched)
            auto_total = sum(r['auto_count'] for r in launched)
            detail = f'ピッキングリスト {pl_total} 件を生成'
            if auto_total:
                detail += f'／AGV分は自動仕分け済み {auto_total} 件'
            messages.success(
                request,
                f'{len(launched)} 件を出荷起動しました（{detail}）。',
            )
        if skipped:
            detail = '／'.join(
                f"{r['order'].outbound_order_code}（{r['reason']}）"
                for r in skipped
            )
            messages.warning(
                request,
                f'{len(skipped)} 件は在庫不足のため起動できませんでした: {detail}',
            )
        if not launched and not skipped:
            messages.info(
                request,
                '対象の出荷指示がありませんでした'
                '（すでに起動済みの可能性があります）。',
            )
        return HttpResponseRedirect(reverse('outbound:launch'))


class PickingListInquiryView(LoginRequiredMixin, TemplateView):
    """ピッキングリスト印刷の一覧画面。

    出荷起動で生成されたピッキングリストを一覧表示する。リスト番号リンクから
    印刷ビュー（帳票表示）へ遷移する。出荷起動で大量に生成されるため、
    ステータス・エリアで絞り込める。
    """

    template_name = 'a/outbound/picking_list_inquiry.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        f = {'status': g.get('status', ''), 'area': g.get('area', '')}

        qs = PickingList.objects.select_related('warehouse', 'area', 'assigned_to')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        if f['status']:
            qs = qs.filter(status=f['status'])
        if f['area'].isdigit():
            qs = qs.filter(area_id=f['area'])
        qs = qs.annotate(
            item_count=Count('items'),
            total_qty=Sum('items__quantity_requested'),
        ).order_by('-created_at', '-picking_list_code')

        ctx['picking_lists'] = qs
        ctx['status_choices'] = PickingList.Status.choices
        areas = Area.objects.filter(is_active=True)
        if wh is not None:
            areas = areas.filter(warehouse=wh)
        ctx['areas'] = areas.order_by('area_code')
        ctx['filters'] = f
        return ctx


class PickingListPrintView(
    CurrentWarehouseScopedMixin, LoginRequiredMixin, DetailView
):
    """ピッキングリスト印刷ビュー（帳票表示）。

    1枚のピッキングリストを帳票レイアウトで表示する。ピッキングリスト番号は
    バーコード（Code39）でも表示。ブラウザの印刷機能（window.print）で実プリンタ
    印刷・PDF保存ができる。
    """

    model = PickingList
    template_name = 'a/outbound/picking_list_print.html'
    context_object_name = 'picking_list'

    def get_queryset(self):
        # 明細は棚番順（sort_order）に固定して帳票化する
        items_qs = PickingListItem.objects.select_related(
            'sku__product', 'location',
            'outbound_order_item__outbound_order__customer',
        ).order_by('sort_order')
        return (
            super().get_queryset()
            .select_related('warehouse', 'area', 'assigned_to', 'created_by')
            .prefetch_related(Prefetch('items', queryset=items_qs))
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['barcode_svg'] = code39_svg(self.object.picking_list_code)
        items = list(self.object.items.all())
        ctx['line_items'] = items
        # 当システムのピッキングリストは「出荷指示 × エリア」単位なので 1リスト=1指示
        ctx['outbound_order'] = (
            items[0].outbound_order_item.outbound_order if items else None
        )
        return ctx


def _order_of_picking_list(picking_list):
    """ピッキングリストが属する出荷指示を返す（1リスト=1指示）。明細が無ければ None。"""
    first = (
        picking_list.items
        .select_related('outbound_order_item__outbound_order__customer')
        .first()
    )
    return first.outbound_order_item.outbound_order if first else None


def _picking_item_rows(picking_list):
    """ピッキング入口画面の明細表示用に、棚番順の行リストを返す。"""
    rows = []
    for it in (picking_list.items
               .select_related('sku__product', 'location')
               .order_by('sort_order')):
        rows.append({
            'location': it.location.location_code,
            'sku': it.sku.sku_code,
            'name': it.sku.product.product_name,
            'requested': it.quantity_requested,
        })
    return rows


class OutboundPickingView(LoginRequiredMixin, View):
    """ピッキング作業（作業の入口）画面 — handheld 端末用。

    ピッキングリスト番号をスキャン → 未着手(pending)／作業中(in_progress)のリストを
    表示 → 「ピッキング開始」で対象リストを作業担当者（ログインユーザー）に紐づけて
    ロックし、ピッキング作業画面へ進む。別の担当者が作業中のリストは引き継ぎ操作を
    しない限り開始できない。AGV/GTP 対象は出荷起動時に自動仕分け済み（completed）
    なので、ここに現れるのはオーダーピッキング対象（要作業）のみ。
    """

    template_name = 'a/outbound/handheld/picking.html'

    def _scoped_lists(self):
        """現在ログイン中の倉庫スコープに絞った PickingList クエリセット。"""
        qs = PickingList.objects.select_related(
            'warehouse', 'area', 'assigned_to')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code', '').strip()
        ctx = {'code': code}
        if code:
            picking_list = (
                self._scoped_lists()
                .filter(picking_list_code=code)
                .first()
            )
            if picking_list is None:
                ctx['lookup_error'] = (
                    f'ピッキングリスト番号「{code}」は見つかりません。')
            else:
                ctx['picking_list'] = picking_list
                ctx['item_rows'] = _picking_item_rows(picking_list)
                ctx['outbound_order'] = _order_of_picking_list(picking_list)
        return render(request, self.template_name, ctx)

    def post(self, request, *args, **kwargs):
        """「ピッキング開始」「引き継いで開始」: リストを担当者に紐づけて作業へ。"""
        list_id = request.POST.get('list_id', '')
        takeover = request.POST.get('takeover') == '1'
        with transaction.atomic():
            picking_list = None
            if list_id.isdigit():
                picking_list = (
                    self._scoped_lists()
                    .select_for_update(of=('self',))
                    .filter(pk=list_id)
                    .first()
                )
            if picking_list is None:
                messages.error(request, 'ピッキングリストが見つかりません。')
                return HttpResponseRedirect(
                    reverse('outbound:handheld_picking'))
            if picking_list.status not in (PickingList.Status.PENDING,
                                            PickingList.Status.IN_PROGRESS):
                messages.info(
                    request,
                    f'ピッキングリスト {picking_list.picking_list_code} は現在'
                    f'「{picking_list.get_status_display()}」のため、'
                    f'ピッキングの対象外です。',
                )
                return HttpResponseRedirect(
                    reverse('outbound:handheld_picking'))
            # 別の担当者が作業中: 引き継ぎ(takeover)指定がなければ開始不可
            prev = picking_list.assigned_to
            blocked = bool(prev) and prev.pk != request.user.pk
            if blocked and not takeover:
                worker = prev.display_name or prev.username
                messages.error(
                    request,
                    f'ピッキングリスト {picking_list.picking_list_code} は '
                    f'{worker} がピッキング作業中です。',
                )
                return HttpResponseRedirect(
                    reverse('outbound:handheld_picking'))
            # 担当者として確保。担当者が変わるとき（新規・引き継ぎ）は開始時刻を更新
            if picking_list.assigned_to_id != request.user.pk:
                picking_list.started_at = timezone.now()
            picking_list.assigned_to = request.user
            picking_list.status = PickingList.Status.IN_PROGRESS
            picking_list.save()
            if blocked and takeover:
                worker = prev.display_name or prev.username
                messages.info(
                    request, f'{worker} からピッキング作業を引き継ぎました。')
        return HttpResponseRedirect(
            reverse('outbound:handheld_picking_work', args=[picking_list.pk]))


class OutboundPickingWorkView(LoginRequiredMixin, View):
    """ピッキング作業（実ピッキング数の登録）画面 — handheld 端末用。

    ピッキング入口画面から遷移。ピッキングリストの明細を棚番順に1商品ずつ、棚番と
    商品をスキャンで照合して実ピッキング数を入力する。「ピッキング完了」で各明細の
    quantity_picked・status（picked/short）を保存し、リストを completed にする。
    指示の全ピッキングリストが完了したら、出荷指示を picking_wait → inspection_wait
    へ進める。在庫の減算は後工程「出荷検品」で行うため、この画面では在庫を動かさない。
    """

    template_name = 'a/outbound/handheld/picking_work.html'

    def _scoped_lists(self):
        qs = PickingList.objects.select_related(
            'warehouse', 'area', 'assigned_to')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def _guard(self, request, picking_list):
        """ピッキング作業が可能か（存在・in_progress・担当者）を検査。不可なら redirect。"""
        if picking_list is None:
            messages.error(request, 'ピッキングリストが見つかりません。')
            return HttpResponseRedirect(reverse('outbound:handheld_picking'))
        if picking_list.status != PickingList.Status.IN_PROGRESS:
            messages.info(
                request,
                f'ピッキングリスト {picking_list.picking_list_code} は現在'
                f'「{picking_list.get_status_display()}」のため、'
                f'ピッキングの対象外です。',
            )
            return HttpResponseRedirect(reverse('outbound:handheld_picking'))
        if (picking_list.assigned_to_id
                and picking_list.assigned_to_id != request.user.pk):
            worker = (picking_list.assigned_to.display_name
                      or picking_list.assigned_to.username)
            messages.error(
                request,
                f'ピッキングリスト {picking_list.picking_list_code} は '
                f'{worker} がピッキング作業中です。',
            )
            return HttpResponseRedirect(reverse('outbound:handheld_picking'))
        return None

    def get(self, request, pk, *args, **kwargs):
        picking_list = self._scoped_lists().filter(pk=pk).first()
        guard = self._guard(request, picking_list)
        if guard:
            return guard
        # 1商品ずつ処理する handheld ウィザード用に、明細を棚番順の JSON で渡す
        items = [
            {
                'id': it.pk,
                'location': it.location.location_code,
                'sku': it.sku.sku_code,
                'jan': it.sku.jan_code or '',
                'name': it.sku.product.product_name,
                'requested': it.quantity_requested,
            }
            for it in (picking_list.items
                       .select_related('sku__product', 'location')
                       .order_by('sort_order'))
        ]
        return render(request, self.template_name, {
            'picking_list': picking_list,
            'items': items,
            'outbound_order': _order_of_picking_list(picking_list),
        })

    @staticmethod
    def _to_int(raw):
        """POST 値を整数に変換。未入力・不正値は -1（検証で弾く）。"""
        try:
            return int((raw or '').strip())
        except ValueError:
            return -1

    def post(self, request, pk, *args, **kwargs):
        with transaction.atomic():
            # 確定対象のリスト行をロックして取得（同時確定の二重処理を防ぐ）
            picking_list = (
                self._scoped_lists()
                .select_for_update(of=('self',))
                .filter(pk=pk)
                .first()
            )
            guard = self._guard(request, picking_list)
            if guard:
                return guard

            items = list(
                picking_list.items.select_related('sku').order_by('sort_order'))

            # 1パス目: 全明細の実ピッキング数を検証（0 〜 指示数量）
            parsed = {}
            for it in items:
                val = self._to_int(request.POST.get(f'picked_{it.pk}'))
                if val < 0 or val > it.quantity_requested:
                    messages.error(
                        request,
                        f'実ピッキング数が未確認・不正な商品があります'
                        f'（{it.sku.sku_code}）。ピッキング作業をやり直してください。',
                    )
                    return HttpResponseRedirect(reverse(
                        'outbound:handheld_picking_work', args=[pk]))
                parsed[it.pk] = val

            # 2パス目: 明細を確定（指示数量どおり=picked / 不足=short）
            now = timezone.now()
            for it in items:
                picked = parsed[it.pk]
                it.quantity_picked = picked
                it.status = (
                    PickingListItem.Status.PICKED
                    if picked >= it.quantity_requested
                    else PickingListItem.Status.SHORT
                )
                it.picked_by = request.user
                it.picked_at = now
                it.save()

            picking_list.status = PickingList.Status.COMPLETED
            picking_list.completed_at = now
            picking_list.save()

            # 指示の全ピッキングリストが完了したら出荷検品工程へ進める。
            # AGV/GTP 分は出荷起動時に completed 済みなのでブロックしない。
            # 「全リスト完了か」の判定は、同一指示の別リストを同時に完了したとき
            # 取りこぼさないよう、先に指示行をロックしてから行う（ロック前に判定
            # すると、双方が相手の完了コミットを見られず指示が picking_wait の
            # まま残る競合が起きる）。
            order = _order_of_picking_list(picking_list)
            if order is not None:
                locked = (
                    OutboundOrder.objects
                    .select_for_update(of=('self',))
                    .get(pk=order.pk)
                )
                unfinished = PickingList.objects.filter(
                    items__outbound_order_item__outbound_order=order,
                    status__in=[PickingList.Status.PENDING,
                                PickingList.Status.IN_PROGRESS],
                ).exists()
                if (not unfinished
                        and locked.status == OutboundOrder.Status.PICKING_WAIT):
                    locked.status = OutboundOrder.Status.INSPECTION_WAIT
                    locked.save()

        messages.success(
            request,
            f'ピッキングリスト {picking_list.picking_list_code} の'
            f'ピッキングが完了しました。',
        )
        return HttpResponseRedirect(reverse('outbound:handheld_picking'))


def _picked_qty_map(order):
    """出荷指示の各明細(OutboundOrderItem.pk)の実ピッキング数を返す。

    出荷起動でロケーション分割された各明細には、ピッキング作業（または AGV/GTP の
    自動仕分け）で生成された PickingListItem が1件ずつ対応する。その quantity_picked を
    引いて {outbound_order_item_id: quantity_picked} の形で返す。
    """
    rows = (
        PickingListItem.objects
        .filter(outbound_order_item__outbound_order=order)
        .values('outbound_order_item_id', 'quantity_picked')
    )
    return {r['outbound_order_item_id']: r['quantity_picked'] for r in rows}


def _inspection_items(order, picked_map):
    """出荷検品の明細表示用に、SKU 順の行リスト（dict）を返す。

    入口画面の明細テーブルと、作業画面の handheld ウィザード（JSON）で共用する。
    """
    rows = []
    for it in (order.items
               .select_related('sku__product', 'location')
               .order_by('sku__sku_code')):
        picked = picked_map.get(it.pk, 0)
        rows.append({
            'id': it.pk,
            'sku': it.sku.sku_code,
            'jan': it.sku.jan_code or '',
            'name': it.sku.product.product_name,
            'location': it.location.location_code if it.location else '',
            'ordered': it.quantity_ordered,
            'picked': picked,
            # 欠品数（指示に満たないピッキング不足分）。0 なら欠品なし
            'short': max(0, it.quantity_ordered - picked),
        })
    return rows


class OutboundInspectionView(LoginRequiredMixin, View):
    """出荷検品（検品・梱包作業の入口）画面 — handheld 端末用。

    出荷指示番号をスキャン → 出荷検品作業待ち(inspection_wait)の指示を表示 →
    「検品開始」で出荷実績(Shipment)を作成・作業担当者（ログインユーザー）に紐づけて
    ロックし、出荷検品作業画面へ進む。検品・梱包工程の作業ロックは Shipment.in_progress_by
    に置く（出荷指示自体にはロックを置かない）。別の担当者が検品中の指示は引き継ぎ操作を
    しない限り開始できない。
    """

    template_name = 'a/outbound/handheld/inspection.html'

    def _scoped_orders(self):
        """現在ログイン中の倉庫スコープに絞った OutboundOrder クエリセット。"""
        qs = OutboundOrder.objects.select_related('warehouse', 'customer')
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
                .filter(outbound_order_code=code)
                .first()
            )
            if order is None:
                ctx['lookup_error'] = f'出荷指示番号「{code}」は見つかりません。'
            else:
                ctx['order'] = order
                ctx['item_rows'] = _inspection_items(order, _picked_qty_map(order))
                ctx['shipment'] = (
                    Shipment.objects.select_related('in_progress_by')
                    .filter(outbound_order=order).first()
                )
        return render(request, self.template_name, ctx)

    def post(self, request, *args, **kwargs):
        """「検品開始」「引き継いで開始」: Shipment を作成・担当者に紐づけて作業へ。"""
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
                messages.error(request, '出荷指示が見つかりません。')
                return HttpResponseRedirect(
                    reverse('outbound:handheld_inspection'))
            if order.status != OutboundOrder.Status.INSPECTION_WAIT:
                messages.info(
                    request,
                    f'出荷指示 {order.outbound_order_code} は現在'
                    f'「{order.get_status_display()}」のため、'
                    f'出荷検品の対象外です。',
                )
                return HttpResponseRedirect(
                    reverse('outbound:handheld_inspection'))
            # 出荷実績(Shipment)＝検品・梱包工程の作業成果物。初回開始で作成する。
            # order を行ロック済みなので二重作成は起きない。
            shipment = Shipment.objects.filter(outbound_order=order).first()
            if shipment is None:
                # shipment_code は採番衝突に備えてリトライ付きで作成する
                shipment = create_with_retry(lambda: Shipment.objects.create(
                    shipment_code=Shipment.next_code(timezone.localdate()),
                    outbound_order=order,
                    status=Shipment.Status.INSPECTING,
                    created_by=request.user,
                ))
            # 別の担当者が作業中: 引き継ぎ(takeover)指定がなければ開始不可
            prev = shipment.in_progress_by
            blocked = bool(prev) and prev.pk != request.user.pk
            if blocked and not takeover:
                worker = prev.display_name or prev.username
                messages.error(
                    request,
                    f'出荷指示 {order.outbound_order_code} は {worker} が'
                    f'出荷検品作業中です。',
                )
                return HttpResponseRedirect(
                    reverse('outbound:handheld_inspection'))
            # 担当者として確保。担当者が変わるとき（新規・引き継ぎ）は開始時刻を更新
            if shipment.in_progress_by_id != request.user.pk:
                shipment.in_progress_at = timezone.now()
            shipment.in_progress_by = request.user
            shipment.save()
            if blocked and takeover:
                worker = prev.display_name or prev.username
                messages.info(
                    request, f'{worker} から出荷検品作業を引き継ぎました。')
        return HttpResponseRedirect(
            reverse('outbound:handheld_inspection_work', args=[order.pk]))


class OutboundInspectionWorkView(LoginRequiredMixin, View):
    """出荷検品・梱包作業（出荷確定）画面 — handheld 端末用。

    出荷検品画面から遷移。出荷する明細を1商品ずつ SKU スキャンで照合し、品番・員数を
    最終確認する。「出荷検品完了」で実出荷数ぶんの StockMovement.OUT を発行して在庫を
    減算、引き当て(StockReservation)を解放、出荷実績明細(ShipmentItem)を SKU 単位で
    作成し、出荷指示を inspection_wait → shipped（出荷完了）へ進める。実出荷数は
    ピッキング工程で確定済みの quantity_picked をそのまま用いる（検証のみ）。

    【簡略化（意図的）】検品・梱包完了をもって「出荷完了(shipped)」とみなす。実工程は
    検品→梱包→送り状貼付→出荷待機→トラック積込→出発で、本来の出荷完了はトラック出発
    （carrier 引き渡し）の時点。本 MVP は出荷バース管理（積込・出発）をスコープ外とし、
    検品完了を出荷確定とみなす。詳細は post() の出荷確定コメントを参照。
    """

    template_name = 'a/outbound/handheld/inspection_work.html'

    def _scoped_orders(self):
        qs = OutboundOrder.objects.select_related('warehouse', 'customer')
        wh = get_current_warehouse(self.request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def _guard(self, request, order, shipment):
        """検品作業が可能か（存在・inspection_wait・Shipment・担当者）を検査。"""
        if order is None:
            messages.error(request, '出荷指示が見つかりません。')
            return HttpResponseRedirect(reverse('outbound:handheld_inspection'))
        if order.status != OutboundOrder.Status.INSPECTION_WAIT:
            messages.info(
                request,
                f'出荷指示 {order.outbound_order_code} は現在'
                f'「{order.get_status_display()}」のため、出荷検品の対象外です。',
            )
            return HttpResponseRedirect(reverse('outbound:handheld_inspection'))
        if shipment is None or shipment.in_progress_by_id != request.user.pk:
            if shipment is not None and shipment.in_progress_by_id:
                worker = (shipment.in_progress_by.display_name
                          or shipment.in_progress_by.username)
                messages.error(
                    request,
                    f'出荷指示 {order.outbound_order_code} は {worker} が'
                    f'出荷検品作業中です。',
                )
            else:
                messages.error(
                    request,
                    f'出荷指示 {order.outbound_order_code} の検品はまだ'
                    f'開始されていません。先に「検品開始」を行ってください。',
                )
            return HttpResponseRedirect(reverse('outbound:handheld_inspection'))
        return None

    def get(self, request, pk, *args, **kwargs):
        order = self._scoped_orders().filter(pk=pk).first()
        shipment = None
        if order is not None:
            shipment = (
                Shipment.objects.select_related('in_progress_by')
                .filter(outbound_order=order).first()
            )
        guard = self._guard(request, order, shipment)
        if guard:
            return guard
        # 1商品ずつ処理する handheld ウィザード用に、明細を JSON で渡す
        items = _inspection_items(order, _picked_qty_map(order))
        return render(request, self.template_name, {
            'order': order, 'shipment': shipment, 'items': items,
        })

    def post(self, request, pk, *args, **kwargs):
        with transaction.atomic():
            # 確定対象の指示・出荷実績の行をロックして取得（同時確定の二重処理を防ぐ）
            order = (
                self._scoped_orders()
                .select_for_update(of=('self',))
                .filter(pk=pk)
                .first()
            )
            shipment = None
            if order is not None:
                shipment = (
                    Shipment.objects
                    .select_for_update(of=('self',))
                    .select_related('in_progress_by')
                    .filter(outbound_order=order)
                    .first()
                )
            guard = self._guard(request, order, shipment)
            if guard:
                return guard

            picked_map = _picked_qty_map(order)
            items = list(
                order.items.select_related('sku', 'location')
                .order_by('location_id', 'sku_id'))

            # 1パス目: 確認フラグ＋在庫を検証する。在庫不足等を見つけたら何も書き換える
            # 前に中断する（在庫行はロックして 2パス目までそのまま保持する）。
            plan = []  # [(item, picked, balance), ...]
            for it in items:
                picked = picked_map.get(it.pk, 0)
                if picked > 0 and request.POST.get(f'confirmed_{it.pk}') != '1':
                    messages.error(
                        request,
                        f'未確認の明細があります（{it.sku.sku_code}）。'
                        f'出荷検品作業をやり直してください。',
                    )
                    return HttpResponseRedirect(reverse(
                        'outbound:handheld_inspection_work', args=[pk]))
                balance = None
                if picked > 0:
                    balance = (
                        StockBalance.objects.select_for_update()
                        .filter(location=it.location, sku=it.sku)
                        .first()
                    )
                    on_hand = balance.quantity if balance else 0
                    if picked > on_hand:
                        loc = it.location.location_code if it.location else '—'
                        messages.error(
                            request,
                            f'{it.sku.sku_code} は棚番 {loc} の在庫（{on_hand}）が'
                            f'実出荷数（{picked}）に不足しています。在庫差異を'
                            f'解消してから再度お試しください。',
                        )
                        return HttpResponseRedirect(reverse(
                            'outbound:handheld_inspection_work', args=[pk]))
                plan.append((it, picked, balance))

            # 2パス目: 出庫（OUT 発行・在庫減算）。ShipmentItem は uk(shipment, sku)
            # のため SKU 単位に集約する。
            now = timezone.now()
            by_sku = {}
            for it, picked, balance in plan:
                it.quantity_shipped = picked
                movement = None
                if picked > 0:
                    quantity_before = balance.quantity
                    quantity_after = quantity_before - picked
                    movement = StockMovement.objects.create(
                        movement_type=StockMovement.MovementType.OUT,
                        location=it.location, sku=it.sku,
                        quantity=-picked,  # OUT なので負の値で記録
                        quantity_before=quantity_before,
                        quantity_after=quantity_after,
                        reference_type=StockMovement.ReferenceType.OUTBOUND_ORDER,
                        reference_id=order.pk,
                        note='',
                        created_by=request.user,
                    )
                    balance.quantity = quantity_after
                    balance.save()
                it.save()
                agg = by_sku.setdefault(
                    it.sku_id,
                    {'sku': it.sku, 'ooi': it, 'qty': 0, 'movements': []})
                agg['qty'] += picked
                if movement is not None:
                    agg['movements'].append(movement)

            # 引き当て(StockReservation)を解放する
            StockReservation.objects.filter(
                order=order, status=StockReservation.Status.ACTIVE
            ).update(status=StockReservation.Status.RELEASED, released_at=now)

            # 出荷実績明細(ShipmentItem)を SKU 単位で作成（実出荷数 > 0 のみ）
            for agg in by_sku.values():
                if agg['qty'] <= 0:
                    continue
                ShipmentItem.objects.create(
                    shipment=shipment,
                    outbound_order_item=agg['ooi'],
                    sku=agg['sku'],
                    quantity_shipped=agg['qty'],
                    # 複数ロケーションに分かれた SKU は出庫履歴が複数になるため、
                    # 単独のときだけ紐づける
                    stock_movement=(agg['movements'][0]
                                    if len(agg['movements']) == 1 else None),
                )

            # --- 出荷確定 ---
            # 【簡略化（意図的）】検品・梱包完了をもって「出荷完了(shipped)」とみなす。
            # 実工程は 検品 → 梱包 → 送り状貼付 → 出荷待機 → トラック積込 → 出発 で、
            # 本来の出荷完了はトラック出発（carrier 引き渡し）の時点。本 MVP は出荷バース
            # 管理（積込・出発）をスコープ外とし、検品完了を出荷確定とみなしている。
            # このため Shipment.Status.READY（出荷準備完了）は未使用で、shipped_at は
            # 実質「出荷確定（梱包完了）日時」を指す。
            shipment.status = Shipment.Status.SHIPPED
            shipment.shipped_at = now
            shipment.inspected_by = request.user
            shipment.inspected_at = now
            # 検品・梱包作業の担当者ロックを解除
            shipment.in_progress_by = None
            shipment.in_progress_at = None
            shipment.save()

            order.status = OutboundOrder.Status.SHIPPED
            order.shipped_at = now
            order.save()

        messages.success(
            request,
            f'出荷指示 {order.outbound_order_code} の出荷検品が完了しました。'
            f'（出荷完了）',
        )
        return HttpResponseRedirect(reverse('outbound:handheld_inspection'))
