"""エラーログ照会画面の動作確認用サンプルデータを作成する開発用コマンド。

画面例外エラー（exception）は ErrorLogMiddleware が実運用で自動記録するが、
取り込みエラー（import）は上位システム連携機能が未実装のため発生源が無い。
照会画面で両種別の表示を確認できるよう、サンプルを投入する。

使い方:
  python manage.py seed_error_logs           # 既存ログが無ければ投入
  python manage.py seed_error_logs --force   # 既存ログがあっても追加
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import ErrorLog


class Command(BaseCommand):
    help = 'エラーログ照会画面の動作確認用サンプルデータを作成する'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='既にエラーログがあっても追加する',
        )

    def handle(self, *args, **options):
        if ErrorLog.objects.exists() and not options['force']:
            self.stdout.write(self.style.WARNING(
                'エラーログが既に存在します。'
                '追加するには --force を付けてください。'))
            return

        user = (
            get_user_model().objects
            .filter(is_superuser=True).order_by('pk').first()
        )
        now = timezone.now()
        ET = ErrorLog.ErrorType

        # (error_type, 経過時間, summary, source, request_method, reference,
        #  user付与, is_resolved, detail)
        samples = [
            (ET.IMPORT, timedelta(hours=2),
             'OMS取込: 必須項目「配送先住所」が空のため取り込めません',
             'OMS連携バッチ', '', 'OMS-2026-000457', False, False,
             '取り込み行: {"external_order_id": "OMS-2026-000457", '
             '"customer_code": "CUST-0001", "delivery_name": "株式会社サンプル", '
             '"delivery_address": "", "items": [{"sku": "SKU-000002", "qty": 3}]}'),
            (ET.IMPORT, timedelta(hours=2, minutes=1),
             'OMS取込: SKU「SKU-999999」がマスタに存在しません',
             'OMS連携バッチ', '', 'OMS-2026-000458', False, False,
             '取り込み行: {"external_order_id": "OMS-2026-000458", '
             '"items": [{"sku": "SKU-999999", "qty": 1}]}\n'
             '原因: skus テーブルに sku_code=SKU-999999 が見つからない'),
            (ET.IMPORT, timedelta(days=1, hours=3),
             'OMS取込: 数量が不正です（quantity=-2）',
             'OMS連携バッチ', '', 'OMS-2026-000461', False, True,
             '取り込み行: {"external_order_id": "OMS-2026-000461", '
             '"items": [{"sku": "SKU-000004", "qty": -2}]}\n'
             '原因: quantity は 1 以上である必要がある'),
            (ET.EXCEPTION, timedelta(hours=5),
             "ValueError: invalid literal for int() with base 10: 'たくさん'",
             '/outbound/orders/new/', 'POST', '', True, False,
             'Traceback (most recent call last):\n'
             '  File ".../django/core/handlers/exception.py", line 55, in inner\n'
             '    response = get_response(request)\n'
             '  File ".../outbound/views.py", line 210, in form_valid\n'
             '    quantity = int(request.POST.get("quantity"))\n'
             "ValueError: invalid literal for int() with base 10: 'たくさん'"),
            (ET.EXCEPTION, timedelta(days=2, hours=1),
             'Sku.DoesNotExist: Sku matching query does not exist.',
             '/inbound/orders/12/edit/', 'POST', '', True, True,
             'Traceback (most recent call last):\n'
             '  File ".../django/core/handlers/exception.py", line 55, in inner\n'
             '    response = get_response(request)\n'
             '  File ".../inbound/forms.py", line 152, in clean_sku_code\n'
             '    self._sku = Sku.objects.get(sku_code=code)\n'
             'Sku.DoesNotExist: Sku matching query does not exist.'),
        ]

        created = 0
        for (etype, ago, summary, source, method, ref,
             with_user, resolved, detail) in samples:
            ErrorLog.objects.create(
                error_type=etype,
                occurred_at=now - ago,
                summary=summary,
                detail=detail,
                source=source,
                request_method=method,
                reference=ref,
                user=user if with_user else None,
                is_resolved=resolved,
                resolved_at=(now - ago + timedelta(hours=1)) if resolved else None,
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f'{created} 件のサンプルエラーログを作成しました'
            f'（取り込みエラー / 画面例外エラー）。'))
