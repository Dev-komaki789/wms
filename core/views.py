from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, TemplateView

from masters.models import Location, Sku
from masters.utils import get_current_warehouse
from outbound.models import OutboundOrder, PickingList
from outbound.utils import code128_svg

from .models import ErrorLog
from .utils import apply_ordering, paginate, parse_query_date


class ErrorLogInquiryView(LoginRequiredMixin, TemplateView):
    """エラーログ照会画面（PC）。検索-first パターン（マスタ照会と同じ規約）。

    画面例外エラー（exception）と上位システムの取り込みエラー（import）を、
    種別・対応状況・期間・キーワードで絞り込んで一覧する。
    """

    template_name = 'a/core/error_log_inquiry.html'

    SEARCH_KEYS = ('q', 'error_type', 'resolved', 'date_from', 'date_to')

    SORTABLE = {
        'occurred': ['occurred_at'],
        'type': ['error_type'],
        'summary': ['summary'],
        'source': ['source'],
        'user': ['user__username'],
        'resolved': ['is_resolved'],
    }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET

        searched = any(k in g for k in self.SEARCH_KEYS)
        ctx['searched'] = searched

        f = {
            'q': g.get('q', '').strip(),
            'error_type': g.get('error_type', ''),
            'resolved': g.get('resolved', ''),
            'date_from': g.get('date_from', ''),
            'date_to': g.get('date_to', ''),
        }

        if searched:
            qs = ErrorLog.objects.select_related('user')
            if f['q']:
                qs = qs.filter(
                    Q(summary__icontains=f['q'])
                    | Q(source__icontains=f['q'])
                    | Q(reference__icontains=f['q'])
                )
            if f['error_type']:
                qs = qs.filter(error_type=f['error_type'])
            if f['resolved'] == 'resolved':
                qs = qs.filter(is_resolved=True)
            elif f['resolved'] == 'unresolved':
                qs = qs.filter(is_resolved=False)
            date_from = parse_query_date(f['date_from'])
            date_to = parse_query_date(f['date_to'])
            if date_from:
                qs = qs.filter(occurred_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(occurred_at__date__lte=date_to)
            agg = qs.aggregate(
                total=Count('id'),
                exception=Count('id', filter=Q(error_type=ErrorLog.ErrorType.EXCEPTION)),
                imported=Count('id', filter=Q(error_type=ErrorLog.ErrorType.IMPORT)),
                unresolved=Count('id', filter=Q(is_resolved=False)),
            )
            qs, ctx['sort'], ctx['dir'] = apply_ordering(
                self.request, qs, self.SORTABLE, 'occurred', 'desc'
            )
            page = paginate(self.request, qs)
            ctx['logs'] = page
            ctx['page_obj'] = page
            ctx['stats'] = {
                'total': agg['total'],
                'exception': agg['exception'],
                'import': agg['imported'],
                'unresolved': agg['unresolved'],
            }
        else:
            ctx['logs'] = ErrorLog.objects.none()
            ctx['stats'] = None

        ctx['error_type_choices'] = ErrorLog.ErrorType.choices
        ctx['filters'] = f
        return ctx


class ErrorLogDetailView(LoginRequiredMixin, DetailView):
    """エラーログ詳細画面（PC）。

    トレースバック・取り込み失敗データなどの詳細を表示し、運用者が確認したら
    「対応済み」に切り替えられる（POST）。
    """

    model = ErrorLog
    template_name = 'a/core/error_log_detail.html'
    context_object_name = 'log'

    def get_queryset(self):
        return super().get_queryset().select_related('user')

    def post(self, request, *args, **kwargs):
        """対応済み / 未対応 を切り替える。"""
        log = self.get_object()
        if log.is_resolved:
            log.is_resolved = False
            log.resolved_at = None
            messages.info(request, 'エラーログを「未対応」に戻しました。')
        else:
            log.is_resolved = True
            log.resolved_at = timezone.now()
            messages.success(request, 'エラーログを「対応済み」にしました。')
        log.save(update_fields=['is_resolved', 'resolved_at'])
        return HttpResponseRedirect(reverse('core:error_log_detail', args=[log.pk]))


class BarcodePrintView(LoginRequiredMixin, TemplateView):
    """デモ用バーコード印刷画面。

    棚番／SKU／ピッキングNo／出荷No を Code128 でカード型に並べた A4 印刷
    シートを生成する。バーコード SVG は outbound.utils.code128_svg を流用。

    GET ?kind なし → 種類選択画面（同テンプレ内の選択フォーム）
    GET ?kind=... → 印刷シート（カード型 2x5=10枚/A4）

    管理者向けデモ機能。is_handheld_worker には見せない（メニュー側で制御）。
    現在倉庫スコープがあれば棚番だけそれで絞る。SKU や伝票は倉庫を跨ぐので絞らない。
    """

    template_name = 'a/core/barcode_print.html'

    KIND_CHOICES = (
        ('location', '棚番（ロケーション）'),
        ('sku', 'SKU'),
        ('picking', 'ピッキングNo'),
        ('outbound', '出荷No'),
    )
    LIMIT_CHOICES = (10, 20, 50, 100)
    DEFAULT_LIMIT = 20

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        kind = g.get('kind', '')
        try:
            limit = int(g.get('limit', self.DEFAULT_LIMIT))
        except ValueError:
            limit = self.DEFAULT_LIMIT
        limit = max(1, min(limit, 200))  # 上限ガード（200枚 = 20 ページまで）

        ctx['kind'] = kind
        ctx['limit'] = limit
        ctx['kind_choices'] = self.KIND_CHOICES
        ctx['limit_choices'] = self.LIMIT_CHOICES
        ctx['print_mode'] = kind in dict(self.KIND_CHOICES)
        ctx['items'] = []

        if not ctx['print_mode']:
            return ctx

        # 種類別にコードと補助情報を1リストに揃える。テンプレ側は dict 共通形式で
        # 描画する（code / svg / subtitle）。
        items = []
        wh = get_current_warehouse(self.request)

        if kind == 'location':
            qs = Location.objects.select_related('area').filter(is_active=True)
            if wh is not None:
                qs = qs.filter(warehouse=wh)
            for loc in qs.order_by('location_code')[:limit]:
                items.append(
                    {
                        'code': loc.location_code,
                        'svg': code128_svg(loc.location_code),
                        'subtitle': loc.area.area_name or loc.area.area_code,
                    }
                )
        elif kind == 'sku':
            qs = Sku.objects.select_related('product').filter(is_active=True)
            for sku in qs.order_by('sku_code')[:limit]:
                parts = [sku.product.product_name]
                if sku.color_info:
                    parts.append(sku.color_info)
                if sku.size_info:
                    parts.append(sku.size_info)
                items.append(
                    {
                        'code': sku.sku_code,
                        'svg': code128_svg(sku.sku_code),
                        'subtitle': ' / '.join(parts),
                    }
                )
        elif kind == 'picking':
            qs = PickingList.objects.select_related('order').order_by('-picking_list_code')[:limit]
            for pl in qs:
                items.append(
                    {
                        'code': pl.picking_list_code,
                        'svg': code128_svg(pl.picking_list_code),
                        'subtitle': pl.order.outbound_order_code if pl.order_id else '',
                    }
                )
        elif kind == 'outbound':
            qs = OutboundOrder.objects.select_related('customer').order_by('-outbound_order_code')[
                :limit
            ]
            for order in qs:
                items.append(
                    {
                        'code': order.outbound_order_code,
                        'svg': code128_svg(order.outbound_order_code),
                        'subtitle': order.customer.customer_name if order.customer_id else '',
                    }
                )

        ctx['items'] = items
        return ctx
