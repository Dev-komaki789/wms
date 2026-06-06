"""DB をクリーンアップして中規模テストデータを投入する開発用コマンド。

User 以外の全データを削除し、マスタ・在庫・指示・履歴を再生成する。
画面操作の練習用に **未着手の指示を多めに**、過去履歴をそこそこ揃える。

規模感:
  - 倉庫 2 / エリア 6 / 棚 192 (WH01:128 + WH02:64)
  - SKU 100 / 商品 100 / カテゴリ ~19 / メーカー 10
  - 顧客 15 / 仕入先 8
  - 在庫 ~500 / 入出庫履歴 ~600 (manual_in)
  - 未着手 入荷指示 38 / 完了済 入荷指示 2 (= 40)
  - 未着手 出荷指示 68 / 完了済 出荷指示 2 (= 70)
  - エラーログ 10 / 棚卸セッション 2

使い方:
  python manage.py reset_and_seed           # 確認プロンプトあり
  python manage.py reset_and_seed --yes     # 確認プロンプトをスキップ
  python manage.py reset_and_seed --seed 7  # 乱数シード変更
"""

from __future__ import annotations

import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import ErrorLog
from inbound.models import InboundOrder, InboundOrderItem, InboundReceipt
from masters.models import (
    Area,
    Category,
    Customer,
    Location,
    Manufacturer,
    Product,
    Sku,
    Supplier,
    Warehouse,
)
from outbound.models import (
    DeliveryNote,
    DeliveryNoteItem,
    OutboundOrder,
    OutboundOrderItem,
    PickingList,
    PickingListItem,
    Shipment,
    ShipmentItem,
    StockReservation,
)
from stock.models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StocktakeAdjustment,
    StocktakeItem,
    StocktakeSession,
)


class Command(BaseCommand):
    help = 'User 以外の全データを削除して中規模テストデータを再生成する'

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes',
            action='store_true',
            help='確認プロンプトをスキップする',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=42,
            help='乱数シード（既定: 42）',
        )

    # ===== entry point =====================================================

    def handle(self, *args, **options):
        if not options['yes']:
            self.stdout.write(
                self.style.WARNING(
                    'User 以外の全データを削除して中規模テストデータを再生成します。'
                )
            )
            confirm = input('続行しますか？ [y/N]: ').strip().lower()
            if confirm != 'y':
                self.stdout.write('中断しました。')
                return

        random.seed(options['seed'])
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.user = self._get_seed_user()

        with transaction.atomic():
            self._delete_all()
            self._ensure_handheld_worker()
            self._ensure_demo_user()
            self._create_masters()
            self._create_initial_stock()
            self._create_completed_inbound()
            self._create_completed_outbound()
            self._create_pending_inbound()
            self._create_pending_outbound()
            self._create_error_logs()
            self._create_stocktakes()

        self._print_summary()

    def _ensure_handheld_worker(self):
        """ハンディ作業者グループと worker1 ユーザーを用意する（冪等）。

        worker1 はハンディ画面しか開けないテスト用アカウント。
        Group が無ければ作る・ユーザーが無ければ作る・既存ユーザーの場合は
        グループ所属だけを保証する。
        """
        from accounts.permissions import ensure_handheld_group

        self.stdout.write('  ハンディ作業者グループ・worker1 を用意...')
        group = ensure_handheld_group()
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username='worker1',
            defaults={
                'display_name': '作業者1（ハンディ専用）',
                # is_staff=True は /admin/login/ を通すためだけに必要。
                # 実際の /admin/ はミドルウェアがブロックして home へ戻す。
                'is_staff': True,
                'is_superuser': False,
            },
        )
        if created:
            user.set_password('***REDACTED***')
            user.save()
        else:
            # 既存ユーザーでも is_staff だけは保証する
            if not user.is_staff:
                user.is_staff = True
                user.save(update_fields=['is_staff'])
        user.groups.add(group)

    def _ensure_demo_user(self):
        """demo ユーザーを用意する（冪等）。

        demo は PC 業務画面を一通り見られるが /admin/ には入れないテスト用アカウント。
        is_staff=True は /admin/login/ を通すため、is_superuser=False と
        handheld_workers Group 未所属の組み合わせで、ミドルウェアが /admin/ を
        遮断しつつ業務画面は全て開ける状態になる。
        """
        self.stdout.write('  demo ユーザーを用意...')
        from accounts.permissions import HANDHELD_GROUP_NAME

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username='demo',
            defaults={
                'display_name': 'デモユーザー（PC全画面・admin除く）',
                'is_staff': True,
                'is_superuser': False,
            },
        )
        if created:
            user.set_password('***REDACTED***')
            user.save()
        else:
            if not user.is_staff:
                user.is_staff = True
                user.save(update_fields=['is_staff'])
        # 既存ユーザーがハンディグループに入っていたら外す（demo は PC 画面想定）
        from django.contrib.auth.models import Group

        handheld = Group.objects.filter(name=HANDHELD_GROUP_NAME).first()
        if handheld is not None:
            user.groups.remove(handheld)

    def _get_seed_user(self):
        user = get_user_model().objects.filter(is_superuser=True).order_by('pk').first()
        if user is None:
            raise CommandError('superuser が見つかりません。先に createsuperuser してください。')
        return user

    # ===== delete ==========================================================

    def _delete_all(self):
        self.stdout.write('  削除フェーズ...')
        # PROTECT 関係を逆順に解いていく
        StocktakeAdjustment.objects.all().delete()
        StocktakeItem.objects.all().delete()
        StocktakeSession.objects.all().delete()
        PickingListItem.objects.all().delete()
        PickingList.objects.all().delete()
        ShipmentItem.objects.all().delete()
        Shipment.objects.all().delete()
        DeliveryNoteItem.objects.all().delete()
        DeliveryNote.objects.all().delete()
        StockReservation.objects.all().delete()
        OutboundOrderItem.objects.all().delete()
        OutboundOrder.objects.all().delete()
        InboundReceipt.objects.all().delete()
        InboundOrderItem.objects.all().delete()
        InboundOrder.objects.all().delete()
        StockTransfer.objects.all().delete()
        StockMovement.objects.all().delete()
        StockBalance.objects.all().delete()
        ErrorLog.objects.all().delete()
        Sku.objects.all().delete()
        Product.objects.all().delete()
        Manufacturer.objects.all().delete()
        # Category は self-FK (PROTECT)。葉から順に消す
        while Category.objects.exists():
            leaves_qs = Category.objects.filter(children__isnull=True)
            if not leaves_qs.exists():
                # ありえないが念のためループ防止
                Category.objects.all().delete()
                break
            leaves_qs.delete()
        Customer.objects.all().delete()
        Supplier.objects.all().delete()
        Location.objects.all().delete()
        Area.objects.all().delete()
        Warehouse.objects.all().delete()

    # ===== master ==========================================================

    def _create_masters(self):
        self.stdout.write('  マスタ生成...')
        self._create_warehouses_and_locations()
        self._create_categories()
        self._create_manufacturers()
        self._create_products_and_skus()
        self._create_suppliers()
        self._create_customers()

    def _create_warehouses_and_locations(self):
        self.wh1 = Warehouse.objects.create(
            warehouse_code='WH01',
            warehouse_name='メイン倉庫',
            address='東京都江東区辰巳3-1-1',
        )
        self.wh2 = Warehouse.objects.create(
            warehouse_code='WH02',
            warehouse_name='サブ倉庫',
            address='千葉県市川市原木中山5-12-3',
        )

        # WH01: A,B = AGV, L = 大型・長物
        # WH02: D,E = AGV, M = 大型・長物
        # area_code は warehouse 内ユニークだが、別倉庫でも分けておくと location_code が
        # 衝突せず見やすい
        self.area_a = Area.objects.create(
            warehouse=self.wh1,
            area_code='A',
            area_name='Aエリア（AGV）',
            location_type=Area.LocationType.STORAGE,
        )
        self.area_b = Area.objects.create(
            warehouse=self.wh1,
            area_code='B',
            area_name='Bエリア（AGV）',
            location_type=Area.LocationType.STORAGE,
        )
        self.area_l = Area.objects.create(
            warehouse=self.wh1,
            area_code='L',
            area_name='Lエリア（大型・長物）',
            location_type=Area.LocationType.LARGE_ITEM,
        )
        self.area_d = Area.objects.create(
            warehouse=self.wh2,
            area_code='D',
            area_name='Dエリア（AGV）',
            location_type=Area.LocationType.STORAGE,
        )
        self.area_e = Area.objects.create(
            warehouse=self.wh2,
            area_code='E',
            area_name='Eエリア（AGV）',
            location_type=Area.LocationType.STORAGE,
        )
        self.area_m = Area.objects.create(
            warehouse=self.wh2,
            area_code='M',
            area_name='Mエリア（大型・長物）',
            location_type=Area.LocationType.LARGE_ITEM,
        )

        # ロケーション 192件
        self._create_storage_locations(self.area_a, aisles=3, racks=6, levels=3)  # 54
        self._create_storage_locations(self.area_b, aisles=3, racks=6, levels=3)  # 54
        self._create_large_item_locations(self.area_l, count=20)  # 20
        self._create_storage_locations(self.area_d, aisles=2, racks=6, levels=3)  # 36
        self._create_storage_locations(self.area_e, aisles=2, racks=4, levels=3)  # 24
        self._create_large_item_locations(self.area_m, count=4)  # 4

    def _create_storage_locations(self, area, *, aisles, racks, levels):
        for a in range(1, aisles + 1):
            for r in range(1, racks + 1):
                for lv in range(1, levels + 1):
                    code = area.format_location_code(
                        aisle=a,
                        rack=r,
                        level=lv,
                    )
                    Location.objects.create(
                        warehouse=area.warehouse,
                        area=area,
                        location_code=code,
                    )

    def _create_large_item_locations(self, area, *, count):
        for s in range(1, count + 1):
            code = area.format_location_code(seq=s)
            Location.objects.create(
                warehouse=area.warehouse,
                area=area,
                location_code=code,
            )

    def _create_categories(self):
        """4階層のうち 2階層（大→中=leaf）構成で十分。商品は leaf に紐付ける。"""
        tree = [
            (
                'CAT-001',
                '工具',
                [
                    ('切削工具', True),
                    ('手工具', True),
                    ('測定工具', True),
                ],
            ),
            (
                'CAT-002',
                '電子部品',
                [
                    ('抵抗器', True),
                    ('コンデンサ', True),
                    ('半導体', True),
                ],
            ),
            (
                'CAT-003',
                '機械部品',
                [
                    ('ベアリング', True),
                    ('配管継手', True),
                    ('ボルト・ナット', True),
                ],
            ),
            (
                'CAT-004',
                '消耗品',
                [
                    ('文具', True),
                    ('梱包資材', True),
                    ('清掃用品', True),
                ],
            ),
            (
                'CAT-005',
                '大型機器',
                [
                    ('エンジン部品', True),
                    ('シャフト・配管', True),
                ],
            ),
        ]
        self.leaf_categories = []  # 通常 SKU の格納先（TOTAL）
        self.large_leaf_categories = []  # ORDER SKU の格納先
        for root_code, root_name, children in tree:
            root = Category.objects.create(
                category_code=root_code,
                category_name=root_name,
                sort_order=int(root_code.split('-')[1]) * 10,
                is_leaf=False,
            )
            for i, (child_name, is_leaf) in enumerate(children, start=1):
                child_code = f'{root_code}-{i:02d}'
                child = Category.objects.create(
                    category_code=child_code,
                    category_name=child_name,
                    parent=root,
                    sort_order=i * 10,
                    is_leaf=is_leaf,
                )
                if root_code == 'CAT-005':
                    self.large_leaf_categories.append(child)
                else:
                    self.leaf_categories.append(child)

    def _create_manufacturers(self):
        # すべて架空のメーカー名（実在企業・ブランドは使用しない）
        names = [
            ('MFR-001', 'ハルナ精工'),
            ('MFR-002', 'トキワ工機'),
            ('MFR-003', 'ミハタ精密'),
            ('MFR-004', 'アラタ製作所'),
            ('MFR-005', 'ヌマタ工業'),
            ('MFR-006', 'セオト機器'),
            ('MFR-007', 'コトネ部品'),
            ('MFR-008', 'ヤヅル精工'),
            ('MFR-009', 'タチバナ機工'),
            ('MFR-010', 'シノギ製作所'),
        ]
        self.manufacturers = [
            Manufacturer.objects.create(
                manufacturer_code=code,
                manufacturer_name=name,
                url=f'https://example.com/{code.lower()}/',
            )
            for code, name in names
        ]

    def _create_products_and_skus(self):
        # 通常 SKU 80件 (TOTAL = 種まき方式 = AGV)
        # 大型 SKU 20件 (ORDER = オーダーピッキング = 大型・長物)
        # 商品名生成用の語彙
        normal_words = [
            ('ドリルビット', ['3mm', '5mm', '8mm', '10mm', '12mm']),
            ('六角レンチ', ['M3', 'M4', 'M5', 'M6', 'M8']),
            ('プラスドライバー', ['#1', '#2', '#3']),
            ('スパナ', ['10mm', '13mm', '17mm', '19mm', '22mm']),
            ('ノギス', ['100mm', '150mm', '200mm']),
            ('抵抗器', ['100Ω', '1kΩ', '10kΩ', '100kΩ']),
            ('セラミックコンデンサ', ['10pF', '100pF', '1nF', '10nF', '100nF']),
            ('電解コンデンサ', ['10μF', '100μF', '470μF', '1000μF']),
            ('LED', ['赤', '青', '緑', '白', '黄']),
            ('ベアリング608', ['ZZ', '2RS', 'オープン']),
            ('ベアリング6200', ['ZZ', '2RS', 'オープン']),
            ('配管継手', ['1/4インチ', '3/8インチ', '1/2インチ']),
            ('六角ボルト', ['M6×20', 'M6×30', 'M8×30', 'M10×40']),
            ('ナイロンナット', ['M4', 'M5', 'M6', 'M8', 'M10']),
            ('ワッシャー', ['M4', 'M5', 'M6', 'M8']),
            ('ボールペン', ['黒', '赤', '青']),
            ('ノート', ['A4', 'B5']),
            ('クラフトテープ', ['50mm', '75mm']),
            ('ダンボール', ['80サイズ', '100サイズ', '120サイズ']),
            ('クロス', ['ブルー', 'ホワイト', 'グレー']),
        ]
        large_words = [
            ('シリンダーヘッド', ['1500cc', '2000cc']),
            ('クランクシャフト', ['1500cc', '2000cc']),
            ('カムシャフト', ['1500cc', '2000cc']),
            ('ステンレス配管', ['1m', '2m', '3m', '5m']),
            ('プロペラシャフト', ['1.5m', '2m']),
            ('エキゾーストマニホールド', ['4気筒', '6気筒']),
        ]

        self.skus = []
        sku_no = 1
        prod_no = 1

        # 通常 SKU 80件
        for category in self._cycle(self.leaf_categories, count=80):
            base, variants = random.choice(normal_words)
            variant = random.choice(variants)
            product = Product.objects.create(
                product_code=f'PRD-{prod_no:05d}',
                product_name=f'{base} {variant}',
                category=category,
                manufacturer=random.choice(self.manufacturers),
                description='',
            )
            sku = Sku.objects.create(
                product=product,
                sku_code=f'SKU-{sku_no:06d}',
                jan_code=f'4901234{sku_no:06d}',
                size_info=variant,
                quantity_per_unit=random.choice([1, 10, 20, 50, 100]),
                picking_type=Sku.PickingType.TOTAL,
            )
            self.skus.append(sku)
            sku_no += 1
            prod_no += 1

        # 大型 SKU 20件
        for category in self._cycle(self.large_leaf_categories, count=20):
            base, variants = random.choice(large_words)
            variant = random.choice(variants)
            product = Product.objects.create(
                product_code=f'PRD-{prod_no:05d}',
                product_name=f'{base} {variant}',
                category=category,
                manufacturer=random.choice(self.manufacturers),
                description='',
            )
            sku = Sku.objects.create(
                product=product,
                sku_code=f'SKU-{sku_no:06d}',
                jan_code=f'4901234{sku_no:06d}',
                size_info=variant,
                quantity_per_unit=1,
                picking_type=Sku.PickingType.ORDER,
            )
            self.skus.append(sku)
            sku_no += 1
            prod_no += 1

    @staticmethod
    def _cycle(items, *, count):
        """items をぐるぐる回しながら count 個 yield する。順序は items 通り。"""
        for i in range(count):
            yield items[i % len(items)]

    def _create_suppliers(self):
        # すべて架空の仕入先名（実在企業は使用しない）。担当者名は一般的な仮名
        rows = [
            ('SUP-001', 'アオゾラ資材', '担当 山田', '03-1111-0001'),
            ('SUP-002', 'ミドリ商事', '担当 鈴木', '06-2222-0002'),
            ('SUP-003', 'ホクト工機商会', '担当 佐藤', '06-3333-0003'),
            ('SUP-004', 'サカエ部品センター', '担当 高橋', '03-4444-0004'),
            ('SUP-005', 'ニシキ産業', '担当 中村', '052-5555-0005'),
            ('SUP-006', 'トウメイ部品商会', '担当 小林', '048-6666-0006'),
            ('SUP-007', 'イズミ工具センター', '担当 加藤', '03-7777-0007'),
            ('SUP-008', 'カンナミ資材ロジ', '担当 吉田', '06-8888-0008'),
        ]
        self.suppliers = [
            Supplier.objects.create(
                supplier_code=code,
                supplier_name=name,
                contact_person=person,
                phone_number=phone,
            )
            for code, name, person, phone in rows
        ]

    def _create_customers(self):
        # 法人 13, 個人事業主 1, 一般個人 1。すべて架空の顧客名（実在企業・個人は使用しない）
        rows = [
            ('CUST-0001', 'アルファ商事', Customer.Type.CORPORATE, '製造業'),
            ('CUST-0002', 'ベルダ工業', Customer.Type.CORPORATE, '製造業'),
            ('CUST-0003', 'カイト機械', Customer.Type.CORPORATE, '機械'),
            ('CUST-0004', 'ソレイユ精機', Customer.Type.CORPORATE, '精密機械'),
            ('CUST-0005', 'みなとエンジニアリング', Customer.Type.CORPORATE, 'エンジニアリング'),
            ('CUST-0006', 'さくら物流', Customer.Type.CORPORATE, '物流'),
            ('CUST-0007', 'まつば金属', Customer.Type.CORPORATE, '金属加工'),
            ('CUST-0008', 'ひので電子', Customer.Type.CORPORATE, '電子'),
            ('CUST-0009', 'つばさオートパーツ', Customer.Type.CORPORATE, '自動車部品'),
            ('CUST-0010', 'ゆきぐに機材', Customer.Type.CORPORATE, '建機'),
            ('CUST-0011', 'まるみ商会', Customer.Type.CORPORATE, '卸売'),
            ('CUST-0012', 'こだまテクノ', Customer.Type.CORPORATE, '製造業'),
            ('CUST-0013', 'みらい資材センター', Customer.Type.CORPORATE, '建設資材'),
            ('CUST-0014', '個人事業主・架空太郎', Customer.Type.SOLE_PROPRIETOR, '電装'),
            ('CUST-0015', '見本花子（個人）', Customer.Type.INDIVIDUAL, ''),
        ]
        self.customers = [
            Customer.objects.create(
                customer_code=code,
                customer_name=name,
                customer_type=ctype,
                industry_type=industry,
                postal_code='100-0000',
                address='東京都千代田区サンプル1-1-1',
            )
            for code, name, ctype, industry in rows
        ]

    # ===== initial stock ===================================================

    def _create_initial_stock(self):
        """SKU を 各倉庫の対応エリアの 1〜7 棚に配置し manual_in で StockMovement 発行。"""
        self.stdout.write('  初期在庫構築...')

        # SKU の picking_type → 対象エリア の対応で、倉庫ごとに使えるロケーションを引いておく
        wh1_storage = list(
            Location.objects.filter(
                warehouse=self.wh1, area__location_type=Area.LocationType.STORAGE
            )
        )
        wh1_large = list(
            Location.objects.filter(
                warehouse=self.wh1, area__location_type=Area.LocationType.LARGE_ITEM
            )
        )
        wh2_storage = list(
            Location.objects.filter(
                warehouse=self.wh2, area__location_type=Area.LocationType.STORAGE
            )
        )
        wh2_large = list(
            Location.objects.filter(
                warehouse=self.wh2, area__location_type=Area.LocationType.LARGE_ITEM
            )
        )

        movements = []
        balances = {}  # (location_id, sku_id) -> quantity

        for sku in self.skus:
            if sku.picking_type == Sku.PickingType.TOTAL:
                primary = wh1_storage
                secondary = wh2_storage
                # 通常 SKU: WH01 に 3〜6 棚、WH02 にも 30% で 1〜2 棚
                primary_count = random.randint(3, 6)
                secondary_count = random.randint(1, 2) if random.random() < 0.3 else 0
                qty_range = (20, 80)
            else:  # ORDER (大型)
                primary = wh1_large
                secondary = wh2_large
                primary_count = random.randint(1, 3)
                secondary_count = 1 if random.random() < 0.2 else 0
                qty_range = (5, 25)

            for loc in random.sample(primary, min(primary_count, len(primary))):
                qty = random.randint(*qty_range)
                balances[(loc.id, sku.id)] = balances.get((loc.id, sku.id), 0) + qty
                movements.append((loc, sku, qty))

            if secondary_count and secondary:
                for loc in random.sample(secondary, min(secondary_count, len(secondary))):
                    qty = random.randint(*qty_range)
                    balances[(loc.id, sku.id)] = balances.get((loc.id, sku.id), 0) + qty
                    movements.append((loc, sku, qty))

        # 各 movement に過去日時を事前割り当て（その最古日を StockBalance.first_received_at に使う）
        movements_with_dates = [
            (loc, sku, qty, self._random_past_datetime(min_days=1, max_days=60))
            for loc, sku, qty in movements
        ]
        earliest_at = {}  # (loc_id, sku_id) -> earliest past datetime
        for loc, sku, qty, past in movements_with_dates:
            key = (loc.id, sku.id)
            if key not in earliest_at or past < earliest_at[key]:
                earliest_at[key] = past

        # StockBalance を一括作成（FIFO 並び替え用に first_received_at もセット）
        bal_objs = [
            StockBalance(
                location_id=loc_id,
                sku_id=sku_id,
                quantity=qty,
                first_received_at=earliest_at[(loc_id, sku_id)],
            )
            for (loc_id, sku_id), qty in balances.items()
        ]
        StockBalance.objects.bulk_create(bal_objs)

        # StockMovement を 1 件ずつ作成し、auto_now_add 後に moved_at/created_at を上書き
        # （manual_in 履歴を過去 60〜1日 にランダム分散）
        # 在庫の積み上げ順を再現するため、(loc,sku) 単位で時系列順に並べる必要は
        # ないが、quantity_before / quantity_after を整合させる必要がある。
        # 個別の loc×sku ごとに段階的に積み上げる。
        loc_sku_running = {}  # (loc_id, sku_id) -> running quantity
        for loc, sku, qty, past in movements_with_dates:
            key = (loc.id, sku.id)
            before = loc_sku_running.get(key, 0)
            after = before + qty
            loc_sku_running[key] = after
            m = StockMovement.objects.create(
                movement_type=StockMovement.MovementType.IN,
                location=loc,
                sku=sku,
                quantity=qty,
                quantity_before=before,
                quantity_after=after,
                reference_type=StockMovement.ReferenceType.MANUAL_IN,
                reference_id=None,
                note='[初期在庫] テストデータ生成',
                created_by=self.user,
            )
            # auto_now_add を上書き
            StockMovement.objects.filter(pk=m.pk).update(
                moved_at=past,
                created_at=past,
            )

    def _random_past_datetime(self, *, min_days, max_days):
        days = random.uniform(min_days, max_days)
        seconds = random.uniform(0, 86400)
        return self.now - timedelta(days=days, seconds=seconds)

    # ===== completed inbound (sample) ======================================

    def _create_completed_inbound(self):
        """完了済み入荷指示を 2 件作る（履歴一覧に表示するためのサンプル）。"""
        self.stdout.write('  完了済み入荷指示 2 件...')
        sample_skus_total = [s for s in self.skus if s.picking_type == Sku.PickingType.TOTAL]
        storage_locs = list(
            Location.objects.filter(
                warehouse=self.wh1, area__location_type=Area.LocationType.STORAGE
            )
        )

        for i in range(2):
            past_date = self.today - timedelta(days=random.randint(5, 25))
            past_dt = self._random_past_datetime(min_days=5, max_days=25)
            code = InboundOrder.next_code(past_date, InboundOrder.SourceType.MANUAL.value)
            order = InboundOrder.objects.create(
                inbound_order_code=code,
                warehouse=self.wh1,
                supplier=random.choice(self.suppliers),
                status=InboundOrder.Status.COMPLETED,
                expected_date=past_date,
                arrived_at=past_dt,
                received_at=past_dt + timedelta(hours=2),
                source_type=InboundOrder.SourceType.MANUAL,
                note='[テストデータ] 完了済み入荷指示',
                created_by=self.user,
            )
            for sku in random.sample(sample_skus_total, k=3):
                qty = random.randint(20, 60)
                item = InboundOrderItem.objects.create(
                    inbound_order=order,
                    sku=sku,
                    quantity_expected=qty,
                    quantity_received=qty,
                )
                loc = random.choice(storage_locs)
                # 在庫加算
                bal, _ = StockBalance.objects.get_or_create(
                    location=loc,
                    sku=sku,
                    defaults={'quantity': 0},
                )
                before = bal.quantity
                bal.quantity = before + qty
                # 0 → 正への切り替えで first_received_at をリセット（FIFO 用）
                update_fields = ['quantity', 'updated_at']
                if before == 0:
                    bal.first_received_at = past_dt + timedelta(hours=2)
                    update_fields.append('first_received_at')
                bal.save(update_fields=update_fields)

                m = StockMovement.objects.create(
                    movement_type=StockMovement.MovementType.IN,
                    location=loc,
                    sku=sku,
                    quantity=qty,
                    quantity_before=before,
                    quantity_after=bal.quantity,
                    reference_type=StockMovement.ReferenceType.INBOUND_ORDER,
                    reference_id=order.pk,
                    created_by=self.user,
                )
                StockMovement.objects.filter(pk=m.pk).update(
                    moved_at=past_dt + timedelta(hours=2),
                    created_at=past_dt + timedelta(hours=2),
                )
                InboundReceipt.objects.create(
                    inbound_order_item=item,
                    location=loc,
                    quantity_expected=qty,
                    quantity_received=qty,
                    quantity_defective=0,
                    discrepancy_type=InboundReceipt.DiscrepancyType.NONE,
                    stock_movement=m,
                    inspected_by=self.user,
                    putaway_by=self.user,
                    putaway_at=past_dt + timedelta(hours=2),
                )

    # ===== completed outbound (sample) =====================================

    def _create_completed_outbound(self):
        """完了済み出荷指示を 2 件作る（履歴一覧に表示するためのサンプル）。"""
        self.stdout.write('  完了済み出荷指示 2 件...')
        # 在庫が複数棚にあって出庫しても 0 にならない SKU を選ぶ
        candidate_balances = list(
            StockBalance.objects.filter(
                location__warehouse=self.wh1,
                quantity__gte=10,
                sku__picking_type=Sku.PickingType.TOTAL,
            ).select_related('sku', 'location')[:50]
        )
        if not candidate_balances:
            return

        for i in range(2):
            past_date = self.today - timedelta(days=random.randint(3, 20))
            past_dt = self._random_past_datetime(min_days=3, max_days=20)
            code = OutboundOrder.next_code(past_date, OutboundOrder.SourceType.MANUAL.value)
            customer = random.choice(self.customers)
            order = OutboundOrder.objects.create(
                outbound_order_code=code,
                warehouse=self.wh1,
                customer=customer,
                status=OutboundOrder.Status.SHIPPED,
                source_type=OutboundOrder.SourceType.MANUAL,
                delivery_name=f'{customer.customer_name} 御中',
                delivery_address=customer.address,
                delivery_postal_code=customer.postal_code,
                shipped_at=past_dt + timedelta(hours=3),
                note='[テストデータ] 完了済み出荷指示',
                created_by=self.user,
            )

            # 2 SKU 選ぶ（ShipmentItem/DeliveryNoteItem は (shipment, sku) ユニーク
            # かつ OutboundOrderItem も (order, sku, location) ユニークなので、
            # SKU が被らないよう SKU 単位で集約してからサンプリングする）
            by_sku = {}
            for b in candidate_balances:
                by_sku.setdefault(b.sku_id, b)
            unique_balances = list(by_sku.values())
            picks = random.sample(unique_balances, k=min(2, len(unique_balances)))
            shipment_code = Shipment.next_code(past_date)
            shipment = Shipment.objects.create(
                shipment_code=shipment_code,
                outbound_order=order,
                status=Shipment.Status.SHIPPED,
                carrier_name=random.choice(['ヤマト運輸', '佐川急便', '日本郵便']),
                tracking_number=f'TR{random.randint(10000000, 99999999)}',
                shipped_at=past_dt + timedelta(hours=3),
                inspected_by=self.user,
                inspected_at=past_dt + timedelta(hours=2),
                created_by=self.user,
            )
            delivery = DeliveryNote.objects.create(
                delivery_note_code=f'DN-{past_date.strftime("%Y%m%d")}-{i + 1:03d}',
                outbound_order=order,
                customer=customer,
            )
            for bal in picks:
                qty = min(random.randint(2, 5), bal.quantity)
                if qty <= 0:
                    continue
                item = OutboundOrderItem.objects.create(
                    outbound_order=order,
                    sku=bal.sku,
                    location=bal.location,
                    quantity_ordered=qty,
                    quantity_shipped=qty,
                )
                before = bal.quantity
                bal.quantity = before - qty
                bal.save(update_fields=['quantity', 'updated_at'])

                m = StockMovement.objects.create(
                    movement_type=StockMovement.MovementType.OUT,
                    location=bal.location,
                    sku=bal.sku,
                    quantity=-qty,
                    quantity_before=before,
                    quantity_after=bal.quantity,
                    reference_type=StockMovement.ReferenceType.OUTBOUND_ORDER,
                    reference_id=order.pk,
                    created_by=self.user,
                )
                StockMovement.objects.filter(pk=m.pk).update(
                    moved_at=past_dt + timedelta(hours=3),
                    created_at=past_dt + timedelta(hours=3),
                )
                ShipmentItem.objects.create(
                    shipment=shipment,
                    outbound_order_item=item,
                    sku=bal.sku,
                    quantity_shipped=qty,
                    stock_movement=m,
                )
                DeliveryNoteItem.objects.create(
                    delivery_note=delivery,
                    outbound_order_item=item,
                    sku=bal.sku,
                    product_name=bal.sku.product.product_name,
                    sku_code=bal.sku.sku_code,
                    quantity=qty,
                )

    # ===== pending inbound =================================================

    def _create_pending_inbound(self):
        """未着手の入荷指示を 38 件作る。"""
        self.stdout.write('  未着手 入荷指示 38 件...')

        # 構成: ASN 20件 / manual 12件 / return 6件
        source_distribution = (
            [InboundOrder.SourceType.ASN] * 20
            + [InboundOrder.SourceType.MANUAL] * 12
            + [InboundOrder.SourceType.RETURN] * 6
        )
        random.shuffle(source_distribution)

        # 採番用カウンタ（ASN は外部システムを模した形式にする）
        asn_seq = 1
        # manual / return の next_code は同じ日付内で連番が必要 → 日付ごとにメモ
        for st in source_distribution:
            # 入荷予定日: 近未来 -3〜+14日 にバラけさせる
            offset = random.randint(-3, 14)
            expected_date = self.today + timedelta(days=offset)

            if st == InboundOrder.SourceType.ASN:
                # ASN は外部システムの番号をそのまま使う
                code = f'ASN-{expected_date.strftime("%Y%m%d")}-{asn_seq:04d}'
                asn_seq += 1
                supplier = random.choice(self.suppliers)
                po = f'PO-{random.randint(100000, 999999)}'
                delivery_note = f'DN-{random.randint(100000, 999999)}'
            elif st == InboundOrder.SourceType.MANUAL:
                code = InboundOrder.next_code(expected_date, st.value)
                supplier = random.choice(self.suppliers)
                po = ''
                delivery_note = ''
            else:  # RETURN
                code = InboundOrder.next_code(expected_date, st.value)
                supplier = None
                po = ''
                delivery_note = ''

            order = InboundOrder.objects.create(
                inbound_order_code=code,
                warehouse=self.wh1,
                supplier=supplier,
                status=InboundOrder.Status.RECEIVING_WAIT,
                expected_date=expected_date,
                purchase_order_code=po,
                supplier_delivery_note_code=delivery_note,
                source_type=st,
                note='' if st != InboundOrder.SourceType.RETURN else '返品受入予定',
                created_by=self.user,
            )

            # 明細 1〜4 SKU
            item_count = random.randint(1, 4)
            chosen = random.sample(self.skus, k=item_count)
            for sku in chosen:
                qty = random.choice([5, 10, 20, 30, 50, 100])
                InboundOrderItem.objects.create(
                    inbound_order=order,
                    sku=sku,
                    quantity_expected=qty,
                )

    # ===== pending outbound ================================================

    def _create_pending_outbound(self):
        """未着手の出荷指示を 68 件作る。"""
        self.stdout.write('  未着手 出荷指示 68 件...')

        source_distribution = (
            [OutboundOrder.SourceType.OMS] * 40
            + [OutboundOrder.SourceType.MANUAL] * 22
            + [OutboundOrder.SourceType.RETURN] * 6
        )
        random.shuffle(source_distribution)

        oms_seq = 1
        for st in source_distribution:
            # 出荷期限: 近未来
            offset = random.randint(-1, 10)
            deadline_dt = self.now + timedelta(days=offset, hours=random.randint(0, 23))

            if st == OutboundOrder.SourceType.OMS:
                code = f'OMS-{deadline_dt.strftime("%Y%m%d")}-{oms_seq:04d}'
                external = f'EC-{random.randint(1000000, 9999999)}'
                oms_seq += 1
            elif st == OutboundOrder.SourceType.MANUAL:
                code = OutboundOrder.next_code(deadline_dt.date(), st.value)
                external = ''
            else:  # RETURN
                code = OutboundOrder.next_code(deadline_dt.date(), st.value)
                external = ''

            customer = random.choice(self.customers)
            order = OutboundOrder.objects.create(
                outbound_order_code=code,
                warehouse=self.wh1,
                customer=customer,
                external_order_id=external,
                status=OutboundOrder.Status.ALLOCATION_WAIT,
                source_type=st,
                deadline_at=deadline_dt,
                delivery_name=f'{customer.customer_name} 御中',
                delivery_address=customer.address,
                delivery_postal_code=customer.postal_code,
                note='' if st != OutboundOrder.SourceType.RETURN else '返品出荷予定',
                created_by=self.user,
            )

            # 明細 1〜3 SKU、引き当てが通る範囲の数量で（在庫合計の半分以下）
            item_count = random.randint(1, 3)
            chosen = random.sample(self.skus, k=item_count)
            for sku in chosen:
                qty = random.choice([1, 2, 3, 5, 8])
                # 既存制約 (outbound_order, sku, location) のため、location は NULL のまま
                OutboundOrderItem.objects.create(
                    outbound_order=order,
                    sku=sku,
                    quantity_ordered=qty,
                )

    # ===== error logs ======================================================

    def _create_error_logs(self):
        self.stdout.write('  エラーログ 10 件...')
        ET = ErrorLog.ErrorType
        samples = [
            (
                ET.IMPORT,
                timedelta(hours=2),
                'OMS取込: 必須項目「配送先住所」が空のため取り込めません',
                'OMS連携バッチ',
                '',
                'OMS-2026-000457',
                False,
                False,
            ),
            (
                ET.IMPORT,
                timedelta(hours=4),
                'OMS取込: SKU「SKU-999999」がマスタに存在しません',
                'OMS連携バッチ',
                '',
                'OMS-2026-000458',
                False,
                False,
            ),
            (
                ET.IMPORT,
                timedelta(days=1, hours=3),
                'OMS取込: 数量が不正です（quantity=-2）',
                'OMS連携バッチ',
                '',
                'OMS-2026-000461',
                False,
                True,
            ),
            (
                ET.IMPORT,
                timedelta(days=2),
                'CSV取込: 区切り文字の解釈に失敗（明細行が分離できません）',
                'マスタCSV取込',
                '',
                '',
                False,
                False,
            ),
            (
                ET.IMPORT,
                timedelta(days=3),
                'OMS取込: 同一OMS注文番号が既に登録済み',
                'OMS連携バッチ',
                '',
                'OMS-2026-000482',
                False,
                True,
            ),
            (
                ET.EXCEPTION,
                timedelta(hours=5),
                "ValueError: invalid literal for int() with base 10: 'たくさん'",
                '/outbound/orders/new/',
                'POST',
                '',
                True,
                False,
            ),
            (
                ET.EXCEPTION,
                timedelta(days=1, hours=8),
                'IntegrityError: duplicate key on uk_locations_warehouse_code',
                '/masters/locations/new/',
                'POST',
                '',
                True,
                True,
            ),
            (
                ET.EXCEPTION,
                timedelta(days=2, hours=1),
                'Sku.DoesNotExist: Sku matching query does not exist.',
                '/inbound/orders/12/edit/',
                'POST',
                '',
                True,
                True,
            ),
            (
                ET.EXCEPTION,
                timedelta(days=4),
                'StockBalance.DoesNotExist: requested balance row not found',
                '/stock/transfer/',
                'POST',
                '',
                True,
                False,
            ),
            (
                ET.EXCEPTION,
                timedelta(days=6),
                'PermissionDenied: 別の作業担当者がロック中です',
                '/inbound/putaway/15/work/',
                'POST',
                '',
                True,
                True,
            ),
        ]
        for etype, ago, summary, source, method, ref, with_user, resolved in samples:
            occurred = self.now - ago
            ErrorLog.objects.create(
                error_type=etype,
                occurred_at=occurred,
                summary=summary,
                detail=summary + '\n\n（テストデータ）',
                source=source,
                request_method=method,
                reference=ref,
                user=self.user if with_user else None,
                is_resolved=resolved,
                resolved_at=(occurred + timedelta(hours=2)) if resolved else None,
            )

    # ===== stocktake sample ================================================

    def _create_stocktakes(self):
        """棚卸セッションのサンプル（計画中 1 / 完了 1）。"""
        self.stdout.write('  棚卸セッション 2 件...')
        StocktakeSession.objects.create(
            session_code=StocktakeSession.next_code(self.today),
            warehouse=self.wh1,
            stocktake_type=StocktakeSession.StocktakeType.CYCLE,
            area=self.area_a,
            status=StocktakeSession.Status.PLANNING,
            planned_at=self.today + timedelta(days=3),
            note='[テストデータ] 月次循環棚卸（A エリア）',
            created_by=self.user,
        )
        past = self.today - timedelta(days=20)
        StocktakeSession.objects.create(
            session_code=StocktakeSession.next_code(past),
            warehouse=self.wh1,
            stocktake_type=StocktakeSession.StocktakeType.CYCLE,
            area=self.area_b,
            status=StocktakeSession.Status.COMPLETED,
            planned_at=past,
            started_at=self.now - timedelta(days=20),
            completed_at=self.now - timedelta(days=20) + timedelta(hours=3),
            note='[テストデータ] 完了済み循環棚卸（B エリア）',
            created_by=self.user,
        )

    # ===== summary =========================================================

    def _print_summary(self):
        self.stdout.write(self.style.SUCCESS('--- 投入後の件数 ---'))
        rows = [
            ('User', get_user_model().objects.count()),
            ('Warehouse', Warehouse.objects.count()),
            ('Area', Area.objects.count()),
            ('Location', Location.objects.count()),
            ('Category', Category.objects.count()),
            ('Manufacturer', Manufacturer.objects.count()),
            ('Product', Product.objects.count()),
            ('Sku', Sku.objects.count()),
            ('Supplier', Supplier.objects.count()),
            ('Customer', Customer.objects.count()),
            ('StockBalance', StockBalance.objects.count()),
            ('StockMovement', StockMovement.objects.count()),
            ('InboundOrder', InboundOrder.objects.count()),
            ('InboundOrderItem', InboundOrderItem.objects.count()),
            ('InboundReceipt', InboundReceipt.objects.count()),
            ('OutboundOrder', OutboundOrder.objects.count()),
            ('OutboundOrderItem', OutboundOrderItem.objects.count()),
            ('Shipment', Shipment.objects.count()),
            ('ShipmentItem', ShipmentItem.objects.count()),
            ('DeliveryNote', DeliveryNote.objects.count()),
            ('StocktakeSession', StocktakeSession.objects.count()),
            ('ErrorLog', ErrorLog.objects.count()),
        ]
        for name, count in rows:
            self.stdout.write(f'  {name:<20} {count:>6}')
