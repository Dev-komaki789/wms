from django.db import migrations


# 旧ステータス値 → 新ステータス値（「次にやる作業」で統一した命名）
FORWARD = {
    'pending': 'receiving_wait',
    'arrived': 'inspection_wait',
    'receiving': 'inspection_wait',  # 旧・入荷中(検品中) は検品作業待ちへ集約
    'putaway': 'putaway_wait',
}
# completed / cancelled は値変更なし
BACKWARD = {
    'receiving_wait': 'pending',
    'inspection_wait': 'arrived',
    'putaway_wait': 'putaway',
}


def _remap(apps, mapping):
    InboundOrder = apps.get_model('inbound', 'InboundOrder')
    for old, new in mapping.items():
        InboundOrder.objects.filter(status=old).update(status=new)


def forward(apps, schema_editor):
    _remap(apps, FORWARD)


def backward(apps, schema_editor):
    _remap(apps, BACKWARD)


class Migration(migrations.Migration):

    dependencies = [
        ('inbound', '0004_alter_inboundorder_status'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
