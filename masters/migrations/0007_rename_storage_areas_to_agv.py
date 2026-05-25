"""既存エリア名の「（常温・標準）」を「（AGV）」へ改称するデータ移行。

シード(reset_and_seed)で作られた通常棚＝AGV 区分のエリア名を、区分名(AGV)に
合わせて改称する。本番など既存DBにも migrate で反映するための一度きりのデータ
移行。新規シードはシードファイル側で既に「（AGV）」になっている。
"""
from django.db import migrations


def rename_to_agv(apps, schema_editor):
    Area = apps.get_model('masters', 'Area')
    for area in Area.objects.filter(area_name__contains='（常温・標準）'):
        area.area_name = area.area_name.replace('（常温・標準）', '（AGV）')
        area.save(update_fields=['area_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('masters', '0006_alter_area_location_type'),
    ]

    operations = [
        # 表示名の一括置換のみ。逆方向は重要でないため no-op。
        migrations.RunPython(rename_to_agv, migrations.RunPython.noop),
    ]
