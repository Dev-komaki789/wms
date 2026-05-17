from django.conf import settings
from django.db import models
from django.utils import timezone


class ErrorLog(models.Model):
    """システムエラーログ。

    2種類のエラーを記録する:
    - exception: 画面操作中に発生した未処理例外（ErrorLogMiddleware が記録）
    - import: 上位システム（OMS 等）からの取り込みデータでエラーになった行

    運用者が原因調査・対応するための照会画面（PC）から参照する。
    """

    class ErrorType(models.TextChoices):
        EXCEPTION = 'exception', '画面例外エラー'
        IMPORT = 'import', '取り込みエラー'

    error_type = models.CharField(
        '種別', max_length=20, choices=ErrorType.choices
    )
    occurred_at = models.DateTimeField('発生日時', default=timezone.now)
    summary = models.CharField(
        '概要', max_length=255,
        help_text='例外クラス＋メッセージ、または取り込みエラーの要約',
    )
    detail = models.TextField(
        '詳細', blank=True,
        help_text='例外のトレースバック、または取り込みに失敗した行データ',
    )
    source = models.CharField(
        '発生元', max_length=255, blank=True,
        help_text='例外: リクエストパス / 取り込み: 取り込み元の名称',
    )
    request_method = models.CharField('HTTPメソッド', max_length=10, blank=True)
    reference = models.CharField(
        '対象', max_length=100, blank=True,
        help_text='取り込みエラーの対象（OMS注文番号など）',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='操作ユーザー',
    )
    is_resolved = models.BooleanField('対応済み', default=False)
    resolved_at = models.DateTimeField('対応日時', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'error_logs'
        verbose_name = 'エラーログ'
        verbose_name_plural = 'エラーログ'
        indexes = [
            models.Index(fields=['-occurred_at'], name='idx_error_logs_occurred'),
            models.Index(fields=['error_type'], name='idx_error_logs_type'),
            models.Index(fields=['is_resolved'], name='idx_error_logs_resolved'),
        ]

    def __str__(self):
        return f'[{self.get_error_type_display()}] {self.summary}'
