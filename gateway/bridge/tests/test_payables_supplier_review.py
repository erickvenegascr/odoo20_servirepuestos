import json
from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command

from bridge.payables_source import PayablesSource
from bridge.payables_supplier_review import supplier_advisories
from bridge.supplier_embeddings import EMBEDDING_PROFILE, OPENAI_EMBEDDINGS_URL, OpenAISupplierEmbedder


SOURCE_INSTANCE = 'source-fixture'
ODOO_URL = 'https://odoo.example.test/sga_gateway/api/v1/'
SUPPLIER_CODE = '007'
FIXTURE_NAME = 'Repuestos Ficticios del Norte'
FIXTURE_TAX = '3101000000'
FIXTURE_EMAIL = 'proveedor@example.invalid'
FIXTURE_IBAN = 'CR76000000000000000001'
FIXTURE_KEY = 'key-local-ficticia'
COMMAND = 'bridge.management.commands.sync_payables'


class SupplierAdvisoryTest(TestCase):
    def test_missing_shortlist_is_counted_without_calling_embedder(self):
        client = Mock()
        client.call.return_value = {'suppliers': [{'code': SUPPLIER_CODE, 'known': False, 'candidates': []}]}
        embedder = Mock()
        advisory, statuses = supplier_advisories(client, SOURCE_INSTANCE, [
            {'code': SUPPLIER_CODE, 'name': FIXTURE_NAME, 'tax_id': FIXTURE_TAX,
             'payment_email': FIXTURE_EMAIL, 'iban': FIXTURE_IBAN},
        ], embedder)
        self.assertEqual(advisory, {})
        self.assertEqual(statuses, {'no_candidates': 1})
        embedder.embed.assert_not_called()
        client.call.assert_called_once_with('payables_supplier_options', instance=SOURCE_INSTANCE, suppliers=[
            {'code': SUPPLIER_CODE, 'name': FIXTURE_NAME, 'tax_id': FIXTURE_TAX},
        ])

    def test_rejects_fields_outside_the_odoo_shortlist_schema(self):
        client = Mock()
        client.call.return_value = {'suppliers': [{
            'code': SUPPLIER_CODE, 'known': True,
            'candidates': [{'partner_id': 8, 'name': 'Repuestos Ficticios Norte', 'iban': FIXTURE_IBAN}],
        }]}
        embedder = Mock()
        with self.assertRaisesRegex(ValueError, '^PAYABLES_SUPPLIER_CANDIDATE_INVALID$'):
            supplier_advisories(client, SOURCE_INSTANCE, [
                {'code': SUPPLIER_CODE, 'name': FIXTURE_NAME, 'tax_id': FIXTURE_TAX},
            ], embedder)
        embedder.embed.assert_not_called()

    def test_unlinked_supplier_with_an_unusable_shortlist_still_requires_review(self):
        client = Mock()
        client.call.return_value = {'suppliers': [{
            'code': SUPPLIER_CODE, 'known': False,
            'candidates': [{'partner_id': 8, 'name': 'Proveedor Ficticio 12345'}],
        }]}
        session = Mock()
        embedder = OpenAISupplierEmbedder(api_key=FIXTURE_KEY, session=session)
        advisory, statuses = supplier_advisories(client, SOURCE_INSTANCE, [
            {'code': SUPPLIER_CODE, 'name': FIXTURE_NAME, 'tax_id': FIXTURE_TAX},
        ], embedder)
        self.assertEqual(statuses, {'unavailable': 1})
        self.assertEqual(advisory[SUPPLIER_CODE]['status'], 'unavailable')
        self.assertEqual(advisory[SUPPLIER_CODE]['candidates'], [])
        session.post.assert_not_called()


class SupplierSyncWithEmbeddingsTest(TestCase):
    def setUp(self):
        self.source_config = {'instance': SOURCE_INSTANCE, 'currency_map': {'¢': 'CRC'}, 'national_currency_code': '¢'}
        self.target_config = {
            'url': 'https://odoo.example.test', 'database': 'fixture-db', 'company_id': 1,
            'application_id': 'fixture-app', 'token': 'sgw1_' + 'x' * 40,
        }
        self.source = PayablesSource(self.source_config)
        self.source.rows = Mock(side_effect=self._source_rows)
        self.odoo_requests = []
        self.openai_requests = []
        self.openai_error = None
        self.supplier_known = True
        self.output = StringIO()

        odoo_session = Mock()
        odoo_session.post.side_effect = self._odoo_response
        openai_session = Mock()
        openai_session.post.side_effect = self._openai_response
        self._patch('bridge.catalog_client.requests.Session', side_effect=[odoo_session, openai_session])
        self._patch(COMMAND + '.configuration', side_effect=[self.source_config, self.target_config])
        self._patch(COMMAND + '.PayablesSource', return_value=nullcontext(self.source))
        self._patch(COMMAND + '.connection', new=Mock(vendor='sqlite'))

    def _patch(self, target, **options):
        patcher = patch(target, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _source_rows(self, sql, params):
        self.assertIn('FROM proveedores', sql)
        self.assertNotIn('cuentaspagar', sql.lower())
        if len(params) != 1:
            return []
        return [{
            'Proveedor': SUPPLIER_CODE, 'ProveedorNomb': FIXTURE_NAME, 'Cedula': FIXTURE_TAX,
            'Activo': True, 'EMailEnvio': FIXTURE_EMAIL, 'Banco': '0001', 'IBAN': FIXTURE_IBAN,
        }]

    def _odoo_response(self, endpoint, **options):
        self.assertTrue(endpoint.startswith(ODOO_URL))
        route = endpoint.removeprefix(ODOO_URL)
        body = options['json']
        self.odoo_requests.append((route, body))
        if route == 'info':
            result = {'contract': 1, 'database': self.target_config['database'], 'company_id': 1,
                      'application_id': self.target_config['application_id'], 'source_instance': SOURCE_INSTANCE,
                      'authentication': 'application_token'}
        elif route == 'payables/supplier-options':
            result = {'suppliers': [{
                'code': SUPPLIER_CODE, 'known': self.supplier_known,
                'candidates': [{'partner_id': 8, 'name': 'Repuestos Ficticios Norte'}],
            }]}
        elif route == 'payables/snapshot':
            result = {'documents': len(body['documents']), 'suppliers': len(body['suppliers']),
                      'quarantined': 0, 'dry_run': body['dry_run']}
        else:
            self.fail(f'Unexpected supplier-only route: {route}')
        response = Mock(status_code=200)
        response.json.return_value = result
        return response

    def _openai_response(self, endpoint, **options):
        self.assertEqual(endpoint, OPENAI_EMBEDDINGS_URL)
        body = options['json']
        self.openai_requests.append(body)
        if self.openai_error:
            raise self.openai_error
        response = Mock(status_code=200)
        response.json.return_value = {'model': body['model'], 'data': [
            {'index': index, 'embedding': [1.0] + [0.0] * (body['dimensions'] - 1)}
            for index, _name in enumerate(body['input'])
        ]}
        return response

    def _run(self, *args):
        with TemporaryDirectory() as folder:
            key_file = Path(folder) / 'test-api-key'
            key_file.write_text(FIXTURE_KEY, encoding='utf-8')
            with patch.dict('os.environ', {'SGA_SUPPLIER_OPENAI_API_KEY_FILE': str(key_file)}):
                call_command('sync_payables', '--suppliers-only', '--supplier-embeddings', *args, stdout=self.output)
        self.assertEqual([route for route, _body in self.odoo_requests], [
            'info', 'payables/supplier-options', 'payables/snapshot',
        ])
        self.assertEqual(self.source.rows.call_count, 2)
        self.assertEqual(self.source.rows.call_args.args[1][0], SUPPLIER_CODE)
        self.assertEqual(len(self.openai_requests), 1)
        return self.odoo_requests[-1][1]

    def test_exports_only_names_to_openai_and_never_reads_invoices(self):
        snapshot = self._run()
        request, = self.openai_requests
        self.assertEqual(request['input'], ['repuestos ficticios del norte', 'repuestos ficticios norte'])
        outbound_json = json.dumps(request)
        for value in (FIXTURE_TAX, FIXTURE_EMAIL, FIXTURE_IBAN, FIXTURE_KEY, SUPPLIER_CODE, SOURCE_INSTANCE):
            self.assertNotIn(value, outbound_json)
        self.assertEqual(snapshot['documents'], [])
        self.assertTrue(snapshot['dry_run'])
        advisory = snapshot['supplier_advisories'][SUPPLIER_CODE]
        self.assertEqual(advisory['status'], 'ranked')
        result, = advisory['candidates']
        self.assertEqual((result['partner_id'], result['score']), (8, 1.0))
        self.assertRegex(result['input_hash'], r'^[0-9a-f]{64}$')
        self.assertIn('OPENAI ranked=1 unavailable=0', self.output.getvalue())

    def test_openai_failure_keeps_exact_supplier_deliverable_with_no_advisory(self):
        self.openai_error = requests.Timeout(f'{FIXTURE_EMAIL} {FIXTURE_IBAN} {FIXTURE_KEY}')
        snapshot = self._run('--apply')
        self.assertEqual((snapshot['dry_run'], snapshot['documents'], snapshot['supplier_advisories']), (False, [], {}))
        supplier, = snapshot['suppliers']
        self.assertEqual((supplier['code'], supplier['tax_id'], supplier['payment_email'], supplier['iban']),
                         (SUPPLIER_CODE, FIXTURE_TAX, FIXTURE_EMAIL, FIXTURE_IBAN))
        output = self.output.getvalue()
        self.assertIn('APPLIED suppliers=1 documents=0', output)
        self.assertIn('OPENAI ranked=0 unavailable=1', output)
        for value in (FIXTURE_TAX, FIXTURE_EMAIL, FIXTURE_IBAN, FIXTURE_KEY):
            self.assertNotIn(value, output)

    def test_openai_failure_marks_unlinked_supplier_with_candidates_for_review(self):
        self.supplier_known = False
        self.openai_error = requests.Timeout('fallo ficticio')
        snapshot = self._run('--apply')
        review = snapshot['supplier_advisories'][SUPPLIER_CODE]
        self.assertEqual(set(review), {'status', 'profile', 'model', 'source_hash', 'candidates'})
        self.assertEqual((review['status'], review['profile'], review['model'], review['candidates']),
                         ('unavailable', EMBEDDING_PROFILE, 'text-embedding-3-small', []))
        self.assertRegex(review['source_hash'], r'^[0-9a-f]{64}$')
        self.assertIn('OPENAI ranked=0 unavailable=1', self.output.getvalue())
