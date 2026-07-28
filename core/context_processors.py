"""core アプリのテンプレート用コンテキスト注入。"""

from django.conf import settings


def handheld_demo_codes(request):
    """ハンディ入口の「番号タップ入力」を表示するかのフラグを全テンプレに渡す。

    候補コードそのものは各 View が context に載せる（画面ごとに対象モデルが違うため）。
    ここでは表示可否のフラグだけを全テンプレで参照できるようにする。
    """
    return {'handheld_demo_codes': settings.HANDHELD_DEMO_CODES}
