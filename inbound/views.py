import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, ProtectedError, Q, Sum
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, TemplateView, UpdateView

from masters.utils import get_current_warehouse

from .forms import InboundOrderForm, InboundOrderItemFormSet
from .models import InboundOrder


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
            qs = InboundOrder.objects.select_related('warehouse', 'supplier', 'created_by')
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
            if f['expected_from']:
                qs = qs.filter(expected_date__gte=f['expected_from'])
            if f['expected_to']:
                qs = qs.filter(expected_date__lte=f['expected_to'])
            qs = qs.annotate(
                item_count=Count('items'),
                total_expected=Sum('items__quantity_expected'),
            ).order_by('-expected_date', '-inbound_order_code')
            ctx['orders'] = qs
            ctx['stats'] = {
                'total': qs.count(),
                'pending': qs.filter(status=InboundOrder.Status.PENDING).count(),
                'arrived': qs.filter(status=InboundOrder.Status.ARRIVED).count(),
                'receiving': qs.filter(status=InboundOrder.Status.RECEIVING).count(),
                'completed': qs.filter(status=InboundOrder.Status.COMPLETED).count(),
            }
        else:
            ctx['orders'] = InboundOrder.objects.none()
            ctx['stats'] = None

        ctx['status_choices'] = InboundOrder.Status.choices
        ctx['source_type_choices'] = InboundOrder.SourceType.choices
        ctx['filters'] = f
        return ctx


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
        return HttpResponseRedirect(self.get_success_url())


class InboundOrderCreateView(_OrderFormMixin, LoginRequiredMixin, CreateView):
    model = InboundOrder
    success_url = reverse_lazy('inbound:order_inquiry')


class InboundOrderUpdateView(
    _OrderFormMixin, CurrentWarehouseScopedMixin, LoginRequiredMixin, UpdateView
):
    model = InboundOrder
    success_url = reverse_lazy('inbound:order_inquiry')

    def get_queryset(self):
        return super().get_queryset().select_related('warehouse', 'supplier')


class InboundOrderDeleteView(
    CurrentWarehouseScopedMixin, LoginRequiredMixin, ProtectedErrorMixin, DeleteView
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
