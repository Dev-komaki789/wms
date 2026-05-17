"""出荷フロー検証用のテスト出荷指示を作成する開発用コマンド。

既存マスタ（倉庫・顧客・SKU・在庫）を前提に、`allocation_wait`（出荷起動待ち）の
出荷指示を作成する。作成後は「出荷起動 → ピッキング → 出荷検品」と画面操作で
出荷フローを一通り検証できる。

シナリオ（出荷起動での挙動を網羅）:
  1. 混在        — オーダーピッキング対象 + AGV対象。pending リストと完了済み
                    リストの両方が生成され、ピッキング → 出荷検品へ進む
  2. AGVのみ     — AGV対象のみ。出荷起動で inspection_wait へ直行する
  3. オーダーのみ — オーダーピッキング対象のみ。通常のピッキング作業を経る

使い方:
  python manage.py seed_outbound_orders            # 既定倉庫 WH01 に3件作成
  python manage.py seed_outbound_orders --warehouse WH02
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from masters.models import Customer, Sku, Warehouse
from outbound.models import OutboundOrder, OutboundOrderItem


class Command(BaseCommand):
    help = '出荷フロー検証用のテスト出荷指示（allocation_wait）を作成する'

    # (シナリオ名, [(SKUコード, 数量), ...])
    SCENARIOS = [
        ('混在（オーダー＋AGV）', [('SKU-000002', 5), ('SKU-000004', 4)]),
        ('AGVのみ', [('SKU-000004', 6)]),
        ('オーダーピッキングのみ', [('SKU-000002', 3), ('SKU-000003', 2)]),
    ]

    def add_arguments(self, parser):
        parser.add_argument(
            '--warehouse', default='WH01',
            help='対象倉庫コード（既定: WH01）',
        )

    def handle(self, *args, **options):
        wh_code = options['warehouse']
        warehouse = Warehouse.objects.filter(warehouse_code=wh_code).first()
        if warehouse is None:
            raise CommandError(f'倉庫「{wh_code}」が見つかりません。')

        customer = (
            Customer.objects.filter(is_active=True)
            .order_by('customer_code').first()
        )
        if customer is None:
            raise CommandError(
                '有効な顧客がありません。先に顧客マスタを登録してください。')

        # 出荷指示の登録者（created_by は必須・PROTECT）
        user = (
            get_user_model().objects
            .filter(is_superuser=True).order_by('pk').first()
        )
        if user is None:
            raise CommandError('スーパーユーザーが見つかりません。')

        # シナリオで使う SKU をまとめて引き、不足があれば中断する
        codes = {c for _, items in self.SCENARIOS for c, _ in items}
        skus = {
            s.sku_code: s
            for s in Sku.objects.filter(sku_code__in=codes, is_active=True)
        }
        missing = codes - set(skus)
        if missing:
            raise CommandError(
                f'SKU が見つかりません（無効化されている可能性）: '
                f'{", ".join(sorted(missing))}')

        today = timezone.localdate()
        created = []
        with transaction.atomic():
            for label, items in self.SCENARIOS:
                # next_code は同一トランザクション内の先行 INSERT も見て採番する
                code = OutboundOrder.next_code(
                    today, OutboundOrder.SourceType.MANUAL.value)
                order = OutboundOrder.objects.create(
                    outbound_order_code=code,
                    warehouse=warehouse,
                    customer=customer,
                    status=OutboundOrder.Status.ALLOCATION_WAIT,
                    source_type=OutboundOrder.SourceType.MANUAL,
                    delivery_name=f'{customer.customer_name} 御中',
                    note=f'[テストデータ] {label}',
                    created_by=user,
                )
                for sku_code, qty in items:
                    # ロケーション・在庫引き当ては出荷起動で確定するため未設定
                    OutboundOrderItem.objects.create(
                        outbound_order=order,
                        sku=skus[sku_code],
                        quantity_ordered=qty,
                    )
                created.append((order, label, items))

        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} 件の出荷指示を作成しました（倉庫 {wh_code}）:'))
        for order, label, items in created:
            detail = ', '.join(f'{c}×{q}' for c, q in items)
            self.stdout.write(
                f'  {order.outbound_order_code}  {label}  [{detail}]')
        self.stdout.write(
            '「出荷起動 → ピッキング → 出荷検品」と画面操作で検証できます。')
