import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('bridge', '0001_initial')]
    operations = [migrations.CreateModel(name='CatalogRun', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('source_instance', models.CharField(max_length=80)),
        ('target_fingerprint', models.CharField(max_length=64)),
        ('dry_run', models.BooleanField(default=True)),
        ('status', models.CharField(default='running', max_length=16)),
        ('cursor', models.PositiveBigIntegerField(default=0)),
        ('categories', models.PositiveIntegerField(default=0)),
        ('products', models.PositiveIntegerField(default=0)),
        ('error_code', models.CharField(blank=True, max_length=80)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
    ])]
