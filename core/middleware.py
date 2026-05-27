"""画面で発生した未処理例外を ErrorLog に記録するミドルウェア。"""

import traceback

from .models import ErrorLog


class ErrorLogMiddleware:
    """ビューで未処理の例外が発生したら ErrorLog に1件記録する。

    process_exception は記録のみ行い None を返すため、通常の例外処理
    （DEBUG 時のトレース画面 / 本番の 500 ページ）はそのまま継続する。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        try:
            user = getattr(request, 'user', None)
            ErrorLog.objects.create(
                error_type=ErrorLog.ErrorType.EXCEPTION,
                summary=f'{type(exception).__name__}: {exception}'[:255],
                detail=traceback.format_exc(),
                source=request.path[:255],
                request_method=request.method or '',
                user=user if (user is not None and user.is_authenticated) else None,
            )
        except Exception:
            # ログ記録自体の失敗で本来の例外処理を妨げない
            pass
        return None
