import copy
import datetime
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.management import call_command, CommandError
from django.test import TestCase, SimpleTestCase
from bridge.catalog_details_source import CatalogDetailsSource, commercial_unit, price_row, vat
from bridge.models import CatalogDetailRun


class PricingContractTests(SimpleTestCase):
    def test_brand_reference_uses_mariadb_canonical_master_code(self):
        source = CatalogDetailsSource({})
        source.rows = Mock(side_effect=[[
            {'Articulo': 13, 'CodHacienda': '', 'Unidad': 'UNIDAD', 'UnidadDet': 'UNIDAD',
             'NumeroUnidades': 1, 'CostoUnit': 20, 'CostoUnitDet': 20, 'TipoIVA': '08', 'IVA': .13,
             'Exento': b'\x00', 'Division': '001', 'Categoria': '1', 'SubCategoria': None,
             'Marca': 'yam', 'CatalogMarca': 'YAM'}], []])
        row = source.details({'codes': {}})[0]
        self.assertEqual(row['brand'], '["brand","YAM"]')
        self.assertEqual(row['snapshot']['Marca'], 'yam')

    def test_fractional_vat_and_inconsistent_source_code(self):
        self.assertEqual(vat(.13, '08', False), (13, '08', False))
        self.assertEqual(vat(13, '08', False), (13, '08', False))
        self.assertEqual(vat(0, '08', True), (0, '01', True))
        self.assertEqual(vat(.01, '02', False), (1, '02', False))
        with self.assertRaises(ValueError):
            vat(.15, '08', False)

    def test_markup_is_percentage_and_detail_variation_is_preserved(self):
        row = {'Nivel': 1, 'PorcGanancia': .470654, 'VariacionDet': .00000123,
               'PrecioSugeridoDet': 166.19, 'ConImptoDet': b'\x01', 'CalculoPrecio': 0}
        value = price_row(row, 13, 'formula')
        self.assertEqual(value['mode'], 'markup')
        self.assertAlmostEqual(value['markup'], 47.065523, places=8)
        self.assertAlmostEqual(value['fixed_price'], 166.19 / 1.13)

    def test_maintained_price_and_user_fixed_strategy_remove_included_tax(self):
        row = {'Nivel': 1, 'PrecioSugeridoDet': 1130, 'ConImptoDet': b'\x01', 'CalculoPrecio': 1}
        self.assertEqual(price_row(row, 13, 'formula')['mode'], 'fixed')
        self.assertAlmostEqual(price_row(row, 13, 'formula')['fixed_price'], 1000)
        row['CalculoPrecio'] = 0
        self.assertEqual(price_row(row, 13, 'fixed')['mode'], 'fixed')

    def test_units_and_unsupported_live_promotion(self):
        self.assertEqual(commercial_unit('CENTIM'), 'cm')
        self.assertEqual(commercial_unit('Unidad'), 'Unid')
        with self.assertRaises(ValueError):
            commercial_unit('unknown measurement')
        with self.assertRaises(ValueError):
            price_row({'Nivel': 1, 'PorcDesctoPromo': .1, 'FechaFinPromo': datetime.datetime(2099, 1, 1)}, 13, 'formula')


class DetailsCommandTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.target = {'url': 'https://synthetic.example', 'database': 'synthetic', 'company_id': 1, 'application_id': 'synthetic-app', 'token': 'sgw1_' + 't' * 64}
        source = root / 'source.json';source.write_text(json.dumps({'instance': 'synthetic-source'}))
        target = root / 'target.json';target.write_text(json.dumps(self.target))
        self.official = root / 'official.json';self.official.write_text(json.dumps({'source': 'https://api.hacienda.go.cr/fe/cabys', 'codes': {'1234567890123': {'status': 'found'}}}))
        env = patch.dict(os.environ, SGA_CATALOG_SOURCE_FILE=str(source), ODOO_CATALOG_TARGET_FILE=str(target))
        env.start();self.addCleanup(env.stop)
        self.info = {'details_contract': 1, 'database': 'synthetic', 'company_id': 1, 'application_id': 'synthetic-app',
                     'source_instance': 'synthetic-source', 'authentication': 'application_token', 'sga_writes_enabled': False}

    def reader(self, mock):
        source = mock.return_value.__enter__.return_value
        source.rows.return_value = [{'cabys': '1234567890123'}]
        source.masters.return_value = []
        source.details.side_effect = [[{'article_id': 8, 'prices': [{'level': 1}]}], []]
        return source

    def test_scope_is_verified_before_source_is_opened(self):
        with patch('bridge.management.commands.sync_catalog_details.CatalogOdoo') as client, patch('bridge.management.commands.sync_catalog_details.CatalogDetailsSource') as source:
            client.return_value.call.return_value = dict(self.info, company_id=99)
            with self.assertRaises(CommandError):
                call_command('sync_catalog_details', cabys_file=str(self.official))
            source.assert_not_called()
        self.assertEqual(CatalogDetailRun.objects.get().status, 'failed')

    def test_acknowledgement_updates_durable_cursor_after_success(self):
        with patch('bridge.management.commands.sync_catalog_details.CatalogOdoo') as client, patch('bridge.management.commands.sync_catalog_details.CatalogDetailsSource') as source:
            self.reader(source)
            client.return_value.call.side_effect = [self.info, {'masters': {'created': 2}},
                {'last_article': 8, 'products': {'created': 1}, 'prices': {'created': 1}, 'issues': {'missing_cabys': 0}}]
            call_command('sync_catalog_details', cabys_file=str(self.official), apply=True)
        run = CatalogDetailRun.objects.get()
        self.assertEqual((run.status, run.products, run.prices, run.cursor), ('succeeded', 1, 1, 8))
        self.assertEqual(run.price_result_counts, {'created': 1})

    def test_uncovered_cabys_is_rejected_before_target_masters_are_changed(self):
        self.official.write_text(json.dumps({'source': 'https://api.hacienda.go.cr/fe/cabys', 'codes': {}}))
        with patch('bridge.management.commands.sync_catalog_details.CatalogOdoo') as client, patch('bridge.management.commands.sync_catalog_details.CatalogDetailsSource') as source:
            self.reader(source);client.return_value.call.return_value = self.info
            with self.assertRaises(CommandError):
                call_command('sync_catalog_details', cabys_file=str(self.official), apply=True)
            self.assertEqual(client.return_value.call.call_count, 1)
        self.assertEqual(CatalogDetailRun.objects.get().cursor, 0)

    def test_lost_or_mismatched_acknowledgement_does_not_advance_cursor(self):
        with patch('bridge.management.commands.sync_catalog_details.CatalogOdoo') as client, patch('bridge.management.commands.sync_catalog_details.CatalogDetailsSource') as source:
            self.reader(source)
            client.return_value.call.side_effect = [self.info, {'masters': {}},
                {'last_article': 99, 'products': {'created': 1}, 'prices': {'created': 1}, 'issues': {}}]
            with self.assertRaises(CommandError):
                call_command('sync_catalog_details', cabys_file=str(self.official), apply=True)
        run = CatalogDetailRun.objects.get()
        self.assertEqual((run.status, run.cursor, run.products), ('failed', 0, 0))
