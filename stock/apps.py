from django.apps import AppConfig


class StockConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stock'

    def ready(self):
        # 在庫変動→発注アラート / 入荷完了→アラート解消 のシグナルを登録。
        # 副作用目的の import なので未使用警告を抑制する。
        from . import signals  # noqa: F401
