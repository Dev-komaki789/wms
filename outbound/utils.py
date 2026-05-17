"""出荷アプリのユーティリティ。"""

from django.db import IntegrityError, transaction


def create_with_retry(make, *, attempts=5):
    """`make()` を実行し、unique 制約違反なら採り直して再実行する。

    出荷指示番号・ピッキングリスト番号などの採番(`next_code`)は「既存の最大
    番号+1」方式でロックを取らないため、同時実行で同じコードを生成しうる。
    `make` は「コードを採番してオブジェクトを create する」呼び出し可能とし、
    衝突(IntegrityError)時は savepoint 単位でロールバックして `make` をやり直す。
    呼び出し側の transaction.atomic() 内から使うこと（内側の atomic は savepoint）。
    """
    last_exc = None
    for _ in range(attempts):
        try:
            with transaction.atomic():
                return make()
        except IntegrityError as exc:
            last_exc = exc
    raise last_exc


# Code39 バーコードの文字 → エレメント幅パターン（9 エレメント: 太線/細線）。
# 各文字は 5 本のバー + 4 本のスペースの 9 エレメントで、うち 3 本が太(w)。
# index 偶数=バー / 奇数=スペース。文字列は開始/終了文字 '*' で挟む。
_CODE39 = {
    '0': 'nnnwwnwnn', '1': 'wnnwnnnnw', '2': 'nnwwnnnnw', '3': 'wnwwnnnnn',
    '4': 'nnnwwnnnw', '5': 'wnnwwnnnn', '6': 'nnwwwnnnn', '7': 'nnnwnnwnw',
    '8': 'wnnwnnwnn', '9': 'nnwwnnwnn',
    'A': 'wnnnnwnnw', 'B': 'nnwnnwnnw', 'C': 'wnwnnwnnn', 'D': 'nnnnwwnnw',
    'E': 'wnnnwwnnn', 'F': 'nnwnwwnnn', 'G': 'nnnnnwwnw', 'H': 'wnnnnwwnn',
    'I': 'nnwnnwwnn', 'J': 'nnnnwwwnn', 'K': 'wnnnnnnww', 'L': 'nnwnnnnww',
    'M': 'wnwnnnnwn', 'N': 'nnnnwnnww', 'O': 'wnnnwnnwn', 'P': 'nnwnwnnwn',
    'Q': 'nnnnnnwww', 'R': 'wnnnnnwwn', 'S': 'nnwnnnwwn', 'T': 'nnnnwnwwn',
    'U': 'wwnnnnnnw', 'V': 'nwwnnnnnw', 'W': 'wwwnnnnnn', 'X': 'nwnnwnnnw',
    'Y': 'wwnnwnnnn', 'Z': 'nwwnwnnnn',
    '-': 'nwnnnnwnw', '.': 'wwnnnnwnn', ' ': 'nwwnnnwnn', '*': 'nwnnwnwnn',
    '$': 'nwnwnwnnn', '/': 'nwnwnnnwn', '+': 'nwnnnwnwn', '%': 'nnnwnwnwn',
}

_NARROW = 2          # 細エレメントの幅
_WIDE = 5            # 太エレメントの幅
_BAR_HEIGHT = 56     # バーの高さ
_QUIET = 16          # 両端のクワイエットゾーン（余白）


def code39_svg(text):
    """文字列を Code39 バーコードの SVG 文字列に変換する（依存ライブラリなし）。

    ピッキングリスト番号など英数字・ハイフンのコードを想定。Code39 が扱えない
    文字は無視する。戻り値はそのまま HTML に埋め込める <svg> 文字列。
    """
    text = (text or '').upper()
    chars = [c for c in text if c in _CODE39 and c != '*']
    sequence = ['*'] + chars + ['*']

    bars = []
    x = _QUIET
    for pos, ch in enumerate(sequence):
        for idx, weight in enumerate(_CODE39[ch]):
            width = _WIDE if weight == 'w' else _NARROW
            if idx % 2 == 0:  # 偶数 index = バー（黒）
                bars.append(
                    f'<rect x="{x}" y="0" width="{width}" height="{_BAR_HEIGHT}"/>'
                )
            x += width
        if pos < len(sequence) - 1:
            x += _NARROW  # 文字間ギャップ（細スペース）
    total = x + _QUIET

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {total} {_BAR_HEIGHT}" width="{total}" '
        f'height="{_BAR_HEIGHT}" fill="#000">'
        f'<rect x="0" y="0" width="{total}" height="{_BAR_HEIGHT}" fill="#fff"/>'
        f'{"".join(bars)}</svg>'
    )
