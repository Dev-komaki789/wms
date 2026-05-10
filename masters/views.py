from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .models import Warehouse


class ProtectedErrorMixin:
    """DeleteView で PROTECT FK エラーをキャッチし、メッセージで通知して一覧に戻す。"""

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except ProtectedError as e:
            count = len(e.protected_objects)
            sample = ', '.join(str(o) for o in list(e.protected_objects)[:3])
            messages.error(
                request,
                f'削除できません: 関連データが {count} 件紐づいています（{sample}{"..." if count > 3 else ""}）。'
                f'先に関連データを削除または別の親に付け替えてください。',
            )
            return HttpResponseRedirect(self.success_url)


# ---- Warehouse ----

class WarehouseListView(LoginRequiredMixin, ListView):
    model = Warehouse
    template_name = 'a/masters/warehouse_list.html'
    context_object_name = 'warehouses'
    ordering = ['warehouse_code']


class WarehouseCreateView(LoginRequiredMixin, CreateView):
    model = Warehouse
    template_name = 'a/masters/warehouse_form.html'
    fields = ['warehouse_code', 'warehouse_name', 'address', 'is_active']
    success_url = reverse_lazy('masters:warehouse_list')


class WarehouseUpdateView(LoginRequiredMixin, UpdateView):
    model = Warehouse
    template_name = 'a/masters/warehouse_form.html'
    fields = ['warehouse_code', 'warehouse_name', 'address', 'is_active']
    success_url = reverse_lazy('masters:warehouse_list')


class WarehouseDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Warehouse
    template_name = 'a/masters/warehouse_confirm_delete.html'
    success_url = reverse_lazy('masters:warehouse_list')
