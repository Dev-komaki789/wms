"""ハンディ作業者の URL アクセスを制限するミドルウェア。

ハンディ作業者（Group: handheld_workers）が PC 業務 URL（マスタ管理・指示
照会・在庫照会など）へ直接アクセスしようとしたら、メニュー（home）へ
リダイレクトする。

許可される URL:
  - `/`                         メニュー画面（ハブ）
  - `/<app>/handheld/*`         各業務ハンディ画面
  - `/admin/login/`             ログイン
  - `/admin/logout/`            ログアウト
  - `/static/*`                 静的ファイル
"""
from django.shortcuts import redirect

from .permissions import is_handheld_worker


class HandheldOnlyMiddleware:
    ALLOWED_PREFIXES = (
        '/inbound/handheld/',
        '/outbound/handheld/',
        '/stock/handheld/',
        '/admin/login/',
        '/admin/logout/',
        '/static/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            is_handheld_worker(getattr(request, 'user', None))
            and request.path != '/'
            and not request.path.startswith(self.ALLOWED_PREFIXES)
        ):
            return redirect('home')
        return self.get_response(request)
