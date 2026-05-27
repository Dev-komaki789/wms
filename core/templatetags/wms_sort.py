"""一覧・照会テーブルの「見出しクリックで並び替え」用テンプレートタグ。

ビュー側は core.utils.apply_ordering で並べ替え＋採用 sort_key/dir を
コンテキスト（慣例として sort / dir）に渡す。テンプレートは各見出しを
{% sort_th '見出し' 'key' sort dir %} で置き換える。クリックすると現在の
検索条件を保持したまま sort/dir を切り替える（昇順⇄降順、page はリセット）。
"""

from django import template

register = template.Library()


@register.inclusion_tag('a/_sort_th.html', takes_context=True)
def sort_th(context, label, key, current_sort, current_dir, align='start', width=''):
    request = context['request']
    params = request.GET.copy()
    is_active = str(current_sort) == str(key)
    # 同じ列を再クリックで昇順⇄降順をトグル。別列は昇順から。
    next_dir = 'desc' if (is_active and current_dir == 'asc') else 'asc'
    params['sort'] = key
    params['dir'] = next_dir
    params.pop('page', None)  # 並べ替えたら1ページ目へ
    return {
        'label': label,
        'href': '?' + params.urlencode(),
        'is_active': is_active,
        'current_dir': current_dir,
        'align': align,
        'width': width,
    }
