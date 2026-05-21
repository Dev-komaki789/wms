"""ユーザー権限ユーティリティ。

ロール判定は Django 標準の Group を使う。グループ名は HANDHELD_GROUP_NAME。
superuser は制限の対象外（常に全画面アクセス可）。
"""
from django.contrib.auth.models import Group

# ハンディ専用作業者を表す Group 名
HANDHELD_GROUP_NAME = 'handheld_workers'


def is_handheld_worker(user):
    """ユーザーが「ハンディ作業者」かどうか。

    判定:
      - 未認証              -> False
      - superuser           -> False（制限の対象外）
      - 指定 Group のメンバ -> True
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return False
    return user.groups.filter(name=HANDHELD_GROUP_NAME).exists()


def ensure_handheld_group():
    """ハンディ作業者グループが無ければ作成して返す。"""
    group, _ = Group.objects.get_or_create(name=HANDHELD_GROUP_NAME)
    return group
