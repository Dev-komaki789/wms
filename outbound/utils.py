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


# Code128 の値(0-106) → エレメント幅パターン（バー/スペース交互・先頭はバー）。
# 各値は 11 モジュール幅（先頭バーから bar,space,bar,... と交互）。最後の
# 値 106(STOP) のみ 13 モジュール（7 エレメント）。
# Code39 より高密度（同じ桁数でもバーが太くなる＝カメラで解像しやすい）かつ
# チェックデジット内蔵なので、画面表示のバーコードでも誤読が起きにくい。
_CODE128_PATTERNS = [
    '212222',
    '222122',
    '222221',
    '121223',
    '121322',
    '131222',
    '122213',
    '122312',
    '132212',
    '221213',
    '221312',
    '231212',
    '112232',
    '122132',
    '122231',
    '113222',
    '123122',
    '123221',
    '223211',
    '221132',
    '221231',
    '213212',
    '223112',
    '312131',
    '311222',
    '321122',
    '321221',
    '312212',
    '322112',
    '322211',
    '212123',
    '212321',
    '232121',
    '111323',
    '131123',
    '131321',
    '112313',
    '132113',
    '132311',
    '211313',
    '231113',
    '231311',
    '112133',
    '112331',
    '132131',
    '113123',
    '113321',
    '133121',
    '313121',
    '211331',
    '231131',
    '213113',
    '213311',
    '213131',
    '311123',
    '311321',
    '331121',
    '312113',
    '312311',
    '332111',
    '314111',
    '221411',
    '431111',
    '111224',
    '111422',
    '121124',
    '121421',
    '141122',
    '141221',
    '112214',
    '112412',
    '122114',
    '122411',
    '142112',
    '142211',
    '241211',
    '221114',
    '413111',
    '241112',
    '134111',
    '111242',
    '121142',
    '121241',
    '114212',
    '124112',
    '124211',
    '411212',
    '421112',
    '421211',
    '212141',
    '214121',
    '412121',
    '111143',
    '111341',
    '131141',
    '114113',
    '114311',
    '411113',
    '411311',
    '113141',
    '114131',
    '311141',
    '411131',
    '211412',
    '211214',
    '211232',
    '2331112',
]

_START_B = 104  # Start Code B
_START_C = 105  # Start Code C
_CODE_B = 100  # Set B への切替（Set C 中に置く）
_CODE_C = 99  # Set C への切替（Set B 中に置く）
_STOP = 106  # STOP

_MODULE = 2  # 1 モジュールの幅（px）。viewBox 内の解像度
_BAR_HEIGHT = 60  # バーの高さ
_QUIET = 10 * _MODULE  # 両端のクワイエットゾーン（10 モジュール）


def _code128_values(data):
    """文字列を Code128 のシンボル値列（start/checksum/stop 込み）に変換する。

    Set B を基本に、4 桁以上の数字連続は Set C に切り替えて高密度化する。
    Set B が扱えない文字（ASCII 32-126 以外）は無視する。
    """
    data = ''.join(c for c in data if 32 <= ord(c) <= 126)
    n = len(data)

    def digit_run(i):
        j = i
        while j < n and data[j].isdigit():
            j += 1
        return j - i

    codes = []
    # 先頭が十分長い数字なら Start C、そうでなければ Start B
    if n >= 2 and digit_run(0) >= (4 if n > 2 else 2):
        cur = 'C'
        codes.append(_START_C)
    else:
        cur = 'B'
        codes.append(_START_B)

    i = 0
    while i < n:
        if cur == 'C':
            if i + 1 < n and data[i].isdigit() and data[i + 1].isdigit():
                codes.append(int(data[i : i + 2]))
                i += 2
            else:
                codes.append(_CODE_B)
                cur = 'B'
        else:  # Set B
            run = digit_run(i)
            # 4 桁以上、または末尾までの偶数桁なら Set C へ
            if run >= 4 or (run >= 2 and i + run == n and run % 2 == 0):
                codes.append(_CODE_C)
                cur = 'C'
            else:
                codes.append(ord(data[i]) - 32)
                i += 1

    # チェックデジット: (start + Σ 位置×値) % 103
    check = codes[0]
    for pos, val in enumerate(codes[1:], start=1):
        check += pos * val
    codes.append(check % 103)
    codes.append(_STOP)
    return codes


def code128_svg(text):
    """文字列を Code128 バーコードの SVG 文字列に変換する（依存ライブラリなし）。

    ピッキングリスト番号など英数字・ハイフンのコードを想定。戻り値はそのまま
    HTML に埋め込める <svg> 文字列。
    """
    codes = _code128_values(text or '')

    bars = []
    x = _QUIET
    for val in codes:
        pattern = _CODE128_PATTERNS[val]
        for idx, ch in enumerate(pattern):
            width = int(ch) * _MODULE
            if idx % 2 == 0:  # 偶数 index = バー（黒）
                bars.append(f'<rect x="{x}" y="0" width="{width}" height="{_BAR_HEIGHT}"/>')
            x += width
    total = x + _QUIET

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {total} {_BAR_HEIGHT}" width="{total}" '
        f'height="{_BAR_HEIGHT}" fill="#000">'
        f'<rect x="0" y="0" width="{total}" height="{_BAR_HEIGHT}" fill="#fff"/>'
        f'{"".join(bars)}</svg>'
    )
