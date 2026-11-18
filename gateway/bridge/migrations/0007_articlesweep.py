from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('bridge', '0006_supplierrelationrun')]
    operations = [migrations.CreateModel(name='ArticleSweep',fields=[
        ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
        ('application_uuid',models.UUIDField(unique=True)),
        ('source_instance',models.CharField(max_length=80)),
        ('after_article',models.PositiveIntegerField(default=0)),
        ('updated_at',models.DateTimeField(auto_now=True)),
    ])]
