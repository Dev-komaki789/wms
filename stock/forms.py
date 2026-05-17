"""在庫操作系の Form。"""
from django import forms

from masters.models import Location, Sku
from masters.utils import get_current_warehouse

from .models import StockBalance


class _StockOperationForm(forms.Form):
    """計画外入庫 / 計画外出庫で共通する handheld 入力フォームの基底クラス。

    location_code / sku_code はバーコードスキャン or 手入力。
    入力時にマスタを引いて self._location / self._sku に解決して保持する。
    理由 (note) は手入力を避けるため Select 化し、選択肢は各サブクラスの
    REASON_CHOICES で定義する。
    """

    location_code = forms.CharField(
        label='ロケーション',
        max_length=30,
        widget=forms.TextInput(attrs={
            'class': 'form-control hh-key',
            'placeholder': '棚番をスキャン',
            'autocomplete': 'off',
            'autofocus': 'autofocus',
            'data-hh-lookup': 'location',
            'inputmode': 'text',
        }),
    )
    sku_code = forms.CharField(
        label='SKU',
        max_length=50,
        widget=forms.TextInput(attrs={
            'class': 'form-control hh-key',
            'placeholder': 'SKU をスキャン',
            'autocomplete': 'off',
            'data-hh-lookup': 'sku',
            'inputmode': 'text',
        }),
    )
    quantity = forms.IntegerField(
        label='数量',
        min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'form-control hh-key text-end',
            'placeholder': '0',
            'inputmode': 'numeric',
        }),
    )
    note = forms.ChoiceField(
        label='理由',
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
        }),
    )

    # 理由 Select の選択肢。サブクラスで上書きする。
    REASON_CHOICES = []

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._request = request
        self._location = None
        self._sku = None
        self.fields['note'].choices = self.REASON_CHOICES

    def clean_location_code(self):
        code = (self.cleaned_data.get('location_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        # 現在倉庫スコープで絞り込む（他倉庫のロケーションは対象外）
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


class UnplannedStockInForm(_StockOperationForm):
    """計画外入庫フォーム（handheld 端末用）。

    InboundOrder を介さず StockMovement.IN を直接発行し、在庫を加算する。
    """

    REASON_CHOICES = [
        ('', '— 理由を選択 —'),
        ('棚卸差異調整（多）', '棚卸差異調整（多）'),
        ('計画外仕入入庫', '計画外仕入入庫'),
        ('顧客返品入庫', '顧客返品入庫'),
        ('預かり品入庫', '預かり品入庫'),
        ('ロケ間移動の戻し', 'ロケ間移動の戻し'),
        ('その他', 'その他'),
    ]


class UnplannedStockOutForm(_StockOperationForm):
    """計画外出庫フォーム（handheld 端末用）。

    OutboundOrder を介さず StockMovement.OUT を直接発行し、在庫を減算する。
    在庫不足の出庫は clean() で弾く（StockBalance の quantity >= 0 制約に
    触れて IntegrityError になる前に、handheld 上でわかりやすく差し戻す）。
    """

    REASON_CHOICES = [
        ('', '— 理由を選択 —'),
        ('棚卸差異調整（少）', '棚卸差異調整（少）'),
        ('破損・廃棄', '破損・廃棄'),
        ('サンプル出庫', 'サンプル出庫'),
        ('社内使用', '社内使用'),
        ('預かり品引き取り', '預かり品引き取り'),
        ('ロケ間移動の持ち出し', 'ロケ間移動の持ち出し'),
        ('その他', 'その他'),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 出庫元ロケーション × SKU の在庫紐づきを即時チェックする。
        # 入庫と違い、出庫はその棚に在庫が無ければ実行できないため。
        self.fields['sku_code'].widget.attrs['data-hh-lookup'] = 'stock'
        self.fields['sku_code'].widget.attrs['data-hh-stock-loc'] = (
            'id_location_code'
        )

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get('quantity')
        # location_code / sku_code が個別 clean を通過していれば _location/_sku が入る
        if self._location and self._sku and quantity:
            balance = (
                StockBalance.objects
                .filter(location=self._location, sku=self._sku)
                .first()
            )
            on_hand = balance.quantity if balance else 0
            if quantity > on_hand:
                self.add_error(
                    'quantity',
                    f'在庫不足です。{self._location.location_code} の '
                    f'{self._sku.sku_code} 在庫は {on_hand} 個です。',
                )
        return cleaned


class StockTransferForm(forms.Form):
    """棚間移動フォーム（handheld 端末用）。

    移動元棚番・SKU・数量・移動先棚番をスキャン/入力する。マスタを引いて
    _from_location / _sku / _to_location に解決して保持し、移動元の在庫不足・
    移動元=移動先 は clean() で弾く。
    """

    from_location_code = forms.CharField(
        label='移動元ロケーション',
        max_length=30,
        widget=forms.TextInput(attrs={
            'class': 'form-control hh-key',
            'placeholder': '移動元の棚番をスキャン',
            'autocomplete': 'off',
            'autofocus': 'autofocus',
            'data-hh-lookup': 'location',
            'inputmode': 'text',
        }),
    )
    sku_code = forms.CharField(
        label='SKU',
        max_length=50,
        widget=forms.TextInput(attrs={
            'class': 'form-control hh-key',
            'placeholder': 'SKU をスキャン',
            'autocomplete': 'off',
            # 移動元ロケーション × SKU の在庫紐づきを即時チェックする
            'data-hh-lookup': 'stock',
            'data-hh-stock-loc': 'id_from_location_code',
            'inputmode': 'text',
        }),
    )
    quantity = forms.IntegerField(
        label='移動数',
        min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'form-control hh-key text-end',
            'placeholder': '0',
            'inputmode': 'numeric',
        }),
    )
    to_location_code = forms.CharField(
        label='移動先ロケーション',
        max_length=30,
        widget=forms.TextInput(attrs={
            'class': 'form-control hh-key',
            'placeholder': '移動先の棚番をスキャン',
            'autocomplete': 'off',
            'data-hh-lookup': 'location',
            'inputmode': 'text',
        }),
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._request = request
        self._from_location = None
        self._to_location = None
        self._sku = None

    def _resolve_location(self, code):
        """棚番コードを現在倉庫スコープ・有効なロケーションに解決する。"""
        qs = (
            Location.objects.select_related('warehouse', 'area')
            .filter(location_code=code, is_active=True)
        )
        wh = get_current_warehouse(self._request) if self._request else None
        if wh is not None:
            qs = qs.filter(warehouse=wh)
        return qs.first()

    def clean_from_location_code(self):
        code = (self.cleaned_data.get('from_location_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        self._from_location = self._resolve_location(code)
        if self._from_location is None:
            raise forms.ValidationError(
                f'棚番「{code}」は存在しないか、無効化されています。'
            )
        return code

    def clean_to_location_code(self):
        code = (self.cleaned_data.get('to_location_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        self._to_location = self._resolve_location(code)
        if self._to_location is None:
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

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get('quantity')
        # 移動元と移動先が同じロケーションなら移動の意味がない
        if (self._from_location and self._to_location
                and self._from_location.pk == self._to_location.pk):
            self.add_error(
                'to_location_code',
                '移動元と移動先が同じロケーションです。',
            )
        # 移動元の在庫不足を弾く（確定時にもロック付きで再検証する）
        if self._from_location and self._sku and quantity:
            balance = (
                StockBalance.objects
                .filter(location=self._from_location, sku=self._sku)
                .first()
            )
            on_hand = balance.quantity if balance else 0
            if quantity > on_hand:
                self.add_error(
                    'quantity',
                    f'在庫不足です。{self._from_location.location_code} の '
                    f'{self._sku.sku_code} 在庫は {on_hand} 個です。',
                )
        return cleaned
