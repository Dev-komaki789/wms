"""EC サイト向け API のシリアライザ。

DRF の Serializer は Django モデルを JSON に変換するための仕組み。
EC backend がマスタ同期で使う想定のため、関連モデルの主要項目も flatten して 1 リクエストで取得できるようにする。
"""

from rest_framework import serializers

from masters.models import Sku


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
