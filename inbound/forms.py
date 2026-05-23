"""入荷指示の Form / FormSet。

入荷指示番号は WMS 内部の管理番号（IO/RT prefix のみ許可）。
上位システム連携の番号は purchase_order_code / supplier_delivery_note_code に別途記録。
"""
import re

from django import forms
from django.core.validators import MaxValueValidator
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from masters.models import Sku, Supplier

from .models import InboundOrder, InboundOrderItem

TEXT = {'class': 'form-control'}
SELECT = {'class': 'form-select'}

# inbound_order_code の許可パターン: 「{prefix}-YYYYMMDD-NNN」、prefix は model の定義から動的に組み立て
_CODE_PREFIX_ALT = '|'.join(
    re.escape(p) for p in InboundOrder.CODE_PREFIX_BY_SOURCE_TYPE.values()
)
INBOUND_CODE_PATTERN = re.compile(rf'^({_CODE_PREFIX_ALT})-\d{{8}}-\d{{3}}$')


class InboundOrderForm(forms.ModelForm):
    class Meta:
        model = InboundOrder
        # status / arrived_at / received_at は業務アクション由来で変化するためフォーム外。
        # source_type は画面登録でありえる「手動登録 / 返品」の2択に絞り、ASN/発注アラートは画面に出さない。
        fields = [
            'inbound_order_code',
            'supplier',
            'expected_date',
            'source_type',
            'purchase_order_code',
            'supplier_delivery_note_code',
            'note',
        ]
        widgets = {
            'inbound_order_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: IO-20260514-001',
                       'autocomplete': 'off', 'maxlength': '15',
                       'pattern': rf'({_CODE_PREFIX_ALT})-\d{{8}}-\d{{3}}',
                       'title': 'IO-YYYYMMDD-NNN（通常）または RT-YYYYMMDD-NNN（返品）形式'}
            ),
            'supplier': forms.Select(attrs=SELECT),
            'expected_date': forms.DateInput(
                # Chrome の date input は min/max が無いと年が 6 桁まで入力できる挙動なので範囲制約で 4 桁に固定
                attrs={**TEXT, 'type': 'date', 'min': '2000-01-01', 'max': '2099-12-31'}
            ),
            'source_type': forms.Select(attrs=SELECT),
            'purchase_order_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: PO-202605-0001',
                       'autocomplete': 'off'}
            ),
            'supplier_delivery_note_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: DN-2026-0123',
                       'autocomplete': 'off'}
            ),
            'note': forms.Textarea(attrs={**TEXT, 'rows': '2'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 入荷指示番号は「{prefix}-YYYYMMDD-NNN」= 15 桁固定（model max_length=30 の
        # auto-maxlength を上書き）
        self.fields['inbound_order_code'].widget.attrs['maxlength'] = '15'
        # 仕入先: 有効なものだけ。編集時に現在の仕入先が無効化されていても残す
        sup_qs = Supplier.objects.filter(is_active=True).order_by('supplier_code')
        if self.instance.pk and self.instance.supplier_id:
            sup_qs = Supplier.objects.filter(
                pk__in=list(sup_qs.values_list('pk', flat=True)) + [self.instance.supplier_id]
            ).order_by('supplier_code')
        self.fields['supplier'].queryset = sup_qs
        self.fields['supplier'].empty_label = '— 仕入先を指定しない —'
        self.fields['supplier'].required = False

        # 入荷予定日のデフォルトは今日
        if not self.instance.pk and not self.data:
            self.fields['expected_date'].initial = timezone.localdate()

        # 入荷元種別: 画面登録は「手動登録」「返品」のみ。ASN/発注アラートは画面で選ばせない（誤入力防止）。
        # 編集時は変更不可（業務的に作成時に確定する項目）
        if self.instance.pk:
            self.fields['source_type'].disabled = True
        else:
            # 空デフォルトで「先に種別を選ばせる」フロー。選択時に JS が inbound_order_code を自動入力する
            self.fields['source_type'].choices = [
                ('', '— 入荷種別を選択 —'),
                (InboundOrder.SourceType.MANUAL.value, '手動登録（通常入荷・連携漏れの補完など）'),
                (InboundOrder.SourceType.RETURN.value, '返品入荷'),
            ]

        # 編集時は inbound_order_code を変更不可（伝票番号は文書の identity）
        if self.instance.pk:
            self.fields['inbound_order_code'].disabled = True

    def clean_inbound_order_code(self):
        """inbound_order_code を「{prefix}-YYYYMMDD-NNN」形式（prefix は IO/RT）に制限。"""
        code = (self.cleaned_data.get('inbound_order_code') or '').strip()
        if not code:
            raise forms.ValidationError('入力してください。')
        if not INBOUND_CODE_PATTERN.match(code):
            prefixes = ' / '.join(InboundOrder.CODE_PREFIX_BY_SOURCE_TYPE.values())
            raise forms.ValidationError(
                f'入荷指示番号は {prefixes} のいずれかから始まる '
                f'「{prefixes}-YYYYMMDD-NNN」形式で入力してください。'
            )
        return code

    def clean(self):
        """通常入荷（manual）では仕入先・入荷予定日を必須化。

        計画外入庫は別画面（StockMovement 直接発行）で扱うため、入荷指示画面の manual は
        「計画的な通常入荷 + ASN 連携漏れの補完」のみ。どちらも仕入先・予定日は本来分かっている。
        返品入荷（return）は顧客返品で仕入先 NULL・日付未確定がありえるので任意のまま。
        """
        cleaned = super().clean()
        source_type = cleaned.get('source_type')
        if source_type == InboundOrder.SourceType.MANUAL.value:
            if not cleaned.get('supplier'):
                self.add_error('supplier', '通常入荷では仕入先を指定してください。')
            if not cleaned.get('expected_date'):
                self.add_error(
                    'expected_date', '通常入荷では入荷予定日を指定してください。'
                )
        return cleaned


class InboundOrderItemForm(forms.ModelForm):
    """入荷指示明細フォーム。

    SKU は大量登録される想定なので select ではなくテキスト入力。
    バーコードスキャン or 手入力に対応し、🔍 ボタンの SKU マスタ検索モーダルから選択も可能。
    """

    sku_code = forms.CharField(
        label='SKU',
        max_length=13,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-sm font-monospace',
            'placeholder': 'SKU コード入力 or 🔍 で検索',
            'autocomplete': 'off',
        }),
    )

    class Meta:
        model = InboundOrderItem
        # sku は Meta.fields に含めない（sku_code テキスト経由で resolve するため）
        fields = ['quantity_expected', 'is_crossdock']
        widgets = {
            'quantity_expected': forms.NumberInput(
                attrs={**TEXT, 'min': '1', 'max': '99999', 'placeholder': '数量'}
            ),
            'is_crossdock': forms.CheckboxInput(
                attrs={'class': 'form-check-input'}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sku = None
        # 入荷予定数は 5 桁まで（〜99,999）
        self.fields['quantity_expected'].validators.append(MaxValueValidator(99999))
        # 編集時: 既存 SKU のコードを pre-fill（label_from_instance 的な動き）
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


class BaseInboundOrderItemFormSet(BaseInlineFormSet):
    """同一入荷指示内で同じ SKU が重複していないか検証する。

    モデルの UniqueConstraint(inbound_order, sku) は DB レベルで担保しているが、
    sku を Meta.fields から外したので formset 内の重複を画面で検出するためここで確認。
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
                    'sku_code', f'SKU「{code}」が同じ入荷指示内で重複しています。'
                )
            else:
                seen.add(code)


InboundOrderItemFormSet = inlineformset_factory(
    InboundOrder,
    InboundOrderItem,
    form=InboundOrderItemForm,
    formset=BaseInboundOrderItemFormSet,
    extra=3,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
