"""stock アプリのテンプレート用コンテキスト注入。

未対応の発注アラート件数をナビバー・サイドバーで表示するための
`open_reorder_alerts_count` を全テンプレで参照可能にする。

現在倉庫スコープの open アラート件数を COUNT 一発で取る。ログイン無し・
倉庫未選択時は 0。インデックス (idx_reorder_alerts_sku_wh_st) があるので
件数取得は O(1) に近い。
"""

from masters.utils import get_current_warehouse


def open_reorder_alerts(request):
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {'open_reorder_alerts_count': 0}
    warehouse = get_current_warehouse(request)
    if warehouse is None:
        return {'open_reorder_alerts_count': 0}
    # 循環 import 防止
    from .models import ReorderAlert

    count = ReorderAlert.objects.filter(
        warehouse=warehouse, status=ReorderAlert.Status.OPEN
    ).count()
    return {'open_reorder_alerts_count': count}
