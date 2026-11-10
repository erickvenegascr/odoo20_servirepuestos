import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('bridge', '0003_gatewayinbox')]
    operations = [migrations.CreateModel(name='ProductImageRun', fields=[
        ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
        ('source_instance', models.CharField(max_length=80)),
        ('target_fingerprint', models.CharField(max_length=64)),
        ('dry_run', models.BooleanField(default=True)),
        ('status', models.CharField(max_length=16, default='running')),
        ('files', models.PositiveIntegerField(default=0)),
        ('products', models.PositiveIntegerField(default=0)),
        ('primary', models.PositiveIntegerField(default=0)),
        ('secondary', models.PositiveIntegerField(default=0)),
        ('result_counts', models.JSONField(default=dict)),
        ('last_key', models.CharField(max_length=255, blank=True)),
        ('error_code', models.CharField(max_length=80, blank=True)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
    ])]
