"""カスタムフォームウィジェット。"""

from django import forms


class StatusToggleWidget(forms.Widget):
    """有効/無効 を Bootstrap btn-check のセグメンテッドボタンで表示する BooleanField 用ウィジェット。

    チェックボックスより視認性が高く、押下面積も広いため誤操作にも強い。
    """

    template_name = 'a/widgets/status_toggle.html'
    input_type = 'toggle'  # _form_fields.html で checkbox 分岐に入らないための識別子

    def format_value(self, value):
        # value は True/False/None/'True'/'False'/'on' などのバリエーションあり
        if value in (True, 'True', 'true', 'on', '1', 1):
            return 'True'
        return 'False'

    def value_from_datadict(self, data, files, name):
        return data.get(name) == 'True'

    def id_for_label(self, id_):
        # ラベルクリック時は「有効」側ラジオにフォーカス
        return f'{id_}_yes' if id_ else ''
