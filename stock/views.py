from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q, Sum
from django.urls import reverse_lazy
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from masters.models import Area

from .forms import UnplannedStockInForm
from .models import StockBalance, StockMovement


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
            ctx['balances'] = qs
            ctx['stats'] = {
                'rows': qs.count(),
                'total_qty': qs.aggregate(s=Sum('quantity'))['s'] or 0,
                'in_stock': qs.filter(quantity__gt=0).count(),
                'zero': qs.filter(quantity=0).count(),
                'sku_count': qs.values('sku').distinct().count(),
            }
        else:
            ctx['balances'] = StockBalance.objects.none()
            ctx['stats'] = None

        ctx['area_types'] = Area.LocationType.choices
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
            balance, _ = StockBalance.objects.get_or_create(
                location=location, sku=sku,
                defaults={'quantity': 0},
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
