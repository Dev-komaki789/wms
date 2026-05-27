"""マスタアプリ共通のユーティリティ。"""

from .models import Warehouse


def get_current_warehouse(request=None):
    """ログイン中の「現在の倉庫」を返す。

    ポートフォリオでは単一倉庫運用前提で WH01 を返す固定実装。
    実運用では `request.user` の所属倉庫から決定する想定。
    """
    return Warehouse.objects.filter(warehouse_code='WH01').first()
