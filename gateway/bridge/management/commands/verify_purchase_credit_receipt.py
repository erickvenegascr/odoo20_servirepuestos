import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from pymysql.err import MySQLError

from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import configuration
from bridge.purchase_credit_receipt import PurchaseCreditReceiptSource
from bridge.purchase_exchange import configured_purchase_bridge


class Command(BaseCommand):
    help = 'Verifica en modo solo lectura un recibo SGA con IVA 13 % y crédito en CRC.'

    def add_arguments(self, parser):
        parser.add_argument('request', type=Path, help='Archivo JSON de la solicitud purchase.receive v2')

    def handle(self, *args, **options):
        try:
            payload = json.loads(options['request'].read_text(encoding='utf-8'))
            client = CatalogOdoo(configuration('ODOO_CATALOG_TARGET'))
            try:
                bridge = configured_purchase_bridge(client)
            finally:
                client.close()
            if bridge is None:
                raise CommandError('El gateway de compras debe estar configurado.')
            if not isinstance(payload, dict) or payload.get('application_id') != bridge.application_id:
                raise CommandError('La solicitud pertenece a otra aplicación Odoo.')
            with PurchaseCreditReceiptSource(bridge.source_config) as source:
                result = source.verify(payload)
        except (ValueError, KeyError, OSError, MySQLError) as exc:
            raise CommandError(getattr(exc, 'code', 'SGA_CREDIT_RECEIPT_UNAVAILABLE')) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
