from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('bridge', '0002_catalogrun')]
    operations = [migrations.AddField(model_name='catalogrun', name='result_counts', field=models.JSONField(default=dict)), migrations.CreateModel(name='GatewayInbox', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('application_uuid', models.UUIDField()), ('message_uuid', models.UUIDField()),
        ('request_checksum', models.CharField(max_length=64)), ('result', models.JSONField()),
        ('received_at', models.DateTimeField(auto_now_add=True)),
        ('acknowledged_at', models.DateTimeField(blank=True, null=True)),
    ], options={'constraints': [models.UniqueConstraint(fields=('application_uuid', 'message_uuid'), name='unique_gateway_inbox_message')]})]
