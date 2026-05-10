from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import AreaForm, WarehouseForm
from .models import Area, Warehouse


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
    form_class = WarehouseForm
    template_name = 'a/masters/warehouse_form.html'
    success_url = reverse_lazy('masters:warehouse_list')


class WarehouseUpdateView(LoginRequiredMixin, UpdateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = 'a/masters/warehouse_form.html'
    success_url = reverse_lazy('masters:warehouse_list')


class WarehouseDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Warehouse
    template_name = 'a/masters/warehouse_confirm_delete.html'
    success_url = reverse_lazy('masters:warehouse_list')


# ---- Area ----

class AreaListView(LoginRequiredMixin, ListView):
    model = Area
    template_name = 'a/masters/area_list.html'
    context_object_name = 'areas'

    def get_queryset(self):
        return (
            Area.objects.select_related('warehouse')
            .order_by('warehouse__warehouse_code', 'area_code')
        )


class AreaCreateView(LoginRequiredMixin, CreateView):
    model = Area
    form_class = AreaForm
    template_name = 'a/masters/area_form.html'
    success_url = reverse_lazy('masters:area_list')


class AreaUpdateView(LoginRequiredMixin, UpdateView):
    model = Area
    form_class = AreaForm
    template_name = 'a/masters/area_form.html'
    success_url = reverse_lazy('masters:area_list')


class AreaDeleteView(LoginRequiredMixin, ProtectedErrorMixin, DeleteView):
    model = Area
    template_name = 'a/masters/area_confirm_delete.html'
    success_url = reverse_lazy('masters:area_list')

    def get_queryset(self):
        return Area.objects.select_related('warehouse')
