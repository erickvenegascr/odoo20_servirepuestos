import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import configuration
from bridge.catalog_details_source import CatalogDetailsSource
from bridge.models import CatalogDetailRun


class Command(BaseCommand):
    help = 'Import new categorization, fiscal product fields and price levels from SELECT-only SGA.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--cabys-file', default=os.environ.get('SGA_CABYS_CATALOG_FILE', ''))
        parser.add_argument('--pricing', choices=['formula', 'fixed'], default='formula')

    def handle(self, *args, **options):
        raw = Path(options['cabys_file']).read_bytes()
        official = json.loads(raw)
        if official.get('source') != 'https://api.hacienda.go.cr/fe/cabys' or not isinstance(official.get('codes'), dict):
            raise CommandError('An official CABYS validation cache is required')
        target = configuration('ODOO_CATALOG_TARGET')
        source_config = configuration('SGA_CATALOG_SOURCE');instance = source_config['instance']
        fingerprint = hashlib.sha256(json.dumps({k: target[k] for k in ['url', 'database', 'company_id', 'application_id']}, sort_keys=True).encode() + instance.encode()).hexdigest()
        lock = int(hashlib.sha256(('details:' + fingerprint).encode()).hexdigest()[:15], 16)
        locked = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', [lock]);locked = cursor.fetchone()[0]
                if not locked:
                    raise CommandError('A catalog details import is already running for this destination')
        run = CatalogDetailRun.objects.create(source_instance=instance, target_fingerprint=fingerprint,
            official_catalog_checksum=hashlib.sha256(raw).hexdigest(), pricing_strategy=options['pricing'], dry_run=not options['apply'])
        client = None
        try:
            client = CatalogOdoo(target);info = client.call('details_info')
            if (info.get('details_contract') != 1 or info.get('database') != target['database']
                    or info.get('company_id') != target['company_id'] or info.get('application_id') != target['application_id']
                    or info.get('source_instance') != instance or info.get('authentication') != 'application_token'
                    or info.get('sga_writes_enabled') is not False):
                raise ValueError('Odoo catalog details contract or destination mismatch')
            totals = Counter();price_totals = Counter();issues = Counter()
            with CatalogDetailsSource(source_config) as source:
                required = {str(r['cabys']).strip() for r in source.rows(
                    "SELECT DISTINCT CodHacienda cabys FROM artsservs WHERE Articulo>0 AND TRIM(IFNULL(CodHacienda,''))<>''")}
                if not required <= set(official['codes']):
                    raise ValueError('Official CABYS cache does not cover the complete current SGA catalogue')
                masters = source.masters()
                seed = client.call('apply_details_batch', instance=instance, masters=masters, products=[], dry_run=run.dry_run)
                run.master_counts = seed['masters'];run.save(update_fields=['master_counts'])
                while True:
                    rows = source.details(official, options['pricing'], after=run.cursor, limit=100)
                    if not rows:
                        break
                    value = client.call('apply_details_batch', instance=instance, masters=masters if run.dry_run else [],
                                        products=rows, dry_run=run.dry_run)
                    if value.get('last_article') != rows[-1]['article_id'] or sum(value['products'].values()) != len(rows):
                        raise ValueError('Catalog details acknowledgement mismatch')
                    prices = sum(len(r['prices']) for r in rows)
                    if sum(value['prices'].values()) != prices:
                        raise ValueError('Price acknowledgement mismatch')
                    totals.update(value['products']);price_totals.update(value['prices']);issues.update(value['issues'])
                    run.products += len(rows);run.prices += prices;run.cursor = value['last_article']
                    run.result_counts = dict(totals);run.price_result_counts = dict(price_totals);run.issue_counts = dict(issues)
                    run.save(update_fields=['products', 'prices', 'cursor', 'result_counts', 'price_result_counts', 'issue_counts'])
                    self.stdout.write(f'{run.id}: products={run.products} prices={run.prices} cursor={run.cursor}')
            run.status = 'succeeded';run.save(update_fields=['status'])
        except Exception as exc:
            run.status = 'failed';run.error_code = getattr(exc, 'code', type(exc).__name__)[:80]
            run.save(update_fields=['status', 'error_code'])
            raise CommandError(f'Catalog details run {run.id} failed ({run.error_code}); retry is a complete idempotent scan.') from None
        finally:
            if client:
                client.close()
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [lock])
        self.stdout.write(f'{"SIMULATION" if run.dry_run else "APPLIED"} {run.id}: products={run.products}, '
                          f'prices={run.prices}, results={run.result_counts}, price_results={run.price_result_counts}, issues={run.issue_counts}')
