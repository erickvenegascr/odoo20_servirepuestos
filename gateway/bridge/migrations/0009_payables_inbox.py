from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('bridge', '0008_supplier_pull_checkpoint')]

    operations = [
        migrations.CreateModel(
            name='PayablesInbox',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('application_id', models.UUIDField()),
                ('operation_id', models.UUIDField()),
                ('source_instance', models.CharField(max_length=80)),
                ('payload', models.JSONField()),
                ('payload_hash', models.CharField(max_length=64)),
                ('lease', models.UUIDField()),
                ('result', models.JSONField(null=True)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('acknowledged_at', models.DateTimeField(null=True)),
            ],
            options={
                'constraints': [models.UniqueConstraint(
                    fields=('application_id', 'operation_id'), name='payables_inbox_operation',
                )],
            },
        ),
    ]
