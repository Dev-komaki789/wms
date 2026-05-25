"""core アプリの共通ユーティリティ。"""
from django.core.paginator import Paginator
from django.utils.dateparse import parse_date

# 一覧・照会画面の1ページあたりの表示件数
PAGE_SIZE = 50


def parse_query_date(value):
    """検索フォームの日付文字列を date に変換する。不正なら None を返す。

    <input type="date"> は仕様上6桁年（最大275760年）まで許容するため、
    生の文字列をそのまま QuerySet.filter() に渡すと date 変換で例外が起きる
    （例: 100000-01-01）。検索ビューはこのヘルパーを通し、戻り値が None で
    ないときだけ日付フィルタを適用することで、異常値でも例外にせず無視する。

    None を返すケース: 空文字 / 桁数異常の年 / 存在しない日付（13月など）。
    """
    if not value:
        return None
    try:
        return parse_date(value)
    except ValueError:
        # parse_date は書式は正しいが存在しない日付（2026-13-01 等）で ValueError
        return None


def paginate(request, queryset, per_page=PAGE_SIZE):
    """queryset を1ページ per_page 件でページネーションし Page を返す。

    検索-first 系の TemplateView から使う（ListView は paginate_by で十分）。
    ?page= が無効値・範囲外でも例外にせず先頭/末尾ページにフォールバックする
    （Paginator.get_page の挙動）。サマリー集計は呼び出し側が元の queryset に
    対して別途行うこと（ページ送りしてもサマリーは全件ベースを保つ）。
    """
    return Paginator(queryset, per_page).get_page(request.GET.get('page'))


def apply_ordering(request, queryset, sortable, default_key, default_dir='asc'):
    """?sort=&dir= に応じて queryset を並べ替える（見出しクリックの並び替え用）。

    sortable: {sort_key: [orm_field, ...]} の許可リスト。並べ替え対象を限定して
              任意フィールドでの order_by 注入を防ぐ。先頭が主キー、以降は同値時の
              副キー。direction（asc/desc）は各フィールドに適用する。
    default_key / default_dir: ?sort= 未指定時の既定。
    戻り値: (並べ替え済み queryset, 採用した sort_key, 'asc'|'desc')。
            sort_key/dir はテンプレートの見出し（sort_th）に渡して矢印表示に使う。
    """
    key = request.GET.get('sort') or default_key
    direction = request.GET.get('dir') or default_dir
    if key not in sortable:
        key, direction = default_key, default_dir
    if direction not in ('asc', 'desc'):
        direction = 'asc'
    prefix = '-' if direction == 'desc' else ''
    order_args = [f'{prefix}{f}' for f in sortable[key]]
    order_args.append('pk')  # 安定ソートの最終副キー
    return queryset.order_by(*order_args), key, direction


class GetPageMixin:
    """ListView の ?page= が不正値・範囲外でも 404 にせずフォールバックさせる。

    既定の paginate_queryset は不正な page で Http404 を投げるが、ページ送り
    リンクは常に有効な番号を出すため不正 page は手入力や古いブックマーク等。
    検索-first 系の paginate() ヘルパー（get_page 使用）と挙動を揃える。
    """

    def paginate_queryset(self, queryset, page_size):
        paginator = self.get_paginator(
            queryset, page_size, orphans=self.get_paginate_orphans(),
            allow_empty_first_page=self.get_allow_empty(),
        )
        page = paginator.get_page(self.request.GET.get(self.page_kwarg))
        return (paginator, page, page.object_list, page.has_other_pages())
