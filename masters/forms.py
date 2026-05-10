"""マスタ画面用 ModelForm 群。

ウィジェットに Bootstrap 5 のクラスを付与する。
これにより各フォームテンプレートは `{{ field }}` を直接書くだけでよい。
"""
from django import forms

from .models import Area, Warehouse
from .widgets import StatusToggleWidget

TEXT = {'class': 'form-control'}
SELECT = {'class': 'form-select'}
STATUS_LABEL = 'ステータス'


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ['warehouse_code', 'warehouse_name', 'address', 'is_active']
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'warehouse_code': forms.TextInput(attrs=TEXT),
            'warehouse_name': forms.TextInput(attrs=TEXT),
            'address': forms.TextInput(attrs=TEXT),
            'is_active': StatusToggleWidget(),
        }


class AreaForm(forms.ModelForm):
    class Meta:
        model = Area
        fields = ['warehouse', 'area_code', 'area_name', 'is_active']
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'warehouse': forms.Select(attrs=SELECT),
            'area_code': forms.TextInput(attrs=TEXT),
            'area_name': forms.TextInput(attrs=TEXT),
            'is_active': StatusToggleWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['warehouse'].queryset = (
            Warehouse.objects.all().order_by('warehouse_code')
        )
        self.fields['warehouse'].empty_label = '— 倉庫を選択 —'

    def clean(self):
        """warehouse + area_code の重複を ModelForm レベルでも捕捉。

        DB のユニーク制約 (uk_areas_warehouse_code) でも防げるが、
        フォームで先に弾くと UX が良い。
        """
        cleaned = super().clean()
        warehouse = cleaned.get('warehouse')
        area_code = cleaned.get('area_code')
        if warehouse and area_code:
            qs = Area.objects.filter(warehouse=warehouse, area_code=area_code)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error(
                    'area_code',
                    f'倉庫「{warehouse.warehouse_code}」内に同じエリアコードが既に存在します。',
                )
        return cleaned
