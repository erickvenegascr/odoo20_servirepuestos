import base64
import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import configuration
from bridge.image_source import ProductImageSource
from bridge.models import ProductImageRun


class Command(BaseCommand):
    help = 'SGA image directory → Odoo main photo and gallery. Metadata simulation by default; source is read-only.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--source-directory', default='/media/datos/shares/sgadatos/Ver13/Documentos/Artículos/jpg')

    def handle(self, *args, **options):
        target = configuration('ODOO_CATALOG_TARGET')
        instance = configuration('SGA_CATALOG_SOURCE')['instance']
        fingerprint = hashlib.sha256(json.dumps({
            'url': target['url'], 'database': target['database'], 'company': target['company_id'],
            'instance': instance, 'application_id': target['application_id'],
        }, sort_keys=True).encode()).hexdigest()
        lock = int(hashlib.sha256(f'media:{fingerprint}'.encode()).hexdigest()[:15], 16)
        locked = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', [lock])
                locked = cursor.fetchone()[0]
                if not locked:
                    raise CommandError('An image scan is already running for this destination')
        run = ProductImageRun.objects.create(source_instance=instance, target_fingerprint=fingerprint,
                                            dry_run=not options['apply'])
        client = None
        try:
            client = CatalogOdoo(target)
            info = client.call('catalog_info')
            if (info.get('contract') != 1 or info.get('media_contract') != 1
                    or info.get('database') != target['database'] or info.get('company_id') != target['company_id']
                    or info.get('application_id') != target['application_id'] or info.get('source_instance') != instance
                    or info.get('authentication') != 'application_token' or info.get('sga_writes_enabled') is not False):
                raise ValueError('Odoo media contract or scope mismatch')
            source = ProductImageSource(options['source_directory'])
            rows = source.snapshot()
            run.products = len({r['article_id'] for r in rows})
            run.save(update_fields=['products'])
            for offset in range(0, len(rows), 50):
                batch = []
                for row in rows[offset:offset + 50]:
                    run.last_key = row['key']
                    item, raw = source.read(row)
                    batch.append((row, item, raw))
                states = client.call('check_images', instance=instance, items=[item for row, item, raw in batch])['items']
                if len(states) != len(batch) or any(state['key'] != item['key'] for state, (_, item, _) in zip(states, batch)):
                    raise ValueError('Media acknowledgement identity mismatch')
                for state, (row, item, raw) in zip(states, batch):
                    run.last_key = item['key']
                    if state['status'] == 'unchanged':
                        status = 'unchanged'
                    elif state['status'] != 'upload':
                        raise ValueError('Media missing product or local image conflict')
                    elif run.dry_run:
                        status = 'would_upload'
                    else:
                        result = client.call('apply_image', instance=instance, item=item,
                                             content=base64.b64encode(raw).decode('ascii'))
                        if result.get('key') != item['key'] or result.get('status') not in ('created', 'updated', 'unchanged'):
                            raise ValueError('Media upload acknowledgement mismatch')
                        status = result['status']
                    run.files += 1
                    run.primary += int(item['primary'])
                    run.secondary += int(not item['primary'])
                    run.result_counts[status] = run.result_counts.get(status, 0) + 1
                    run.save(update_fields=['files', 'primary', 'secondary', 'result_counts', 'last_key'])
                self.stdout.write(f'{run.id}: images={run.files} primary={run.primary} secondary={run.secondary}')
            run.status = 'succeeded'
            run.save(update_fields=['status'])
        except Exception as exc:
            run.status = 'failed';run.error_code = getattr(exc, 'code', type(exc).__name__)[:80]
            run.save(update_fields=['status', 'error_code', 'last_key'])
            raise CommandError(f'Image run {run.id} failed ({run.error_code}); retry is a complete idempotent scan.') from None
        finally:
            if client:
                client.close()
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [lock])
        self.stdout.write(f'{"SIMULATION" if run.dry_run else "APPLIED"} {run.id}: images={run.files}, '
                          f'products={run.products}, primary={run.primary}, secondary={run.secondary}, results={run.result_counts}')
