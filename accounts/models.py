from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models


class User(AbstractUser):
    """WMS ユーザー。AbstractUser の username を user_code として使用する。"""

    username = models.CharField(
        verbose_name='ログインID',
        max_length=20,
        unique=True,
        validators=[
            RegexValidator(
                regex=r'^[A-Za-z0-9\-\.]+$',
                message='英数字・ハイフン・ドットのみ使用可能です',
            ),
        ],
        help_text='例: EMP-00123 / yamada.t',
    )

    display_name = models.CharField(
        verbose_name='表示名',
        max_length=150,
        help_text='例: 山田太郎',
    )

    email = models.EmailField(
        verbose_name='メールアドレス',
        max_length=255,
        unique=True,
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'users'
        verbose_name = 'ユーザー'
        verbose_name_plural = 'ユーザー'

    def __str__(self):
        return f'{self.display_name or self.username} ({self.username})'
