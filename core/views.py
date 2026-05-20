from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, TemplateView

from .models import ErrorLog
from .utils import paginate, parse_query_date


class ErrorLogInquiryView(LoginRequiredMixin, TemplateView):
    """エラーログ照会画面（PC）。検索-first パターン（マスタ照会と同じ規約）。

    画面例外エラー（exception）と上位システムの取り込みエラー（import）を、
    種別・対応状況・期間・キーワードで絞り込んで一覧する。
    """

    template_name = 'a/core/error_log_inquiry.html'

    SEARCH_KEYS = ('q', 'error_type', 'resolved', 'date_from', 'date_to')

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
                exception=Count(
                    'id', filter=Q(error_type=ErrorLog.ErrorType.EXCEPTION)),
                imported=Count(
                    'id', filter=Q(error_type=ErrorLog.ErrorType.IMPORT)),
                unresolved=Count('id', filter=Q(is_resolved=False)),
            )
            page = paginate(self.request, qs.order_by('-occurred_at'))
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
        return HttpResponseRedirect(
            reverse('core:error_log_detail', args=[log.pk]))
