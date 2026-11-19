import time
from django.core.management.base import BaseCommand, CommandError
from bridge.catalog_source import configuration
from bridge.catalog_client import CatalogOdoo
from bridge.services_exchange import exchange_once
from bridge.article_exchange import configured_writer, sweep_once


class Command(BaseCommand):
    help = 'Comunicación Odoo ↔ gateway; adaptador opcional de fichas, deshabilitado por defecto.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--poll-seconds', type=int, default=15)

    def handle(self, *args, **options):
        client = CatalogOdoo(configuration('ODOO_CATALOG_TARGET'))
        writer = configured_writer(client)
        try:
            while True:
                try:
                    count = exchange_once(client, writer)
                    if writer is not None and writer.config.get('read_sweep_enabled',True):
                        sweep_once(client, writer)
                    if count:
                        self.stdout.write(f'Odoo gateway: {count} mensaje(s) confirmado(s); comunicación confirmada.')
                except Exception as exc:
                    if options['once']:
                        raise CommandError(f'Application exchange failed ({type(exc).__name__})') from None
                    self.stderr.write(f'Application exchange deferred ({type(exc).__name__})')
                if options['once']:
                    return
                time.sleep(max(options['poll_seconds'], 5))
        finally:
            client.close()
