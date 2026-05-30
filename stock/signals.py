"""stock アプリのシグナル群。

主目的: 在庫変動を契機に発注アラート (ReorderAlert) を自動生成 / 自動クローズ
する。

発火タイミング:
  - StockBalance.post_save:
      在庫数が更新されたら (sku × warehouse) の現在庫を再集計し、有効な
      SkuReorderSetting があれば reorder_point を下回っているかチェック。
      下回っていて、かつ同じ (sku × warehouse) の open なアラートが無ければ
      新しい ReorderAlert(status=open) を1件作る。
      既に open がある場合は重複作成しない（クローズ後に再度下回ったら新規）。

      ※ StockMovement.post_save にすると StockBalance.save() より先に走って
      しまい古い値で集計してしまうので、必ず StockBalance 側で受ける。

  - InboundOrder.post_save:
      この入荷指示に紐付いた reorder_alerts のうち、入荷指示が
      status='completed'（入荷完了）になったタイミングでアラート側を
      resolved に更新する。
"""

from django.db.models import Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


@receiver(post_save, sender='stock.StockBalance')
def maybe_create_reorder_alert(sender, instance, created, **kwargs):
    """StockBalance が更新されたら、対象 SKU × 倉庫の発注点を下回ったか確認。

    新規作成・更新どちらでも発火させる（quantity=0 で新規作成されることも
    あるため）。集計には instance を含む全ロケの最新値を使うので順序依存しない。
    """
    # 文字列参照（apps.get_model）を使うのは循環 import 対策。
    from django.apps import apps

    StockBalance = apps.get_model('stock', 'StockBalance')
    SkuReorderSetting = apps.get_model('stock', 'SkuReorderSetting')
    ReorderAlert = apps.get_model('stock', 'ReorderAlert')

    sku = instance.sku
    # location.warehouse がこの StockBalance がある倉庫。
    warehouse = instance.location.warehouse

    # 有効な発注設定が無ければ何もしない（多くの SKU はそもそも対象外）
    setting = SkuReorderSetting.objects.filter(sku=sku, warehouse=warehouse, is_active=True).first()
    if setting is None:
        return

    # 倉庫内の現在庫合計（複数ロケーション合算）。post_save 時点では instance も
    # 含めて DB 反映済みなので、集計でそのまま正しい値が出る。
    current_qty = (
        StockBalance.objects.filter(sku=sku, location__warehouse=warehouse).aggregate(
            total=Sum('quantity')
        )['total']
        or 0
    )
    if current_qty > setting.reorder_point:
        return  # まだ発注点以上

    # 同じ (sku × warehouse) で open のアラートが既にあれば重複させない
    has_open = ReorderAlert.objects.filter(
        sku=sku, warehouse=warehouse, status=ReorderAlert.Status.OPEN
    ).exists()
    if has_open:
        return

    ReorderAlert.objects.create(
        sku=sku,
        warehouse=warehouse,
        reorder_setting=setting,
        quantity_at_alert=current_qty,
        reorder_point_at_alert=setting.reorder_point,
        recommended_qty=setting.reorder_qty,
        status=ReorderAlert.Status.OPEN,
    )


@receiver(post_save, sender='inbound.InboundOrder')
def maybe_resolve_reorder_alerts(sender, instance, created, **kwargs):
    """入荷指示が 'completed'（入荷完了）になったとき、紐付いている
    open/ordered の ReorderAlert を resolved に切り替える。

    完了でないステータスの更新では何もしない。
    """
    if created:
        return  # 新規作成時には何も入庫していないので無関係

    # 入荷指示の Status はモデル側で定義済み。文字列で比較する。
    # InboundOrder.Status.COMPLETED = 'completed'
    if instance.status != 'completed':
        return

    from django.apps import apps

    ReorderAlert = apps.get_model('stock', 'ReorderAlert')

    # 変換時に紐付け済みの open/ordered を一括 resolved に
    ReorderAlert.objects.filter(
        inbound_order=instance,
        status__in=[ReorderAlert.Status.OPEN, ReorderAlert.Status.ORDERED],
    ).update(status=ReorderAlert.Status.RESOLVED, resolved_at=timezone.now())
