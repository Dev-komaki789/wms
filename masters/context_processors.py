"""テンプレート全体に注入するコンテキスト。"""
from .utils import get_current_warehouse


def current_warehouse(request):
    """ヘッダー表示や権限制御で使う「現在の倉庫」を全テンプレートに注入する。"""
    return {'current_warehouse': get_current_warehouse(request)}
