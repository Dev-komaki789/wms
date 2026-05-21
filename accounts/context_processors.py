"""テンプレートに「ロール」情報を流すコンテキストプロセッサ。"""
from .permissions import is_handheld_worker


def user_role(request):
    """`is_handheld_worker` を全テンプレートで使えるようにする。

    base.html / home.html などで `{% if is_handheld_worker %}` で
    PC 業務メニューを非表示にする。
    """
    user = getattr(request, 'user', None)
    return {'is_handheld_worker': is_handheld_worker(user)}
