import hashlib
import json
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import CatalogSource, configuration
from bridge.models import CatalogRun


class Command(BaseCommand):
    help = 'SGA → Odoo: catalog only. Default is simulation; SGA is always read-only.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=100)

    def handle(self, *args, **options):
        size = options['batch_size']
        if not 1 <= size <= 200:
            raise CommandError('Batch size must be between 1 and 200')
        try:
            source_config = configuration('SGA_CATALOG_SOURCE')
            target = configuration('ODOO_CATALOG_TARGET')
            instance = source_config['instance']
            if not isinstance(instance, str) or not 1 <= len(instance) <= 80:
                raise ValueError('Invalid source instance')
        except (OSError, KeyError, ValueError, TypeError):
            raise CommandError('Catalog secret files are missing or invalid') from None
        fingerprint = hashlib.sha256(json.dumps({
            'url': target['url'], 'database': target['database'], 'company': target['company_id'],
            'instance': instance, 'host': source_config['host'], 'database_source': source_config['database'],
        }, sort_keys=True).encode()).hexdigest()
        # A complete reconciliation is deliberate: category links have no reliable
        # change stream on MariaDB 5.5. Every run starts at zero, safely idempotent.
        logical_identity = f"{target['database']}:{target['company_id']}:{instance}"
        lock = int(hashlib.sha256(logical_identity.encode()).hexdigest()[:15], 16)
        locked = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', [lock])
                locked = cursor.fetchone()[0]
            if not locked:
                raise CommandError('Another catalog scan is already running for this destination')
        run = CatalogRun.objects.create(id=uuid.uuid4(), source_instance=instance,
                                        target_fingerprint=fingerprint, dry_run=not options['apply'])
        client = None
        try:
            client = CatalogOdoo(target)
            info = client.call('catalog_info')
            if (info.get('contract') != 1 or info.get('company_id') != int(target['company_id'])
                    or info.get('database') != target['database'] or info.get('sga_writes_enabled') is not False
                    or info.get('source_instance') != instance or info.get('authentication') != 'application_token'
                    or (target.get('application_id') and info.get('application_id') != target['application_id'])):
                raise ValueError('Odoo catalog contract/company mismatch')
            with CatalogSource(source_config) as source:
                nodes = source.categories()
                for offset in range(0, len(nodes), size):
                    result = client.call('apply_catalog_batch', instance=instance,
                                         categories=nodes[offset:offset + size], products=[],
                                         dry_run=run.dry_run)
                    run.categories += result['categories']
                    run.save(update_fields=['categories'])
                while True:
                    rows = source.products(after=run.cursor, limit=size)
                    if not rows:
                        break
                    result = client.call('apply_catalog_batch', instance=instance,
                                         categories=[], products=rows, dry_run=run.dry_run)
                    # Advance only after the complete Odoo transaction was acknowledged.
                    run.cursor = rows[-1]['article_id']
                    run.products += result['products']
                    for key in ('created', 'updated', 'unchanged'):
                        run.result_counts[key] = run.result_counts.get(key, 0) + result.get(key, 0)
                    run.save(update_fields=['cursor', 'products', 'result_counts'])
                    self.stdout.write(f'{run.id}: articles={run.products} cursor={run.cursor}')
            run.status = 'succeeded'
            run.save(update_fields=['status'])
        except Exception as exc:
            run.status = 'failed'
            run.error_code = getattr(exc, 'code', type(exc).__name__)[:80]
            run.save(update_fields=['status', 'error_code'])
            raise CommandError(f'Catalog run {run.id} failed ({run.error_code}); inspect access, '
                               'contract and conflicts. Retry starts a complete idempotent scan.') from None
        finally:
            if client:
                client.close()
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [lock])
        self.stdout.write(self.style.SUCCESS(
            f'{"SIMULATION" if run.dry_run else "APPLIED"} {run.id}: '
            f'categories={run.categories}, articles={run.products}, results={run.result_counts}'))
