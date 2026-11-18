import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('bridge', '0005_catalogdetailrun')]
    operations = [migrations.CreateModel(name='SupplierRelationRun', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('source_instance', models.CharField(max_length=80)),
        ('target_fingerprint', models.CharField(max_length=64)),
        ('dry_run', models.BooleanField(default=True)),
        ('status', models.CharField(max_length=16, default='running')),
        ('relations', models.PositiveIntegerField(default=0)),
        ('last_key', models.JSONField(default=list)),
        ('source_checksum', models.CharField(max_length=64, blank=True)),
        ('result_counts', models.JSONField(default=dict)),
        ('vendor_counts', models.JSONField(default=dict)),
        ('error_code', models.CharField(max_length=80, blank=True)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
    ])]
