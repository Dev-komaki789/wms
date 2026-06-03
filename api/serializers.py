"""EC サイト向け API のシリアライザ。

DRF の Serializer は Django モデルを JSON に変換するための仕組み。
EC backend がマスタ同期で使う想定のため、関連モデルの主要項目も flatten して 1 リクエストで取得できるようにする。
"""

from rest_framework import serializers

from masters.models import Category, Product, Sku


class SkuSerializer(serializers.ModelSerializer):
    """SKU を JSON で返すシリアライザ。

    Product との JOIN を select_related で取り、関連項目（product_code / product_name / category_id）も
    flatten して返す。EC 側は wms_id をキーに ec_skus テーブルへ UPSERT する想定。
    """

    product_code = serializers.CharField(source='product.product_code', read_only=True)
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    category_id = serializers.IntegerField(source='product.category_id', read_only=True)

    class Meta:
        model = Sku
        fields = [
            'id',
            'sku_code',
            'product_id',
            'product_code',
            'product_name',
            'category_id',
            'jan_code',
            'size_info',
            'color_info',
            'quantity_per_unit',
            'picking_type',
            'is_active',
            'created_at',
            'updated_at',
        ]


class ProductSerializer(serializers.ModelSerializer):
    """Product を JSON で返すシリアライザ。

    Category / Manufacturer を select_related で取り、コードと名前を flatten で同梱する。
    manufacturer は SET_NULL 可なので allow_null=True が必要。
    """

    category_code = serializers.CharField(source='category.category_code', read_only=True)
    category_name = serializers.CharField(source='category.category_name', read_only=True)
    manufacturer_code = serializers.CharField(
        source='manufacturer.manufacturer_code', read_only=True, allow_null=True
    )
    manufacturer_name = serializers.CharField(
        source='manufacturer.manufacturer_name', read_only=True, allow_null=True
    )

    class Meta:
        model = Product
        fields = [
            'id',
            'product_code',
            'product_name',
            'category_id',
            'category_code',
            'category_name',
            'manufacturer_id',
            'manufacturer_code',
            'manufacturer_name',
            'description',
            'is_active',
            'created_at',
            'updated_at',
        ]


class CategorySerializer(serializers.ModelSerializer):
    """Category を JSON で返すシリアライザ。

    自己参照木構造は parent_id で表現。EC 側は parent_id を辿ってツリー構築する。
    EC で商品登録先になる末端ノード判定に is_leaf を使う。
    """

    class Meta:
        model = Category
        fields = [
            'id',
            'category_code',
            'category_name',
            'parent_id',
            'description',
            'sort_order',
            'is_leaf',
            'is_active',
            'created_at',
            'updated_at',
        ]


# ===== 出荷指示作成 (POST /api/orders/) のリクエスト用 =====


class OutboundOrderItemCreateSerializer(serializers.Serializer):
    """注文確定時の明細リクエスト。SKU コードと数量だけ受ける。"""

    sku_code = serializers.CharField(max_length=50)
    quantity = serializers.IntegerField(min_value=1)


class OutboundOrderCreateSerializer(serializers.Serializer):
    """EC から WMS への注文確定リクエスト。

    詳細仕様は integration/api_spec.md を参照。

    - external_order_id: EC 側で採番した注文 ID（WMS は保存するだけ）
    - delivery_*: 配送先情報（個人情報は EC 側に留め、配送に必要な分のみ伝票として保持）
    - items: SKU コード × 数量のリスト
    """

    external_order_id = serializers.CharField(max_length=50)
    delivery_postal_code = serializers.CharField(max_length=10, allow_blank=True, default='')
    delivery_address = serializers.CharField(max_length=255, allow_blank=True, default='')
    delivery_name = serializers.CharField(max_length=100, allow_blank=True, default='')
    items = OutboundOrderItemCreateSerializer(many=True, allow_empty=False)
    note = serializers.CharField(allow_blank=True, default='', required=False)
