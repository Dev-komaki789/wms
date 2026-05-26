"""出荷指示の Form / FormSet。

出荷指示番号は WMS 内部の管理番号（OO/RO prefix のみ許可）。
OMS 連携の注文番号は external_order_id に別途記録する。
明細は需要（SKU × 数量）のみ。ピッキング元ロケーションと在庫引き当ては
出荷起動工程で確定するため、登録画面では入力しない。
"""
import re

from django import forms
from django.core.validators import MaxValueValidator
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from masters.models import Customer, Sku

from .models import OutboundOrder, OutboundOrderItem

TEXT = {'class': 'form-control'}
SELECT = {'class': 'form-select'}

# 画面登録は通常出荷（source_type=manual / OO prefix）のみ。
# 返品出荷（return / RO prefix）は「着払い等の特殊ケース」用の区分で画面スコープ外（将来対応）。
OUTBOUND_CODE_PATTERN = re.compile(r'^OO-\d{8}-\d{3}$')


class OutboundOrderForm(forms.ModelForm):
    class Meta:
        model = OutboundOrder
        # status / source_type / shipped_at / cancelled_* は業務アクション・システム由来のため
        # フォーム外。画面登録は通常出荷（source_type=manual）に固定し、ビューで設定する。
        fields = [
            'outbound_order_code',
            'customer',
            'external_order_id',
            'deadline_at',
            'delivery_postal_code',
            'delivery_address',
            'delivery_name',
            'note',
        ]
        widgets = {
            'outbound_order_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: OO-20260517-001',
                       'autocomplete': 'off', 'maxlength': '15',
                       'pattern': r'OO-\d{8}-\d{3}',
                       'title': 'OO-YYYYMMDD-NNN 形式'}
            ),
            # 顧客は検索モーダルで選択する（テンプレ側の独自UI＋hidden で送信）
            'customer': forms.HiddenInput(),
            'external_order_id': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: OMS-2026-000123',
                       'autocomplete': 'off'}
            ),
            'deadline_at': forms.DateTimeInput(
                attrs={**TEXT, 'type': 'datetime-local'},
                format='%Y-%m-%dT%H:%M',
            ),
            'delivery_postal_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: 100-0001', 'autocomplete': 'off',
                       'inputmode': 'numeric', 'maxlength': '8'}
            ),
            'delivery_address': forms.TextInput(attrs={**TEXT, 'autocomplete': 'off'}),
            'delivery_name': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: 株式会社○○ 御中', 'autocomplete': 'off'}
            ),
            'note': forms.Textarea(attrs={**TEXT, 'rows': '2'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 顧客: 有効なものだけ。編集時に現在の顧客が無効化されていても残す
        cus_qs = Customer.objects.filter(is_active=True).order_by('customer_code')
        if self.instance.pk and self.instance.customer_id:
            cus_qs = Customer.objects.filter(
                pk__in=list(cus_qs.values_list('pk', flat=True)) + [self.instance.customer_id]
            ).order_by('customer_code')
        self.fields['customer'].queryset = cus_qs
        self.fields['customer'].empty_label = '— 顧客を選択 —'
        # 画面登録は通常出荷のみなので顧客は必須
        self.fields['customer'].required = True

        # 出荷期限: datetime-local 入力。任意（OMS 連携では設定されるが手動登録では空可）
        self.fields['deadline_at'].input_formats = ['%Y-%m-%dT%H:%M']
        self.fields['deadline_at'].required = False

        # 桁数を実形式に絞る（model max_length の auto-maxlength を上書き）
        # 出荷指示番号「OO-YYYYMMDD-NNN」= 15 桁、配送先郵便番号は 8 桁（例 100-0001）
        self.fields['outbound_order_code'].widget.attrs['maxlength'] = '15'
        self.fields['delivery_postal_code'].widget.attrs['maxlength'] = '8'

        if self.instance.pk:
            # 編集時は outbound_order_code を変更不可（伝票番号は文書の identity）
            self.fields['outbound_order_code'].disabled = True
        elif not self.data:
            # 新規登録: 次の出荷指示番号（OO-YYYYMMDD-NNN）を初期表示
            self.fields['outbound_order_code'].initial = OutboundOrder.next_code(
                timezone.localdate(), OutboundOrder.SourceType.MANUAL.value
            )

    def clean_outbound_order_code(self):
        """outbound_order_code を「OO-YYYYMMDD-NNN」形式に制限。"""
        code = (self.cleaned_data.get('outbound_order_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        if not OUTBOUND_CODE_PATTERN.match(code):
            raise forms.ValidationError(
                '出荷指示番号は「OO-YYYYMMDD-NNN」形式で入力してください。'
            )
        return code


class OutboundOrderItemForm(forms.ModelForm):
    """出荷指示明細フォーム（需要 = SKU × 数量）。

    SKU は大量登録される想定なので select ではなくテキスト入力。
    バーコードスキャン or 手入力に対応し、🔍 ボタンの SKU マスタ検索モーダルから選択も可能。
    ピッキング元ロケーション・在庫引き当ては出荷起動工程で確定するため、ここでは扱わない。
    """

    sku_code = forms.CharField(
        label='SKU',
        max_length=13,
        widget=forms.TextInput(attrs={
            'class': 'form-control font-monospace',
            'placeholder': 'SKU コード',
            'autocomplete': 'off',
        }),
    )

    class Meta:
        model = OutboundOrderItem
        # sku は sku_code テキスト経由で resolve。location/reservation は出荷起動で確定。
        fields = ['quantity_ordered']
        widgets = {
            'quantity_ordered': forms.NumberInput(
                attrs={**TEXT, 'class': 'form-control text-end',
                       'min': '1', 'max': '99999', 'placeholder': '数量'}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sku = None
        # 出荷指示数は 5 桁まで（〜99,999）
        self.fields['quantity_ordered'].validators.append(MaxValueValidator(99999))
        # 編集時: 既存 SKU のコードを pre-fill
        if self.instance.pk and self.instance.sku_id:
            self.fields['sku_code'].initial = self.instance.sku.sku_code

    def clean_sku_code(self):
        """sku_code を SKU マスタで引いて Sku インスタンスを self._sku に保持。"""
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

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self._sku is not None:
            instance.sku = self._sku
        if commit:
            instance.save()
        return instance


class BaseOutboundOrderItemFormSet(BaseInlineFormSet):
    """同一出荷指示内で同じ SKU が重複していないか検証する。

    モデルの UniqueConstraint(outbound_order, sku, location) は location が違えば
    同一 SKU を許すが、登録時点では location=NULL のため画面では SKU 重複を禁止する。
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        seen = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            code = form.cleaned_data.get('sku_code')
            if not code:
                continue
            if code in seen:
                form.add_error(
                    'sku_code', f'SKU「{code}」が同じ出荷指示内で重複しています。'
                )
            else:
                seen.add(code)


OutboundOrderItemFormSet = inlineformset_factory(
    OutboundOrder,
    OutboundOrderItem,
    form=OutboundOrderItemForm,
    formset=BaseOutboundOrderItemFormSet,
    extra=3,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
