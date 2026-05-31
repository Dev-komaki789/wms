"""アクセス制限ミドルウェア（ハンディ作業者の制限 ＋ 非superuserの管理サイト遮断）。

1) 非 superuser が Django 管理サイト(/admin/。login/logout 除く)へ来たら
   メニュー(home)へ戻す。業務ユーザー（demo 等）がログイン後に管理サイトの
   トップへ着地するのを防ぎ、メニュー画面へ誘導する。
2) ハンディ作業者（Group: handheld_workers）が PC 業務 URL（マスタ管理・指示
   照会・在庫照会など）へ直接アクセスしようとしたら、メニュー（home）へ
   リダイレクトする。

許可される URL:
  - `/`                         メニュー画面（ハブ）
  - `/<app>/handheld/*`         各業務ハンディ画面
  - `/masters/api/*`            ハンディ画面が AJAX で叩くマスタ参照 API
                                （SKU 実在チェック・棚番実在チェック・SKU 検索等）
  - `/stock/api/*`              ハンディ画面が AJAX で叩く在庫参照 API
  - `/admin/login/`             ログイン
  - `/admin/logout/`            ログアウト
  - `/static/*`                 静的ファイル
"""

from django.shortcuts import redirect

from .permissions import is_handheld_worker


class HandheldOnlyMiddleware:
    # ハンディ画面が裏で呼ぶ AJAX エンドポイント（/masters/api/、/stock/api/）も
    # 許可しないと、画面は開けるのに棚番/SKU の実在チェックだけ通らない、という
    # 症状になる（リダイレクトされて HTML が返り、フロントは「セッション切れ」
    # として扱ってしまう）。
    ALLOWED_PREFIXES = (
        '/inbound/handheld/',
        '/outbound/handheld/',
        '/stock/handheld/',
        '/masters/api/',
        '/stock/api/',
        '/admin/login/',
        '/admin/logout/',
        '/static/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        # 非 superuser が Django 管理サイト(/admin/)に来たらメニュー(home)へ戻す。
        # 業務ユーザー（demo・ハンディ作業者など）はログイン(/admin/login/)後に
        # 管理サイトのトップへ着地してしまうため、メニュー画面へ誘導する。
        # superuser のみ管理サイトを使える。ログイン/ログアウトは対象外。
        if (
            user is not None
            and getattr(user, 'is_authenticated', False)
            and not user.is_superuser
            and request.path.startswith('/admin/')
            and not request.path.startswith(('/admin/login/', '/admin/logout/'))
        ):
            return redirect('home')
        # ハンディ作業者は PC 業務 URL に来たらメニュー(home)へ戻す
        if (
            is_handheld_worker(user)
            and request.path != '/'
            and not request.path.startswith(self.ALLOWED_PREFIXES)
        ):
            return redirect('home')
        return self.get_response(request)
