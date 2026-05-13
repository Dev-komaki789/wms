"""マスタ画面用 ModelForm 群。

ウィジェットに Bootstrap 5 のクラスを付与する。
これにより各フォームテンプレートは `{{ field }}` を直接書くだけでよい。
"""
import re

from django import forms

from .models import Area, Category, Location, Manufacturer, Product, Sku, Warehouse
from .utils import get_current_warehouse
from .widgets import StatusToggleWidget

AREA_CODE_PATTERN = re.compile(r'^[A-Z]$')

TEXT = {'class': 'form-control'}
SELECT = {'class': 'form-select'}
STATUS_LABEL = 'ステータス'

SEGMENT_INPUT_ATTRS = {
    'class': 'form-control text-center font-monospace',
    'inputmode': 'numeric',
    'pattern': r'\d*',
    'autocomplete': 'off',
}


class AreaForm(forms.ModelForm):
    class Meta:
        model = Area
        fields = ['warehouse', 'area_code', 'area_name', 'location_type', 'is_active']
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'warehouse': forms.Select(attrs=SELECT),
            'area_code': forms.TextInput(
                attrs={**TEXT, 'maxlength': '1', 'placeholder': '例: A',
                       'autocomplete': 'off',
                       'pattern': '[A-Z]',
                       'style': 'text-transform: uppercase',
                       'title': '英大文字 A〜Z を 1 文字',
                       'oninput': 'this.value = this.value.toUpperCase()'}
            ),
            'area_name': forms.TextInput(attrs=TEXT),
            'location_type': forms.RadioSelect,
            'is_active': StatusToggleWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 倉庫は disabled で固定。
        #   - 編集時: instance.warehouse をそのまま表示（他倉庫のエリアを編集しようとした際の誤表示を防ぐ）
        #   - 新規時: 現在ログイン中の倉庫を初期値に
        if self.instance.pk and self.instance.warehouse_id:
            wh = self.instance.warehouse
        else:
            wh = get_current_warehouse()
        if wh is not None:
            self.fields['warehouse'].queryset = Warehouse.objects.filter(pk=wh.pk)
            self.fields['warehouse'].initial = wh.pk
        self.fields['warehouse'].empty_label = None
        self.fields['warehouse'].disabled = True
        # area_code は 1 文字に強制（model は max_length=20 だが widget の auto-maxlength を上書き）
        self.fields['area_code'].widget.attrs['maxlength'] = '1'

    def clean_area_code(self):
        """エリアコードを大文字化 + A〜Z 1 文字のみに制限。"""
        code = (self.cleaned_data.get('area_code') or '').strip().upper()
        if not code:
            raise forms.ValidationError('入力してください。')
        if not AREA_CODE_PATTERN.match(code):
            raise forms.ValidationError('英大文字 A〜Z を 1 文字で入力してください。')
        return code

    def clean(self):
        """同一倉庫内のエリアコード重複チェック。"""
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


class LocationForm(forms.ModelForm):
    """ロケーション登録フォーム。

    UI フロー:
      1. 区分 (area_type) を選ぶ → エリア dropdown を区分で絞り込む（JS）
      2. エリアを選ぶ → エリアの区分に応じたセグメント入力欄を表示（JS）
      3. セグメント入力 → サーバー側で area.format_location_code() で組み立て
    """

    area_type = forms.ChoiceField(
        label='区分',
        choices=[('', '— 区分を選択 —')] + list(Area.LocationType.choices),
        widget=forms.Select(attrs=SELECT),
        required=True,
    )

    # セグメント入力欄。区分に応じて表示・必須が変わる（個別の required は clean() で判定）
    aisle = forms.CharField(
        label='通路', required=False, max_length=2,
        widget=forms.TextInput(attrs={**SEGMENT_INPUT_ATTRS, 'maxlength': '2',
                                       'placeholder': '00'}),
    )
    rack = forms.CharField(
        label='ラック', required=False, max_length=2,
        widget=forms.TextInput(attrs={**SEGMENT_INPUT_ATTRS, 'maxlength': '2',
                                       'placeholder': '00'}),
    )
    level = forms.CharField(
        label='段', required=False, max_length=2,
        widget=forms.TextInput(attrs={**SEGMENT_INPUT_ATTRS, 'maxlength': '2',
                                       'placeholder': '00'}),
    )
    seq = forms.CharField(
        label='連番', required=False, max_length=3,
        widget=forms.TextInput(attrs={**SEGMENT_INPUT_ATTRS, 'maxlength': '3',
                                       'placeholder': '000'}),
    )

    class Meta:
        model = Location
        fields = ['area', 'location_name', 'is_active']
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'area': forms.Select(attrs=SELECT),
            'location_name': forms.TextInput(attrs=TEXT),
            'is_active': StatusToggleWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # エリア dropdown は現在倉庫のものだけに限定（他倉庫のエリアにロケーションを作れないように）
        area_qs = (
            Area.objects.select_related('warehouse')
            .order_by('warehouse__warehouse_code', 'area_code')
        )
        current_wh = get_current_warehouse()
        if current_wh is not None:
            area_qs = area_qs.filter(warehouse=current_wh)
        self.fields['area'].queryset = area_qs
        self.fields['area'].empty_label = '— エリアを選択 —'
        # 編集時: 既存 location_code をセグメントに分解して初期値を埋める
        if self.instance.pk and self.instance.area_id:
            area = self.instance.area
            self.fields['area_type'].initial = area.location_type
            for key, val in area.parse_location_code(self.instance.location_code).items():
                if key in self.fields:
                    self.fields[key].initial = val

    def _validate_segment(self, key, max_digits):
        """セグメント値を取り出して検証し、ゼロパディングで返す。エラーは self.add_error。"""
        val = (self.cleaned_data.get(key) or '').strip()
        if not val:
            self.add_error(key, '入力してください。')
            return None
        if not val.isdigit():
            self.add_error(key, '数字のみで入力してください。')
            return None
        if len(val) > max_digits:
            self.add_error(key, f'{max_digits}桁以内で入力してください。')
            return None
        return val.zfill(max_digits)

    def clean(self):
        cleaned = super().clean()
        area = cleaned.get('area')
        if not area:
            return cleaned

        # 区分とエリアの整合性
        area_type = cleaned.get('area_type')
        if area_type and area_type != area.location_type:
            self.add_error('area_type', '選択した区分とエリアの区分が一致しません。')
            return cleaned

        # セグメントから location_code を組み立て
        segments = {}
        for key, _label, digits in Area.LOCATION_CODE_SEGMENTS.get(area.location_type, []):
            padded = self._validate_segment(key, digits)
            if padded is None:
                continue
            segments[key] = padded

        # セグメントエラーがあればここで終了
        if any(self.has_error(k) for k, _, _ in Area.LOCATION_CODE_SEGMENTS.get(area.location_type, [])):
            return cleaned

        location_code = area.format_location_code(**segments)
        cleaned['location_code'] = location_code

        # 倉庫内重複チェック
        qs = Location.objects.filter(warehouse=area.warehouse, location_code=location_code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                f'倉庫「{area.warehouse.warehouse_code}」内に同じ棚番「{location_code}」が既に存在します。'
            )

        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        area = self.cleaned_data['area']
        instance.warehouse = area.warehouse
        instance.location_code = self.cleaned_data['location_code']
        if commit:
            instance.save()
        return instance


class CategoryForm(forms.ModelForm):
    """カテゴリ登録・編集フォーム。

    UI:
      - 親カテゴリ select（自分自身と全子孫、末端カテゴリ、最大深度のものを除外）
      - カテゴリコードは保存時にサーバー側で自動採番（フォームには出さない）
      - is_leaf=True に切替は子カテゴリが無いときのみ可
    """

    class Meta:
        model = Category
        fields = [
            'parent', 'category_name', 'description',
            'sort_order', 'is_leaf', 'is_active',
        ]
        labels = {
            'is_active': STATUS_LABEL,
            'is_leaf': '商品の登録先にする',
        }
        widgets = {
            'parent': forms.Select(attrs=SELECT),
            'category_name': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: 研削工具'}
            ),
            'description': forms.Textarea(attrs={**TEXT, 'rows': '3'}),
            'sort_order': forms.NumberInput(attrs={**TEXT, 'min': '1'}),
            'is_leaf': forms.RadioSelect(
                choices=[
                    (False, 'いいえ — さらに子カテゴリで分類する'),
                    (True, 'はい — このカテゴリに商品を直接ひもづける'),
                ],
            ),
            'is_active': StatusToggleWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 親カテゴリ候補: 末端でなく、最大深度未満で、自分自身と子孫を除く
        qs = Category.objects.filter(is_leaf=False)
        if self.instance.pk:
            exclude_ids = {self.instance.pk}
            exclude_ids.update(c.pk for c in self.instance.get_descendants())
            qs = qs.exclude(pk__in=exclude_ids)
        # 親の depth が MAX_DEPTH - 2 以下 (=自分が MAX_DEPTH - 1 以下) のみ親候補
        eligible_ids = [c.pk for c in qs if c.depth < Category.MAX_DEPTH - 1]
        self.fields['parent'].queryset = (
            Category.objects.filter(pk__in=eligible_ids).order_by('sort_order', 'category_code')
        )
        self.fields['parent'].empty_label = '— ルート（大カテゴリとして登録）—'

    def clean(self):
        cleaned = super().clean()
        parent = cleaned.get('parent')
        is_leaf = cleaned.get('is_leaf')

        # MAX_DEPTH チェック
        if parent and parent.depth >= Category.MAX_DEPTH - 1:
            self.add_error(
                'parent',
                f'これ以上深い階層にはカテゴリを追加できません（最大 {Category.MAX_DEPTH} 階層）。',
            )

        # 親が末端カテゴリだった場合
        if parent and parent.is_leaf:
            self.add_error('parent', '末端カテゴリは子カテゴリを持てません。')

        # 子がいる状態で is_leaf=True への切替を防ぐ
        if is_leaf and self.instance.pk and self.instance.children.exists():
            self.add_error(
                'is_leaf',
                f'子カテゴリが {self.instance.children.count()} 件存在するため、末端カテゴリには切り替えられません。',
            )

        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        # 新規時のみコード自動採番
        if not instance.pk:
            if instance.parent:
                instance.category_code = Category.next_child_code(instance.parent)
            else:
                instance.category_code = Category.next_root_code()
        if commit:
            instance.save()
        return instance


class ManufacturerForm(forms.ModelForm):
    class Meta:
        model = Manufacturer
        fields = ['manufacturer_code', 'manufacturer_name', 'url', 'is_active']
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'manufacturer_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: MAKITA', 'autocomplete': 'off'}
            ),
            'manufacturer_name': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: 株式会社マキタ'}
            ),
            'url': forms.URLInput(attrs={**TEXT, 'placeholder': 'https://...'}),
            'is_active': StatusToggleWidget(),
        }


class ProductForm(forms.ModelForm):
    """商品登録・編集フォーム。

    UI:
      - 商品コードは保存時にサーバー側で自動採番（フォームには出さない）
      - カテゴリは末端カテゴリ（is_leaf=True かつ is_active=True）のみ選択可、breadcrumb 付き label
      - メーカーは任意（指定しない選択肢あり）
    """

    class Meta:
        model = Product
        fields = [
            'category', 'manufacturer', 'product_name', 'description', 'is_active',
        ]
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'category': forms.Select(attrs=SELECT),
            'manufacturer': forms.Select(attrs=SELECT),
            'product_name': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: インパクトドライバ TD173DRGX'}
            ),
            'description': forms.Textarea(attrs={**TEXT, 'rows': '3'}),
            'is_active': StatusToggleWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # カテゴリ: 末端のみ。breadcrumb 付きで表示
        cat_qs = (
            Category.objects.filter(is_leaf=True, is_active=True)
            .select_related('parent', 'parent__parent', 'parent__parent__parent')
            .order_by('category_code')
        )
        # 編集時、現在のカテゴリが is_leaf でない/無効になっていても選択肢に残す
        if self.instance.pk and self.instance.category_id:
            cat_qs = Category.objects.filter(
                pk__in=list(cat_qs.values_list('pk', flat=True)) + [self.instance.category_id]
            ).select_related('parent', 'parent__parent', 'parent__parent__parent')
        self.fields['category'].queryset = cat_qs
        self.fields['category'].label_from_instance = lambda c: c.breadcrumb
        self.fields['category'].empty_label = '— カテゴリを選択 —'

        # メーカー: 有効なものだけ、任意
        mfr_qs = Manufacturer.objects.filter(is_active=True).order_by('manufacturer_code')
        if self.instance.pk and self.instance.manufacturer_id:
            mfr_qs = Manufacturer.objects.filter(
                pk__in=list(mfr_qs.values_list('pk', flat=True)) + [self.instance.manufacturer_id]
            )
        self.fields['manufacturer'].queryset = mfr_qs
        self.fields['manufacturer'].empty_label = '— メーカーを指定しない —'
        self.fields['manufacturer'].required = False

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance.pk:
            instance.product_code = Product.next_product_code()
        if commit:
            instance.save()
        return instance


class SkuForm(forms.ModelForm):
    """SKU 登録・編集フォーム。

    UI:
      - SKU コードは保存時にサーバー側で自動採番（フォームには出さない）
      - 商品は有効なものだけ選択可（編集時は現在の商品も残す）
      - JAN / サイズ / カラーは任意
      - ピッキング種別は card 風 RadioSelect（種まき / オーダー）
    """

    class Meta:
        model = Sku
        fields = [
            'product', 'jan_code', 'size_info', 'color_info',
            'quantity_per_unit', 'picking_type', 'is_active',
        ]
        labels = {'is_active': STATUS_LABEL}
        widgets = {
            'product': forms.Select(attrs=SELECT),
            'jan_code': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: 4901234567890',
                       'maxlength': '20', 'autocomplete': 'off',
                       'inputmode': 'numeric'}
            ),
            'size_info': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: M / 100mm / 1.5L'}
            ),
            'color_info': forms.TextInput(
                attrs={**TEXT, 'placeholder': '例: ブラック / 赤'}
            ),
            'quantity_per_unit': forms.NumberInput(attrs={**TEXT, 'min': '1'}),
            'picking_type': forms.RadioSelect,
            'is_active': StatusToggleWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 商品: 有効なものだけ。編集時は現在の商品が無効化されていても残す
        product_qs = (
            Product.objects.filter(is_active=True)
            .select_related('category', 'manufacturer')
            .order_by('product_code')
        )
        if self.instance.pk and self.instance.product_id:
            product_qs = (
                Product.objects.filter(
                    pk__in=list(product_qs.values_list('pk', flat=True))
                    + [self.instance.product_id]
                )
                .select_related('category', 'manufacturer')
                .order_by('product_code')
            )
        self.fields['product'].queryset = product_qs
        self.fields['product'].empty_label = '— 商品を選択 —'

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance.pk:
            instance.sku_code = Sku.next_sku_code()
        if commit:
            instance.save()
        return instance
