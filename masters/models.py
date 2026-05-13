"""マスタテーブル群。

カラム順は設計（docs/Django在庫管理システム.sql）に合わせる:
  id → business → is_active → created_at → updated_at
TimestampMixin は使わず、各モデルで created_at/updated_at を末尾に明示。
"""
import re

from django.core.validators import MinValueValidator
from django.db import models


class Warehouse(models.Model):
    warehouse_code = models.CharField('倉庫コード', max_length=20, unique=True)
    warehouse_name = models.CharField('倉庫名', max_length=100)
    address = models.CharField('住所', max_length=255, blank=True)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'warehouses'
        verbose_name = '倉庫'
        verbose_name_plural = '倉庫'

    def __str__(self):
        return f'{self.warehouse_name} ({self.warehouse_code})'


class Area(models.Model):
    class LocationType(models.TextChoices):
        STORAGE = 'storage', '通常棚'
        LARGE_ITEM = 'large_item', '大型・長物'

    # 区分ごとの棚番セグメント定義: (key, label, digits)
    # 通常棚: area_code + 通路 + ラック + 段 → 'A-01-02-03'
    # 大型・長物: area_code + 連番 → 'L-001'
    LOCATION_CODE_SEGMENTS = {
        LocationType.STORAGE: [
            ('aisle', '通路', 2),
            ('rack', 'ラック', 2),
            ('level', '段', 2),
        ],
        LocationType.LARGE_ITEM: [
            ('seq', '連番', 3),
        ],
    }

    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    area_code = models.CharField('エリアコード', max_length=20)
    area_name = models.CharField('エリア名', max_length=100, blank=True)
    location_type = models.CharField(
        '区分',
        max_length=20,
        choices=LocationType.choices,
        default=LocationType.STORAGE,
    )
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'areas'
        verbose_name = 'エリア'
        verbose_name_plural = 'エリア'
        constraints = [
            models.UniqueConstraint(
                fields=['warehouse', 'area_code'], name='uk_areas_warehouse_code'
            ),
        ]

    def __str__(self):
        return f'{self.area_name or self.area_code} ({self.warehouse.warehouse_code})'

    def format_location_code(self, **segments):
        """セグメント値から完全な棚番を組み立てる。

        例: area_code='A', location_type=STORAGE
            format_location_code(aisle='01', rack='02', level='03')
            → 'A-01-02-03'
        """
        if self.location_type == self.LocationType.STORAGE:
            aisle = str(segments.get('aisle', '')).zfill(2)
            rack = str(segments.get('rack', '')).zfill(2)
            level = str(segments.get('level', '')).zfill(2)
            return f'{self.area_code}-{aisle}-{rack}-{level}'
        if self.location_type == self.LocationType.LARGE_ITEM:
            seq = str(segments.get('seq', '')).zfill(3)
            return f'{self.area_code}-{seq}'
        return ''

    def parse_location_code(self, code):
        """既存の棚番コードをセグメントに分解する（編集画面で初期値を埋めるのに使う）。"""
        prefix = f'{self.area_code}-'
        if not code.startswith(prefix):
            return {}
        suffix = code[len(prefix):]
        if self.location_type == self.LocationType.STORAGE:
            parts = suffix.split('-')
            if len(parts) == 3:
                return {'aisle': parts[0], 'rack': parts[1], 'level': parts[2]}
        elif self.location_type == self.LocationType.LARGE_ITEM:
            return {'seq': suffix}
        return {}


class Location(models.Model):
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    area = models.ForeignKey(Area, on_delete=models.PROTECT, verbose_name='エリア')
    location_code = models.CharField('棚番コード', max_length=30)
    location_name = models.CharField('表示名', max_length=100, blank=True)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'locations'
        verbose_name = 'ロケーション'
        verbose_name_plural = 'ロケーション'
        constraints = [
            models.UniqueConstraint(
                fields=['warehouse', 'location_code'],
                name='uk_locations_warehouse_code',
            ),
        ]

    def __str__(self):
        return self.location_code

    @property
    def position_summary(self):
        """一覧画面で表示する位置情報の要約。

        通常棚: '通路01 / ラック03 / 2段目'
        大型・長物: '連番管理'
        """
        parsed = self.area.parse_location_code(self.location_code)
        if not parsed:
            return ''
        if 'aisle' in parsed:
            return f'通路{parsed["aisle"]} / ラック{parsed["rack"]} / {int(parsed["level"])}段目'
        if 'seq' in parsed:
            return '連番管理'
        return ''


class Category(models.Model):
    """商品カテゴリ。最大 MAX_DEPTH 階層の木構造。

    例: 大カテゴリ(CAT-001) → 中カテゴリ(CAT-001-01) → 小カテゴリ(CAT-001-01-01)
    """

    MAX_DEPTH = 4  # 1=大, 2=中, 3=小, 4=詳細
    LEVEL_LABELS = ['大カテゴリ', '中カテゴリ', '小カテゴリ', '詳細カテゴリ']

    category_code = models.CharField('カテゴリコード', max_length=30, unique=True)
    category_name = models.CharField('カテゴリ名', max_length=100)
    description = models.TextField('説明', blank=True)
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='children',
        verbose_name='親カテゴリ',
    )
    sort_order = models.IntegerField('表示順', default=10)
    is_leaf = models.BooleanField('商品の登録先', default=False)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'categories'
        verbose_name = 'カテゴリ'
        verbose_name_plural = 'カテゴリ'
        ordering = ['sort_order', 'category_code']

    def __str__(self):
        return self.category_name

    @property
    def depth(self):
        """0=root, 1=child of root, ..."""
        d, p = 0, self.parent
        while p is not None:
            d += 1
            p = p.parent
        return d

    @property
    def level_label(self):
        """大/中/小/詳細カテゴリ"""
        d = self.depth
        if 0 <= d < len(self.LEVEL_LABELS):
            return self.LEVEL_LABELS[d]
        return f'第{d + 1}階層'

    @property
    def ancestors(self):
        """自分を除いたルートまでの祖先を上から順に返す。"""
        result = []
        cur = self.parent
        while cur is not None:
            result.insert(0, cur)
            cur = cur.parent
        return result

    @property
    def breadcrumb(self):
        """ルートから自分までのパスを ' › ' で連結（例: '工具 › 切削工具 › ドリル'）。"""
        names = []
        cur = self
        while cur is not None:
            names.insert(0, cur.category_name)
            cur = cur.parent
        return ' › '.join(names)

    def get_descendants(self):
        """全子孫を返す（深さ優先）。"""
        result = []
        stack = list(self.children.all())
        while stack:
            cat = stack.pop(0)
            result.append(cat)
            stack[:0] = list(cat.children.all())
        return result

    @classmethod
    def next_root_code(cls):
        """次のルートカテゴリコード（CAT-NNN）を採番する。"""
        existing = cls.objects.filter(parent__isnull=True).values_list('category_code', flat=True)
        max_n = 0
        for code in existing:
            m = re.match(r'^CAT-(\d{3})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'CAT-{max_n + 1:03d}'

    @classmethod
    def next_child_code(cls, parent):
        """親配下の次のコード（{親}-NN）を採番する。"""
        prefix = f'{parent.category_code}-'
        max_n = 0
        for code in cls.objects.filter(parent=parent).values_list('category_code', flat=True):
            if code.startswith(prefix):
                suffix = code[len(prefix):]
                m = re.match(r'^(\d{2})$', suffix)
                if m:
                    max_n = max(max_n, int(m.group(1)))
        return f'{prefix}{max_n + 1:02d}'


class Manufacturer(models.Model):
    manufacturer_code = models.CharField('メーカーコード', max_length=20, unique=True)
    manufacturer_name = models.CharField('メーカー名', max_length=100)
    url = models.URLField('URL', max_length=255, blank=True)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'manufacturers'
        verbose_name = 'メーカー'
        verbose_name_plural = 'メーカー'

    def __str__(self):
        return self.manufacturer_name


class Product(models.Model):
    product_code = models.CharField('商品コード', max_length=50, unique=True)
    product_name = models.CharField('商品名', max_length=200)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, verbose_name='カテゴリ'
    )
    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='メーカー',
    )
    description = models.TextField('説明', blank=True)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'products'
        verbose_name = '商品'
        verbose_name_plural = '商品'

    def __str__(self):
        return f'{self.product_name} ({self.product_code})'

    @classmethod
    def next_product_code(cls):
        """次の商品コード（PRD-NNNNN）を採番する。"""
        max_n = 0
        for code in cls.objects.values_list('product_code', flat=True):
            m = re.match(r'^PRD-(\d{5})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'PRD-{max_n + 1:05d}'


class Sku(models.Model):
    class PickingType(models.TextChoices):
        TOTAL = 'total', '種まき方式（AGV/GTP）'
        ORDER = 'order', 'オーダーピッキング（大型・長物）'

    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name='商品')
    sku_code = models.CharField('SKUコード', max_length=50, unique=True)
    jan_code = models.CharField('JANコード', max_length=20, blank=True, db_index=True)
    size_info = models.CharField('サイズ', max_length=50, blank=True)
    color_info = models.CharField('カラー', max_length=50, blank=True)
    quantity_per_unit = models.IntegerField(
        '入数（ケース）',
        default=1,
        validators=[MinValueValidator(1)],
    )
    picking_type = models.CharField(
        'ピッキング種別',
        max_length=20,
        choices=PickingType.choices,
        default=PickingType.TOTAL,
    )
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'skus'
        verbose_name = 'SKU'
        verbose_name_plural = 'SKU'

    def __str__(self):
        return f'{self.sku_code} ({self.product.product_name})'

    @classmethod
    def next_sku_code(cls):
        """次の SKU コード（SKU-NNNNNN）を採番する。"""
        max_n = 0
        for code in cls.objects.values_list('sku_code', flat=True):
            m = re.match(r'^SKU-(\d{6})$', code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f'SKU-{max_n + 1:06d}'


class Supplier(models.Model):
    supplier_code = models.CharField('仕入先コード', max_length=20, unique=True)
    supplier_name = models.CharField('仕入先名', max_length=100)
    contact_person = models.CharField('担当者', max_length=50, blank=True)
    phone_number = models.CharField('電話番号', max_length=20, blank=True)
    email = models.EmailField('メールアドレス', max_length=255, blank=True)
    postal_code = models.CharField('郵便番号', max_length=10, blank=True)
    address = models.CharField('住所', max_length=255, blank=True)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'suppliers'
        verbose_name = '仕入先'
        verbose_name_plural = '仕入先'

    def __str__(self):
        return self.supplier_name


class Customer(models.Model):
    class Type(models.IntegerChoices):
        CORPORATE = 1, '法人'
        SOLE_PROPRIETOR = 2, '個人事業主'
        INDIVIDUAL = 3, '一般個人'

    customer_code = models.CharField('顧客コード', max_length=20, unique=True)
    customer_name = models.CharField('顧客名', max_length=100)
    customer_type = models.IntegerField(
        '顧客種別',
        choices=Type.choices,
        default=Type.CORPORATE,
    )
    industry_type = models.CharField('業種', max_length=50, blank=True)
    postal_code = models.CharField('郵便番号', max_length=10, blank=True)
    address = models.CharField('住所', max_length=255, blank=True)
    is_active = models.BooleanField('有効', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'customers'
        verbose_name = '顧客'
        verbose_name_plural = '顧客'

    def __str__(self):
        return self.customer_name
