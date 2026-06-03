"""EC サイト連携用の API キー認証。

サービス間通信向けのシンプルな認証クラス。
`Authorization: Bearer <key>` ヘッダで送られたキーを環境変数 `WMS_API_KEY` と照合する。

設計判断:
- 認証は「許可されたサービスかどうか」のサービス間認証のみ判定する
  （ユーザー単位の認可は EC backend 側の JWT で行う、ここでは関与しない）
- 認証通過時の request.user は暫定で最初の superuser を紐づける
  TODO: API キー認証実装が完了したら、専用システムユーザー (ec_system 等) に置き換える
- /api/schema/* (OpenAPI スキーマ & Swagger UI) は認証不要にする
  （仕様書を見るのに API キーを要求すると採用面接デモができないため）

参考: https://www.django-rest-framework.org/api-guide/authentication/#custom-authentication
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class APIKeyAuthentication(BaseAuthentication):
    """Authorization: Bearer <key> ヘッダで認証する。

    settings.WMS_API_KEY と照合し、一致したら暫定で最初の superuser を request.user に紐づける。
    """

    keyword = 'Bearer'

    def authenticate(self, request):
        # OpenAPI スキーマと Swagger UI は認証不要（採用面接でデモ可能にするため）
        if request.path.startswith('/api/schema'):
            return None

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith(f'{self.keyword} '):
            # 認証ヘッダ自体がない → DRF の DEFAULT_PERMISSION_CLASSES で弾かせる
            return None

        token = auth_header[len(self.keyword) + 1:].strip()
        expected = getattr(settings, 'WMS_API_KEY', '') or ''
        if not expected:
            raise AuthenticationFailed(
                'サーバー側で WMS_API_KEY が設定されていません'
            )
        if token != expected:
            raise AuthenticationFailed('API キーが正しくありません')

        # 暫定: 最初の superuser を返す
        # TODO: 専用システムユーザー (ec_system 等) に置き換える
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).order_by('id').first()
        if user is None:
            raise AuthenticationFailed('API システムユーザーが見つかりません')
        return (user, token)

    def authenticate_header(self, request):
        """401 レスポンス時に返す WWW-Authenticate ヘッダの値。"""
        return self.keyword


class APIKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    """Swagger UI に「Authorize」ボタンと Bearer 認証フォームを表示するための拡張。

    これを定義しないと drf-spectacular はカスタム認証クラスを認識できず、
    Swagger UI に認証スキームが出ない。
    """

    target_class = 'api.authentication.APIKeyAuthentication'
    name = 'BearerAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'http',
            'scheme': 'bearer',
            'description': (
                'EC backend からの API 呼び出し認証用の共有秘密鍵。'
                'Authorization: Bearer <key> ヘッダで送信する。'
            ),
        }
