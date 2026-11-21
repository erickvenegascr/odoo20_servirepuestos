import json

from django.core.management.base import BaseCommand, CommandError

from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import configuration
from bridge.supplier_pull import reconcile_suppliers


class Command(BaseCommand):
    help = 'Compare the complete SGA supplier snapshot and send only pending changes to Odoo.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--max-relations', type=int, default=3000)
        parser.add_argument('--budget', type=int, default=42)

    def handle(self, *args, **options):
        batch = options['batch_size']
        maximum = options['max_relations']
        budget = options['budget']
        if not 1 <= batch <= 800 or not 1 <= maximum <= 10000 or not 10 <= budget <= 50:
            raise CommandError('Invalid supplier pull delivery limits')
        client = None
        try:
            source = configuration('SGA_CATALOG_SOURCE')
            target = configuration('ODOO_CATALOG_TARGET')
            client = CatalogOdoo(target, timeout=(4, 8))
            summary = reconcile_suppliers(source, target, client, apply=options['apply'],
                                          max_relations=maximum, batch_size=batch, budget=budget)
        except Exception as exc:
            detail = str(exc)
            code = getattr(exc, 'code', detail if detail.startswith('SGA_SUPPLIER_') else type(exc).__name__)
            raise CommandError(f'Supplier pull failed ({code}); pending changes remain available for retry.') from None
        finally:
            if client:
                client.close()
        self.stdout.write(json.dumps(summary, sort_keys=True))
