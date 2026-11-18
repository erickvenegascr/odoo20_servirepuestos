import hashlib
import json
from collections import Counter
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import configuration
from bridge.supplier_source import SupplierSource
from bridge.models import SupplierRelationRun


class Command(BaseCommand):
    help = 'Import supplier links, textual codes and unchanged SGA priorities; no quotes or SGA writes.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=1000)

    def handle(self, *args, **options):
        if not 1 <= options['batch_size'] <= 1000:
            raise CommandError('Batch size must be 1..1000')
        target = configuration('ODOO_CATALOG_TARGET'); source_config = configuration('SGA_CATALOG_SOURCE')
        instance = source_config['instance']
        fingerprint = hashlib.sha256(json.dumps({k: target[k] for k in ['url', 'database', 'company_id', 'application_id']}, sort_keys=True).encode() + instance.encode()).hexdigest()
        lock = int(hashlib.sha256(('suppliers:' + fingerprint).encode()).hexdigest()[:15], 16); locked = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', [lock]); locked = cursor.fetchone()[0]
                if not locked:
                    raise CommandError('Supplier import already running for this destination')
        run = SupplierRelationRun.objects.create(source_instance=instance, target_fingerprint=fingerprint,
                                                 dry_run=not options['apply'])
        client = None
        try:
            client = CatalogOdoo(target); info = client.call('suppliers_info')
            if (info.get('suppliers_contract') != 1 or info.get('database') != target['database']
                    or info.get('company_id') != target['company_id'] or info.get('application_id') != target['application_id']
                    or info.get('source_instance') != instance or info.get('authentication') != 'application_token'
                    or info.get('sga_writes_enabled') is not False):
                raise ValueError('Supplier contract or destination mismatch')
            counts = Counter(); checksum = hashlib.sha256(); after = (0, '')
            with SupplierSource(source_config) as source:
                vendors = source.vendors()
                seed = client.call('apply_suppliers_batch', instance=instance, vendors=vendors, relations=[], dry_run=run.dry_run)
                run.vendor_counts = seed['vendors']; run.save(update_fields=['vendor_counts'])
                checksum.update(json.dumps(vendors, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
                while True:
                    rows = source.relations(after=after, limit=options['batch_size'])
                    if not rows:
                        break
                    result = client.call('apply_suppliers_batch', instance=instance,
                        vendors=vendors if run.dry_run else [], relations=rows, dry_run=run.dry_run)
                    last_key = [rows[-1]['article_id'], rows[-1]['vendor_code']]
                    if result.get('last_key') != last_key or result.get('count') != len(rows):
                        raise ValueError('Supplier acknowledgement mismatch')
                    if sum(result['relations'].get(k, 0) for k in ['created', 'updated', 'adopted', 'unchanged']) != len(rows):
                        raise ValueError('Supplier result count mismatch')
                    for row in rows:
                        checksum.update(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
                    counts.update(result['relations']); after = tuple(last_key)
                    run.relations += len(rows); run.last_key = last_key; run.result_counts = dict(counts)
                    run.source_checksum = checksum.hexdigest()
                    run.save(update_fields=['relations', 'last_key', 'result_counts', 'source_checksum'])
                    self.stdout.write(f'{run.id}: relations={run.relations}, last_key={run.last_key}', ending='\n')
            run.status = 'succeeded'; run.save(update_fields=['status'])
        except Exception as exc:
            run.status = 'failed'; run.error_code = getattr(exc, 'code', type(exc).__name__)[:80]
            run.save(update_fields=['status', 'error_code'])
            raise CommandError(f'Supplier run {run.id} failed ({run.error_code}); retry is a complete idempotent scan.') from None
        finally:
            if client:
                client.close()
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [lock])
        self.stdout.write(f'{"SIMULATION" if run.dry_run else "APPLIED"} {run.id}: relations={run.relations}, '
                          f'results={run.result_counts}, vendors={run.vendor_counts}, source_checksum={run.source_checksum}')
