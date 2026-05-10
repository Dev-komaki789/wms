"""マスタテーブル群。

カラム順は設計（docs/Django在庫管理システム.sql）に合わせる:
  id → business → is_active → created_at → updated_at
TimestampMixin は使わず、各モデルで created_at/updated_at を末尾に明示。
"""
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
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    area_code = models.CharField('エリアコード', max_length=20)
    area_name = models.CharField('エリア名', max_length=100, blank=True)
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


class Location(models.Model):
    class Type(models.TextChoices):
        STORAGE = 'storage', '通常棚'
        LARGE_ITEM = 'large_item', '大型・長物'
        STAGING = 'staging', '出荷ステージング'
        CROSSDOCK = 'crossdock', 'クロスドック'
        RECEIVING = 'receiving', '入荷エリア'

    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, verbose_name='倉庫'
    )
    area = models.ForeignKey(Area, on_delete=models.PROTECT, verbose_name='エリア')
    location_code = models.CharField('棚番コード', max_length=30)
    location_name = models.CharField('表示名', max_length=100, blank=True)
    location_type = models.CharField(
        '種別',
        max_length=20,
        choices=Type.choices,
        default=Type.STORAGE,
    )
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
        indexes = [
            models.Index(fields=['location_type'], name='idx_locations_type'),
        ]

    def __str__(self):
        return self.location_code


class Category(models.Model):
    category_code = models.CharField('カテゴリコード', max_length=20, unique=True)
    category_name = models.CharField('カテゴリ名', max_length=100)
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='children',
        verbose_name='親カテゴリ',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'categories'
        verbose_name = 'カテゴリ'
        verbose_name_plural = 'カテゴリ'

    def __str__(self):
        return self.category_name


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
