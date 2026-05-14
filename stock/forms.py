"""在庫操作系の Form。"""
from django import forms

from masters.models import Location, Sku
from masters.utils import get_current_warehouse


class UnplannedStockInForm(forms.Form):
    """計画外入庫フォーム（handheld 端末用）。

    location_code / sku_code はバーコードスキャン or 手入力。
    入力時にマスタを引いて self._location / self._sku に格納する。
    """

    location_code = forms.CharField(
        label='ロケーション',
        max_length=30,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg font-monospace',
            'placeholder': '棚番をスキャン',
            'autocomplete': 'off',
            'autofocus': 'autofocus',
            'inputmode': 'text',
        }),
    )
    sku_code = forms.CharField(
        label='SKU',
        max_length=50,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg font-monospace',
            'placeholder': 'SKU をスキャン',
            'autocomplete': 'off',
            'inputmode': 'text',
        }),
    )
    quantity = forms.IntegerField(
        label='数量',
        min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'form-control form-control-lg text-end font-monospace',
            'placeholder': '0',
            'inputmode': 'numeric',
        }),
    )
    note = forms.CharField(
        label='備考',
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '理由など（任意）',
            'autocomplete': 'off',
        }),
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._request = request
        self._location = None
        self._sku = None

    def clean_location_code(self):
        code = (self.cleaned_data.get('location_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        # 現在倉庫スコープで絞り込む（他倉庫のロケーションには入庫させない）
        qs = (
            Location.objects.select_related('warehouse', 'area')
            .filter(location_code=code, is_active=True)
        )
        wh = get_current_warehouse(self._request) if self._request else None
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        try:
            self._location = qs.get()
        except Location.DoesNotExist:
            raise forms.ValidationError(
                f'棚番「{code}」は存在しないか、無効化されています。'
            )
        return code

    def clean_sku_code(self):
        code = (self.cleaned_data.get('sku_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        try:
            self._sku = (
                Sku.objects.select_related('product')
                .get(sku_code=code, is_active=True)
            )
        except Sku.DoesNotExist:
            raise forms.ValidationError(
                f'SKU「{code}」は存在しないか、無効化されています。'
            )
        return code
