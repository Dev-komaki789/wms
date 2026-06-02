# WMS — 倉庫管理システム

実運用を想定して設計・実装した倉庫管理システム（Warehouse Management System）です。
マスタ管理から入出荷・在庫・棚卸・発注アラートまでの一連の業務を、PC とハンディ端末の両方で完結できます。

🌐 **本番サイト**: <https://komaki-wms.com>（AWS EC2 + RDS でホスティング）
📁 **リポジトリ**: 本リポジトリ
👤 **作者**: 個人開発 / 転職用ポートフォリオ

![ホーム画面](screenshots/01-home.png)

---

## 目次

- [このプロジェクトについて](#このプロジェクトについて)
- [主要機能](#主要機能)
- [スクリーンショット](#スクリーンショット)
- [技術スタック](#技術スタック)
- [設計のこだわり](#設計のこだわり)
- [アーキテクチャ](#アーキテクチャ)
- [ローカル起動手順](#ローカル起動手順)
- [プロジェクト構成](#プロジェクト構成)
- [CI / デプロイ](#ci--デプロイ)
- [ロードマップ](#ロードマップ)

---

## このプロジェクトについて

ECや製造業の物流現場で使われる **WMS（倉庫管理システム）** を、ゼロから設計・実装した個人開発プロジェクトです。

業務系 Web アプリの一般的な要素（マスタ CRUD・帳票・CSV 入出力・権限管理・履歴管理）に加えて、現場のハンディ端末を想定した **スマホ最適化画面 + カメラによるバーコードスキャン** までを含めています。

### 規模感（2026-05 現在）

| 指標 | 値 |
| --- | --- |
| アプリ数（Django app） | 6（masters / stock / inbound / outbound / accounts / core） |
| モデル数 | 31 クラス（テーブル設計上は 41 テーブル） |
| ビュークラス数 | 116 |
| HTML テンプレート数 | 83 |
| Python コード行数（migrations 除く） | 約 13,700 行 |
| マイグレーション数 | 累計 25 |

---

## 主要機能

### マスタ管理
- 倉庫 / エリア / ロケーション / カテゴリ / メーカー / 商品 / SKU / 顧客 / 仕入先 の **8 マスタ**
- 一覧 / 検索 / 作成 / 編集 / 削除（CRUD）+ **CSV インポート / エクスポート**（区切り文字 `,` / `^` 選択可）
- FK は **PROTECT 中心**：参照されているマスタは削除させない（誤削除防止）

### 在庫管理
- **在庫照会**：倉庫・エリア・ロケーション・SKU で絞り込み、CSV エクスポート可
- **入出庫履歴**：StockMovement を時系列で表示、参照元（入荷指示 / 出荷指示 / 棚卸調整 / 手動）まで追跡
- **棚移動**：棚→棚の在庫移動を 1 トランザクションで処理
- **棚卸**：全数棚卸 / 循環棚卸の両方をサポート（全数中の在庫変動はロックでブロック）

### 入荷管理
- 入荷指示の作成（手動 / ASN / 返品）→ 検品 → 格納 → 完了
- 入荷予定リストを **A4 印刷**（Code128 バーコード入り）

### 出荷管理
- 出荷指示の作成（手動 / OMS / 返品）→ 引き当て → ピッキング → 検品 → 出荷
- ピッキングリスト・送り状の印刷
- 引き当てロジックは別ドキュメントで設計（FIFO ベース）

### 発注設定 + 発注アラート
- SKU ごとに発注点 / 発注量を設定
- 在庫の更新（`StockBalance.post_save`）をフックして **自動でアラート生成**
- アラート → 入荷指示への 1 クリック変換、入荷完了で自動 resolved

### ハンディ作業者ロール
- 専用グループのユーザーは **ハンディ画面しか開けない**（`HandheldOnlyMiddleware`）
- 入荷検品 / 入荷格納 / 棚卸カウント / 出荷ピッキング をスマホで完結
- **カメラスキャン**（BarcodeDetector API + Canvas 切り出しによる枠内検出）
- **振動フィードバック**（Android のみ・処理結果を体感で通知）

### 印刷帳票
- すべて **A4 印刷を前提に CSS で組版**（外部 PDF ライブラリ不使用）
- **Code128 バーコードを自前実装**（印刷側と読取側を密結合で安定動作させるため）

### エラーログ
- 取込エラー（OMS / CSV）と例外発生（500 系）を一元管理
- 対応済み / 未対応のステータス管理

---

## スクリーンショット

> 画面はすべて Bootstrap 5 ベース。PC（業務）とスマホ（ハンディ）でレイアウトと操作系を分けています。

### ホーム / 業務ハブ
KPI サマリーと機能カテゴリ別のカードグリッド。ナビバーに発注アラートのバッジ表示。

![ホーム画面](screenshots/01-home.png)

### マスタ照会（SKU）
共通の照会画面パターン（フィルタ + 並び替え + CSV インポート / エクスポート + ページネーション）。

![SKU マスタ照会](screenshots/02-master-sku.png)

### 在庫照会
倉庫・エリア・ロケーション・SKU を AND で絞り込み。CSV 出力もそのまま絞り込み条件を反映。

![在庫照会](screenshots/03-stock-inquiry.png)

### ハンディ画面（出荷ピッキング）
スマホ画面に合わせた縦長レイアウト。カメラでバーコード読取 → 数量入力 → 次の棚へ進む。

![ハンディ：出荷ピッキング](screenshots/04-handheld-picking.png)

### 発注アラート照会
在庫が発注点を割ったら自動生成。ワンクリックで入荷指示に変換し、入荷完了で resolved 自動更新。

![発注アラート照会](screenshots/05-reorder-alert.png)

### 印刷帳票（ピッキングリスト）
ブラウザ印刷でそのまま A4 出力。Code128 バーコードはサーバ側 SVG 生成。

![ピッキングリスト印刷](screenshots/06-print-picking-list.png)

### ER 図
41 テーブル構成。FK PROTECT を基本とし、参照先のあるマスタは削除させない設計。

![ER 図](screenshots/07-er-diagram.png)

---

## 技術スタック

### バックエンド
- **Python 3.12**
- **Django 5.2**（MTV / Class-Based Views 中心）
- **PostgreSQL 17**
- **gunicorn**（WSGI / `--preload --timeout 60`）
- **whitenoise**（静的ファイル配信）

### フロントエンド
- **Bootstrap 5**（テーマカスタマイズなし）
- **素の JavaScript**（フレームワーク不使用 / `static/wms.js` に共通化）
- **BarcodeDetector API**（カメラスキャン・Chromium 系のみ）
- **PWA**（manifest + apple-touch-icon、スマホのホーム画面追加対応）

### 開発 / インフラ
- **uv**（依存管理 / `uv.lock` で再現性確保）
- **ruff**（lint + format）
- **Docker Compose**（開発用 PostgreSQL のみ）
- **GitHub Actions**（CI：ruff + Django check + makemigrations --check）
- **AWS EC2** (Ubuntu 24.04) **+ RDS** (PostgreSQL) + **nginx** + **Let's Encrypt**（本番）

---

## 設計のこだわり

### 1. FK PROTECT を基本にした「壊れないマスタ」
削除の連鎖を起こさず、参照されているマスタは削除できないようにしています（`on_delete=PROTECT`）。
削除しようとすると、依存している件数を画面に表示して止めます。これは実運用で **「誤って親マスタを消す」事故** を防ぐためです。

### 2. ハンディ専用ロールの徹底
`HandheldOnlyMiddleware` で、ハンディ作業者グループのユーザーは指定されたプレフィックスの URL しか開けないようにしています。
AJAX API もこの ALLOWED_PREFIXES に登録する必要があり、追加時の取りこぼし防止のため **demo ユーザー（worker1）での実機確認** をワークフロー化しています。

### 3. 在庫の二重表現（StockBalance / StockMovement）
- `StockBalance`：現在在庫を `(location, sku)` ユニーク制約で 1 行ずつ保持
- `StockMovement`：すべての増減を時系列で追記（before / after / 参照元タイプ / 参照元 ID）

これによって「現在いくつあるか」と「いつ・なぜ増減したか」を両立しています。発注アラートは `StockBalance.post_save` シグナルで発火（`StockMovement.post_save` だと早すぎて誤動作することが分かったため）。

### 4. 業務画面の UX 規約を全画面で統一
- Enter / ↑ / ↓ で次のフィールドへ移動（業務端末ライクなキーボード操作）
- 送信ボタンに `data-confirm` 属性を付けて確認モーダルを自動表示
- マスタ一覧は `align-middle` + `font-monospace` でコード列を等幅
- 「PC 画面は最大 1200px の narrow レイアウト」など、設計を `docs/画面仕様規約.md` に明文化

### 5. CSV 入出力は自前実装
Django ライブラリに頼らず、区切り文字（`,` / `^`）と文字コードを画面から選べるようにしています。
8 マスタ + 入荷指示 + 出荷指示に対応。エラー時は行番号付きで返します。

### 6. 印刷帳票も自前実装
外部 PDF ライブラリ不使用。CSS の `@media print` と `@page` だけで A4 帳票を組み、Code128 バーコードは SVG を Python 側で生成しています（読取側のカメラ検出器と同じ仕様にすることで安定動作）。

### 7. テストデータ生成コマンド
`reset_and_seed` 1 コマンドで、ユーザー以外を削除して中規模データ（SKU 100 / ロケーション 192 / 在庫 約 500 / 入出庫履歴 約 600）を投入できます。乱数シード指定可。

---

## アーキテクチャ

```
┌───────────────┐
│  ブラウザ      │
│ (PC / スマホ)  │
└───────┬───────┘
        │ HTTPS
        ▼
┌───────────────┐
│  nginx         │ ← Let's Encrypt（証明書）
│ (静的配信)     │
└───────┬───────┘
        │ proxy_pass
        ▼
┌───────────────┐
│  gunicorn      │ ← systemd で常駐
│ (WSGI)         │
└───────┬───────┘
        │ Django (MTV)
        ▼
┌───────────────┐
│ PostgreSQL     │
│   (RDS)        │
└───────────────┘
```

主要な処理フロー：

| 業務 | 主なテーブル |
| --- | --- |
| 入荷 | `InboundOrder → InboundOrderItem → InboundReceipt → StockMovement → StockBalance` |
| 出荷 | `OutboundOrder → OutboundOrderItem → StockReservation → PickingList → Shipment → StockMovement → StockBalance` |
| 棚卸 | `StocktakeSession → StocktakeItem → StocktakeAdjustment → StockMovement → StockBalance` |
| 発注アラート | `StockBalance.post_save → SkuReorderSetting と照合 → ReorderAlert 生成` |

---

## ローカル起動手順

### 前提
- Python 3.12
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker / Docker Compose（開発用 PostgreSQL を起動するため）

### 手順

```bash
# 1. 依存インストール
uv sync

# 2. 環境変数ファイル作成
cp .env.example .env
# DJANGO_SECRET_KEY を生成して .env に書く:
uv run python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# 3. PostgreSQL 起動（Docker Compose）
docker compose up -d

# 4. マイグレーション
uv run python manage.py migrate

# 5. 管理者ユーザー作成
uv run python manage.py createsuperuser

# 6. （任意）テストデータ投入
uv run python manage.py reset_and_seed --yes

# 7. 開発サーバー起動
uv run python manage.py runserver
```

ブラウザで <http://localhost:8000/> にアクセス。

`reset_and_seed` を実行すると、ハンディ専用ユーザー `worker1` も作成されます（ハンディ画面の動作確認用）。本番環境のパスワードは公開していません。ハンディ画面のデモを希望される方は個別にご連絡ください。

### よく使うコマンド

| 目的 | コマンド |
| --- | --- |
| 開発サーバー起動 | `uv run python manage.py runserver` |
| マイグレーション作成 | `uv run python manage.py makemigrations` |
| マイグレーション適用 | `uv run python manage.py migrate` |
| Django shell | `uv run python manage.py shell` |
| lint | `uv run ruff check .` |
| format | `uv run ruff format .` |
| DB 起動 | `docker compose up -d` |
| DB 停止 | `docker compose down` |
| DB データごと削除 | `docker compose down -v` |
| テストデータ再投入 | `uv run python manage.py reset_and_seed --yes` |

### 環境変数

| 変数名 | 用途 |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django のシークレットキー |
| `DJANGO_DEBUG` | デバッグモード（`True` / `False`） |
| `DJANGO_ALLOWED_HOSTS` | 許可ホスト（カンマ区切り） |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | CSRF 許可オリジン（本番のみ必須） |
| `POSTGRES_HOST` | DB ホスト |
| `POSTGRES_PORT` | DB ポート（デフォルト `5432`） |
| `POSTGRES_DB` | DB 名 |
| `POSTGRES_USER` | DB ユーザー |
| `POSTGRES_PASSWORD` | DB パスワード |

---

## プロジェクト構成

```
wms/
├── accounts/           # カスタム User / ハンディ作業者ロール / 権限ミドルウェア
├── masters/            # 8 マスタ（倉庫・エリア・棚・カテゴリ・メーカー・商品・SKU・顧客・仕入先）
├── stock/              # 在庫・履歴・棚移動・棚卸・発注設定・発注アラート
├── inbound/            # 入荷指示・検品・格納
├── outbound/           # 出荷指示・引当・ピッキング・検品・出荷
├── core/               # ホーム画面 / エラーログ / 共通ミックスイン / テストデータ生成コマンド
├── config/             # Django プロジェクト設定（settings.py / urls.py）
├── templates/          # 共通テンプレート（a/base.html など）+ ハンディ用テンプレート
├── static/             # CSS / wms.js（共通 JS）/ ロゴ / favicon / manifest
├── deploy/             # gunicorn.service / nginx.conf（本番デプロイ用）
├── sample_csv/         # マスタ CSV のサンプル
├── screenshots/        # README で参照しているスクリーンショット
├── docker-compose.yml  # 開発用 PostgreSQL
├── pyproject.toml      # 依存定義（uv）
├── uv.lock
├── DEPLOY.md           # 本番デプロイ手順（AWS EC2 + RDS）
└── README.md
```

---

## CI / デプロイ

### CI（GitHub Actions）
`master` への push と PR で自動実行：

1. `ruff check`（lint）
2. `ruff format --check`（フォーマット）
3. `python manage.py check`（Django 設定 / モデル / URL 整合性）
4. `python manage.py makemigrations --check`（マイグレーション漏れ検出）

### 本番デプロイ
詳細は [DEPLOY.md](DEPLOY.md) を参照。
要点：

- **EC2**（Ubuntu 24.04 / t3.small）+ **RDS**（PostgreSQL t3.micro / 単一AZ）
- **nginx**（HTTPS 終端 + 静的配信）→ **gunicorn**（systemd 常駐）→ **Django**
- **Let's Encrypt**（certbot）で HTTPS 化
- **PWA**（manifest + apple-touch-icon）でスマホのホーム画面追加に対応

CD は組まず、コード反映は SSH での `git pull` → `migrate` → `collectstatic` → `systemctl restart gunicorn` を手動で行う運用です（個人ポートフォリオなのでオーバースペックを避けた判断）。

---

## ロードマップ

### 直近で予定しているもの
- **ダッシュボード**：Chart.js などで KPI（在庫推移 / ピッキング件数 / エラー率）を時系列で可視化
- **REST API 化**：DRF を導入して内部処理を API として公開

### 構想中
- **EC サイト連携**：別リポジトリで Next.js + DRF のSPA を作り、WMS と API で連携（業務系 MPA × 顧客向け SPA の使い分けデモ）
- **pytest テスト整備**：CI に組み込み済み・テストコード追加
- **AGV / 種まきピッキング**：データモデルは予約済み（`PickingType.TOTAL`）、UI 未実装

---

## ライセンス / クレジット

個人開発のポートフォリオ用リポジトリです。再配布や商用利用は想定していません。

会社名・人名・住所は架空のものを使用しています（テストデータ・スクリーンショット内も含む）。
