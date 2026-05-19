"""core アプリの共通ユーティリティ。"""
from django.utils.dateparse import parse_date


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
