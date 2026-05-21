from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, ProtectedError, Q, Sum
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import (
    CreateView, DeleteView, DetailView, TemplateView, UpdateView, View,
)

from core.order_csv import OrderCsvExportView, OrderCsvImportView
from core.utils import paginate, parse_query_date
from masters.models import Area, Sku
from stock.views import StocktakeLockGuardMixin
from masters.utils import get_current_warehouse
from stock.models import StockBalance, StockMovement

from .csv_io import OUTBOUND_ORDER_SPEC
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
            deadline_from = parse_query_date(f['deadline_from'])
            deadline_to = parse_query_date(f['deadline_to'])
            if deadline_from:
                qs = qs.filter(deadline_at__date__gte=deadline_from)
            if deadline_to:
                qs = qs.filter(deadline_at__date__lte=deadline_to)
            qs = qs.annotate(
                item_count=Count('items'),
                total_ordered=Sum('items__quantity_ordered'),
            ).order_by('-priority', 'deadline_at', '-outbound_order_code')
            page = paginate(self.request, qs)
            ctx['orders'] = page
            ctx['page_obj'] = page
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
    """SKU の引き当て可能な棚を返す（並び順は呼び出し側で選ぶ）。

    引き当て可能数 = StockBalance.quantity − 有効な引き当て(active)の合計。
    呼び出し側のトランザクション内で StockBalance 行をロックする。
    戻り値: [(Location, available_qty, first_received_at), ...]
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
            result.append((b.location, available, b.first_received_at))
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
    # 棚選択ロジック（ハイブリッド）:
    #   - 単一の棚で需要を満たせる場合: 在庫数の多い棚を1つ選ぶ
    #     （巡回棚数 1 で完結。1棚で足りる時に分割するのは作業効率上の損）
    #   - 単一棚では足りず複数棚に分割する場合: first_received_at の古い棚から
    #     順に取る（FIFO に近い動作。古い在庫の死蔵を防ぐ）
    # 将来検討（実装保留）:
    #   ① 純 FIFO: 単一棚で足りる場合も最古棚を優先（効率は落ちるが古い在庫を確実に減らす）
    #   ② 滞留在庫モニタ画面: 一定期間動いていない (棚×SKU) を可視化
    #     （StockBalance.first_received_at と現在日時の差で算出）
    plan = []  # [(item, [(location, qty), ...]), ...]
    for item in items:
        need = item.quantity_ordered
        locations = _available_locations(item.sku, order.warehouse)
        # 単一棚で需要を満たせるか
        single_enough = max((avail for _, avail, _ in locations), default=0) >= need
        if single_enough:
            # 在庫数の多い棚を 1 つだけ使う（巡回棚数 1）
            locations = sorted(locations, key=lambda t: t[1], reverse=True)
        else:
            # 分割が必要 → first_received_at 古い順（FIFO）。null は最後に
            locations = sorted(
                locations,
                key=lambda t: (t[2] is None, t[2]),
            )
        alloc = []
        for location, available, _ in locations:
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
    商品をスキャンで照合して実ピッキング数を入力する。1明細ごとに
    OutboundPickingItemView へ即時 POST（明細単位コミット = 中断耐性あり）。
    全明細が完了したらリストを COMPLETED にし、指示の全ピッキングリストが
    完了したら出荷指示を picking_wait → inspection_wait へ進める。
    在庫の減算は後工程「出荷検品」で行うため、この画面では在庫を動かさない。

    GET 時は picked_at がまだ未設定の明細のみを画面に出す（途中離脱後の
    再入場で「続きから」できる）。確定済みは再編集不可。
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
        # 1商品ずつ処理する handheld ウィザード用に、未ピッキング明細だけを
        # 棚番順の JSON で渡す（picked_at が設定された明細は前セッションで処理済み）
        items = []
        done_count = 0
        total = 0
        for it in (picking_list.items
                   .select_related('sku__product', 'location')
                   .order_by('sort_order')):
            total += 1
            if it.picked_at is not None:
                done_count += 1
                continue
            items.append({
                'id': it.pk,
                'location': it.location.location_code,
                'sku': it.sku.sku_code,
                'jan': it.sku.jan_code or '',
                'name': it.sku.product.product_name,
                'requested': it.quantity_requested,
            })
        return render(request, self.template_name, {
            'picking_list': picking_list,
            'items': items,
            'done_count': done_count,
            'total': total,
            'outbound_order': _order_of_picking_list(picking_list),
        })

class OutboundPickingItemView(LoginRequiredMixin, View):
    """ピッキングの 1 明細を即時確定する API（fetch 用）。

    POST: quantity_picked (0 〜 quantity_requested) を受け取り、PickingListItem に
    実ピッキング数・担当者・完了日時・status (picked/short) を記録する。
    全明細が完了したら PickingList を COMPLETED に進め、さらに同指示の全
    ピッキングリストが完了していれば OutboundOrder を picking_wait →
    inspection_wait に進める。

    レスポンス (JSON):
      - ok=True  -> {'ok': True, 'all_done': bool, 'remaining': int}
      - ok=False -> {'ok': False, 'error': str}
    """

    http_method_names = ['post']

    def _scoped_lists(self, request):
        qs = PickingList.objects.select_related(
            'warehouse', 'area', 'assigned_to')
        wh = get_current_warehouse(request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    @staticmethod
    def _to_int(raw):
        try:
            return int((raw or '').strip())
        except ValueError:
            return -1

    def post(self, request, list_pk, item_pk, *args, **kwargs):
        picked = self._to_int(request.POST.get('quantity_picked'))
        with transaction.atomic():
            picking_list = (
                self._scoped_lists(request)
                .select_for_update(of=('self',))
                .filter(pk=list_pk)
                .first()
            )
            if picking_list is None:
                return JsonResponse({
                    'ok': False, 'error': 'ピッキングリストが見つかりません。',
                })
            if picking_list.status != PickingList.Status.IN_PROGRESS:
                return JsonResponse({
                    'ok': False,
                    'error': f'ピッキングリスト {picking_list.picking_list_code} は'
                             f'「{picking_list.get_status_display()}」のためピッキングできません。',
                })
            if (picking_list.assigned_to_id
                    and picking_list.assigned_to_id != request.user.pk):
                worker = (picking_list.assigned_to.display_name
                          or picking_list.assigned_to.username)
                return JsonResponse({
                    'ok': False, 'error': f'{worker} がピッキング作業中です。',
                })

            item = (picking_list.items
                    .select_related('sku').filter(pk=item_pk).first())
            if item is None:
                return JsonResponse({
                    'ok': False, 'error': '対象の明細が見つかりません。',
                })
            if item.picked_at is not None:
                return JsonResponse({
                    'ok': False,
                    'error': f'{item.sku.sku_code} は既にピッキング済みです。',
                })
            if picked < 0 or picked > item.quantity_requested:
                return JsonResponse({
                    'ok': False,
                    'error': f'実ピッキング数は 0 〜 {item.quantity_requested} の範囲で入力してください。',
                })

            now = timezone.now()
            item.quantity_picked = picked
            item.status = (
                PickingListItem.Status.PICKED
                if picked >= item.quantity_requested
                else PickingListItem.Status.SHORT
            )
            item.picked_by = request.user
            item.picked_at = now
            item.save()

            # 全明細が picked_at を持ったらリストを COMPLETED に
            remaining = picking_list.items.filter(picked_at__isnull=True).count()
            all_done = (remaining == 0)
            if all_done:
                picking_list.status = PickingList.Status.COMPLETED
                picking_list.completed_at = now
                picking_list.save()
                # 同一指示の別リスト同時完了でも取りこぼさないよう、先に指示行を
                # ロックしてから「全リスト完了か」を判定する（既存ロジックと同じガード）
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

        return JsonResponse({
            'ok': True, 'all_done': all_done, 'remaining': remaining,
        })


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


class OutboundInspectionWorkView(StocktakeLockGuardMixin, LoginRequiredMixin, View):
    """出荷検品・梱包作業（出荷確定）画面 — handheld 端末用。

    出荷検品画面から遷移。出荷する明細を1商品ずつ SKU スキャンで照合し、品番・員数を
    最終確認する。1明細ごとに OutboundInspectionItemView へ即時 POST して、
    在庫減算・StockMovement.OUT 発行・引き当て解放・inspected_at 設定までを
    1トランザクションでコミット（中断耐性あり）。全明細の検品が終わったら
    Shipment.SHIPPED / OutboundOrder.SHIPPED に進めて ShipmentItem を SKU 単位で
    集約作成する。実出荷数はピッキング工程で確定済みの quantity_picked を用いる。

    GET 時は inspected_at がまだ未設定の明細のみを画面に渡す（途中離脱後の再入場で
    続きから）。確定済みは再編集不可（業務 WMS の慣習に従う）。

    【簡略化（意図的）】検品・梱包完了をもって「出荷完了(shipped)」とみなす。実工程は
    検品→梱包→送り状貼付→出荷待機→トラック積込→出発で、本来の出荷完了はトラック出発
    （carrier 引き渡し）の時点。本 MVP は出荷バース管理（積込・出発）をスコープ外とし、
    検品完了を出荷確定とみなす。
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
        # 1商品ずつ処理する handheld ウィザード用に、未検品の明細だけを JSON で渡す
        picked_map = _picked_qty_map(order)
        rows = _inspection_items(order, picked_map)
        items = []
        done_count = 0
        total = 0
        for row, oo_item in zip(rows, order.items.order_by('sku__sku_code').all()):
            total += 1
            if oo_item.inspected_at is not None:
                done_count += 1
                continue
            # JSON に出すのは未検品のものだけ
            items.append(row)
        return render(request, self.template_name, {
            'order': order, 'shipment': shipment, 'items': items,
            'done_count': done_count, 'total': total,
        })


class OutboundInspectionItemView(StocktakeLockGuardMixin, LoginRequiredMixin, View):
    """出荷検品の 1 明細を即時確定する API（fetch 用）。

    POST: 明細単位で在庫減算・StockMovement.OUT 発行・引き当て解放・
    inspected_at セットまでを 1 トランザクションで行う。
    picked == 0（欠品で出荷対象外）の明細は在庫を動かさず inspected_at だけ
    セットする（フロントから自動で skip 用に呼ばれる）。
    全明細が inspected_at を持ったら、ShipmentItem を SKU 単位で集約作成し
    Shipment.SHIPPED / OutboundOrder.SHIPPED に進める。

    レスポンス (JSON):
      - ok=True  -> {'ok': True, 'all_done': bool, 'remaining': int}
      - ok=False -> {'ok': False, 'error': str}
    """

    http_method_names = ['post']

    def _scoped_orders(self, request):
        qs = OutboundOrder.objects.select_related('warehouse', 'customer')
        wh = get_current_warehouse(request)
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs

    def post(self, request, order_pk, item_pk, *args, **kwargs):
        with transaction.atomic():
            order = (
                self._scoped_orders(request)
                .select_for_update(of=('self',))
                .filter(pk=order_pk)
                .first()
            )
            if order is None:
                return JsonResponse({
                    'ok': False, 'error': '出荷指示が見つかりません。',
                })
            if order.status != OutboundOrder.Status.INSPECTION_WAIT:
                return JsonResponse({
                    'ok': False,
                    'error': f'出荷指示 {order.outbound_order_code} は'
                             f'「{order.get_status_display()}」のため検品できません。',
                })
            shipment = (
                Shipment.objects
                .select_for_update(of=('self',))
                .select_related('in_progress_by')
                .filter(outbound_order=order).first()
            )
            if shipment is None or shipment.in_progress_by_id != request.user.pk:
                return JsonResponse({
                    'ok': False,
                    'error': '検品作業のロックが取れていません。先に「検品開始」を行ってください。',
                })

            item = (
                order.items
                .select_related('sku', 'location', 'reservation')
                .filter(pk=item_pk).first()
            )
            if item is None:
                return JsonResponse({
                    'ok': False, 'error': '対象の明細が見つかりません。',
                })
            if item.inspected_at is not None:
                return JsonResponse({
                    'ok': False,
                    'error': f'{item.sku.sku_code} は既に検品済みです。',
                })

            picked_map = _picked_qty_map(order)
            picked = picked_map.get(item.pk, 0)

            now = timezone.now()
            if picked > 0:
                # 在庫不足ガード（select_for_update で行ロック）
                balance = (
                    StockBalance.objects.select_for_update()
                    .filter(location=item.location, sku=item.sku)
                    .first()
                )
                on_hand = balance.quantity if balance else 0
                if picked > on_hand:
                    loc = item.location.location_code if item.location else '—'
                    return JsonResponse({
                        'ok': False,
                        'error': f'{item.sku.sku_code} は棚番 {loc} の在庫'
                                 f'（{on_hand}）が実出荷数（{picked}）に不足しています。'
                                 f'在庫差異を解消してから再度お試しください。',
                    })
                quantity_before = balance.quantity
                quantity_after = quantity_before - picked
                StockMovement.objects.create(
                    movement_type=StockMovement.MovementType.OUT,
                    location=item.location, sku=item.sku,
                    quantity=-picked,  # OUT は負の値で記録
                    quantity_before=quantity_before,
                    quantity_after=quantity_after,
                    reference_type=StockMovement.ReferenceType.OUTBOUND_ORDER,
                    reference_id=order.pk,
                    note='',
                    created_by=request.user,
                )
                balance.quantity = quantity_after
                balance.save()
                item.quantity_shipped = picked
                # この明細に紐づく引き当てを解放
                if item.reservation_id and item.reservation.status == StockReservation.Status.ACTIVE:
                    item.reservation.status = StockReservation.Status.RELEASED
                    item.reservation.released_at = now
                    item.reservation.save(update_fields=['status', 'released_at', 'updated_at'])

            item.inspected_at = now
            item.inspected_by = request.user
            item.save()

            # 全明細が inspected_at を持ったら出荷確定（ShipmentItem 集約・Shipment / Order 遷移）
            remaining = order.items.filter(inspected_at__isnull=True).count()
            all_done = (remaining == 0)
            if all_done:
                self._finalize_shipment(order, shipment, request.user, now)

        return JsonResponse({
            'ok': True, 'all_done': all_done, 'remaining': remaining,
        })

    def _finalize_shipment(self, order, shipment, user, now):
        """全明細検品完了時の最終処理: ShipmentItem 集約 + Shipment/Order 遷移。"""
        # 念のため残っている ACTIVE 引き当ても解放（per-item で取り逃した分の保険）
        StockReservation.objects.filter(
            order=order, status=StockReservation.Status.ACTIVE,
        ).update(status=StockReservation.Status.RELEASED, released_at=now)

        # ShipmentItem は uk(shipment, sku) のため SKU 単位に集約する
        by_sku = {}
        for it in order.items.select_related('sku').all():
            qty = it.quantity_shipped or 0
            if qty <= 0:
                continue
            agg = by_sku.setdefault(
                it.sku_id, {'sku': it.sku, 'ooi': it, 'qty': 0})
            agg['qty'] += qty
        for agg in by_sku.values():
            if agg['qty'] <= 0:
                continue
            ShipmentItem.objects.create(
                shipment=shipment,
                outbound_order_item=agg['ooi'],
                sku=agg['sku'],
                quantity_shipped=agg['qty'],
            )

        # 出荷確定（簡略化: 検品完了 = 出荷完了。詳細は WorkView の docstring 参照）
        shipment.status = Shipment.Status.SHIPPED
        shipment.shipped_at = now
        shipment.inspected_by = user
        shipment.inspected_at = now
        shipment.in_progress_by = None
        shipment.in_progress_at = None
        shipment.save()

        order.status = OutboundOrder.Status.SHIPPED
        order.shipped_at = now
        order.save()


# ---- 出荷指示 CSV 入出力 ----

class OutboundOrderCsvExportView(OrderCsvExportView):
    """出荷指示を CSV（ヘッダ＋明細フラット様式）でエクスポートする。"""
    spec = OUTBOUND_ORDER_SPEC


class OutboundOrderCsvImportView(OrderCsvImportView):
    """出荷指示を CSV でインポートする（新規作成のみ）。"""
    spec = OUTBOUND_ORDER_SPEC
