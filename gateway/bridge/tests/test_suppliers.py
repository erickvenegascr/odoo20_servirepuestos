from io import StringIO
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, TestCase
from django.core.management import call_command
from django.core.management.base import CommandError
from bridge.supplier_source import SupplierSource
from bridge.models import SupplierRelationRun


class SupplierSourceTests(SimpleTestCase):
    def test_source_codes_priority_and_bit_values_preserved(self):
        source = SupplierSource({})
        source.rows = MagicMock(return_value=[{'Articulo': 8, 'Proveedor': '019', 'CodigoArticulo': '000008',
            'Prioridad': 0, 'Deshabilitado': b'\x00', 'Activo': b'\x01'}])
        row = source.relations(after=(8, '018'), limit=10)[0]
        self.assertEqual((row['vendor_code'], row['product_code'], row['priority']), ('019', '000008', 0))
        self.assertFalse(row['disabled']); self.assertTrue(row['vendor_active'])
        sql, params = source.rows.call_args.args
        self.assertTrue(sql.startswith('SELECT ')); self.assertNotIn('PrecioCotiz', sql)
        self.assertEqual(params, (8, 8, '018', 10))

    def test_supplier_contract_never_reads_quotes_or_bank_balances(self):
        source = SupplierSource({}); source.rows = MagicMock(return_value=[])
        source.vendors()
        sql = source.rows.call_args.args[0]
        self.assertTrue(sql.startswith('SELECT '))
        for forbidden in ['PrecioCotiz', 'IBAN', 'MontoDeuda', 'TipoCredito']:
            self.assertNotIn(forbidden, sql)


class SupplierCommandTests(TestCase):
    def run_command(self, *, apply=False, fail=False, wrong=False):
        def config(name):
            return {'instance': 'test-sga'} if name == 'SGA_CATALOG_SOURCE' else {
                'url': 'https://odoo.invalid', 'database': 'test-db', 'company_id': 1, 'application_id': 'test-app'}
        with patch('bridge.management.commands.sync_suppliers.configuration', side_effect=config), \
             patch('bridge.management.commands.sync_suppliers.SupplierSource') as source, \
             patch('bridge.management.commands.sync_suppliers.CatalogOdoo') as target:
            source.return_value.__enter__.return_value.vendors.return_value = []
            row = {'article_id': 8, 'vendor_code': '019', 'product_code': '000008', 'priority': 0,
                   'disabled': False, 'vendor_active': True}
            source.return_value.__enter__.return_value.relations.side_effect = [[row], []]
            def call(method, **kwargs):
                if method == 'suppliers_info':
                    return {'suppliers_contract': 1, 'database': 'wrong' if wrong else 'test-db', 'company_id': 1,
                        'application_id': 'test-app', 'source_instance': 'test-sga',
                        'authentication': 'application_token', 'sga_writes_enabled': False}
                rows = kwargs['relations']
                if rows and fail:
                    raise TimeoutError('synthetic lost acknowledgement')
                return {'vendors': {}, 'relations': {'created': len(rows)}, 'count': len(rows),
                    'last_key': [8, '019'] if rows else [0, '']}
            target.return_value.call.side_effect = call
            call_command('sync_suppliers', apply=apply, stdout=StringIO())
            return target.return_value, source

    def test_simulation_by_default_and_persistent_audit(self):
        client, source = self.run_command()
        run = SupplierRelationRun.objects.get()
        self.assertTrue(run.dry_run)
        self.assertEqual((run.status, run.relations, run.last_key), ('succeeded', 1, [8, '019']))
        self.assertEqual(len(run.source_checksum), 64)
        self.assertTrue(all(c.kwargs.get('dry_run', True) for c in client.call.call_args_list))

    def test_failure_does_not_advance_checkpoint(self):
        with self.assertRaises(CommandError):
            self.run_command(apply=True, fail=True)
        run = SupplierRelationRun.objects.get()
        self.assertEqual((run.status, run.relations, run.last_key), ('failed', 0, []))
        self.assertEqual(run.error_code, 'TimeoutError')

    def test_wrong_destination_does_not_read_source(self):
        with self.assertRaises(CommandError):
            self.run_command(wrong=True)
        self.assertEqual(SupplierRelationRun.objects.get().status, 'failed')
