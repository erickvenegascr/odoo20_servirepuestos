import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from bridge.odoo_json2 import OdooJson2Client, OdooJson2Error


class OdooJson2ClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.key_file = Path(directory.name) / 'fake-odoo.key'
        self.key = 'test-json2-key-example-001'
        self.key_file.write_text(self.key + '\n', encoding='utf-8')
        self.key_file.chmod(0o600)
        self.config = {
            'url': 'https://odoo.example.test/',
            'database': 'test-db',
            'company_id': 17,
            'api_key_file': self.key_file,
        }
        self.session = Mock(spec=requests.Session())
        self.response = self.session.post.return_value
        self.response.status_code = 200

    def client(self, **overrides):
        client = OdooJson2Client({**self.config, **overrides}, session=self.session)
        self.addCleanup(client.close)
        return client

    def test_search_read_uses_named_arguments_and_one_fixed_company(self):
        rows = [{'id': 12, 'currency_id': 2}]
        self.response.json.return_value = rows
        result = self.client().search_read(
            'account.move', [['move_type', '=', 'in_invoice']], ['id', 'currency_id'],
            limit=100, order='id', offset=200,
        )
        self.assertEqual(result, rows)
        self.assertFalse(self.session.trust_env)
        self.session.post.assert_called_once_with(
            'https://odoo.example.test/json/2/account.move/search_read',
            headers={'Authorization': 'Bearer ' + self.key, 'X-Odoo-Database': 'test-db'},
            json={
                'domain': [['move_type', '=', 'in_invoice']], 'fields': ['id', 'currency_id'],
                'load': None, 'limit': 100, 'order': 'id', 'offset': 200,
                'context': {'allowed_company_ids': [17]},
            },
            timeout=(5, 60), allow_redirects=False, verify=True,
        )

    def test_read_returns_standard_rows_and_uses_ids(self):
        self.response.json.return_value = [{'id': 12, 'amount_residual': 25.5}]
        self.assertEqual(
            self.client().read('account.move', [12], ('id', 'amount_residual')),
            [{'id': 12, 'amount_residual': 25.5}],
        )
        self.assertTrue(self.session.post.call_args.args[0].endswith('/account.move/read'))
        self.assertEqual(self.session.post.call_args.kwargs['json'], {
            'ids': [12], 'fields': ['id', 'amount_residual'], 'load': None,
            'context': {'allowed_company_ids': [17]},
        })

    def test_generic_call_preserves_arguments_and_supplied_non_company_context(self):
        self.response.json.return_value = True
        context = {'lang': 'es_CR', 'allowed_company_ids': [17]}
        self.assertIs(
            self.client().call('account.move', 'action_post', ids=[12], context=context), True,
        )
        self.assertEqual(self.session.post.call_args.kwargs['json'], {
            'ids': [12], 'context': context,
        })

    def test_refuses_origin_paths_and_credentials_in_url(self):
        for url in (
            'http://odoo.example.test',
            'https://user:secret@odoo.example.test',
            'https://odoo.example.test/other',
            'https://odoo.example.test?next=https://other.example.test',
            'https://odoo.example.test#other',
            'https://odoo.example.test\\other',
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                OdooJson2Error, '^ODOO_JSON2_HTTPS_ORIGIN_REQUIRED$',
            ):
                self.client(url=url)
        self.session.post.assert_not_called()

    def test_rejects_bad_certificate_and_path_or_context_injection(self):
        client = self.client()
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_INVALID_PATH$'):
            client.call('account.move/../../other', 'write')
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_INVALID_PATH$'):
            client.call('account.move', '_private')
        for context in (
            {'allowed_company_ids': [17, 18]}, {'default_company_id': 18}, {'force_company': 18},
        ):
            with self.subTest(context=context), self.assertRaisesRegex(
                OdooJson2Error, '^ODOO_JSON2_CROSS_COMPANY_CONTEXT$',
            ):
                client.call('account.move', 'read', context=context)
        self.session.post.assert_not_called()
        self.session.post.side_effect = requests.exceptions.SSLError('synthetic cert detail')
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_TRANSPORT_FAILED$'):
            client.call('account.move', 'read', ids=[12])
        self.assertTrue(self.session.post.call_args.kwargs['verify'])

    def test_api_key_requires_protected_file_and_native_key(self):
        self.key_file.chmod(0o644)
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_KEY_FILE_UNPROTECTED$'):
            self.client()
        self.key_file.chmod(0o640)
        self.key_file.write_text('sgw1_synthetic_gateway_token\n', encoding='utf-8')
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_API_KEY_REQUIRED$'):
            self.client()
        self.key_file.write_text('test-json2-foo\nheader: test', encoding='utf-8')
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_API_KEY_REQUIRED$'):
            self.client()
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_KEY_FILE_UNREADABLE$'):
            self.client(api_key_file=self.key_file.with_name('missing.key'))
        self.session.post.assert_not_called()

    def test_never_follows_cross_domain_redirect(self):
        self.response.status_code = 307
        self.response.headers = {'Location': 'https://other.example.test/receive'}
        with self.assertRaises(OdooJson2Error) as caught:
            self.client().call('account.move', 'action_post', ids=[12])
        self.assertEqual(caught.exception.status, 307)
        self.assertFalse(caught.exception.outcome_unknown)
        self.session.post.assert_called_once()
        self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])
        self.assertTrue(self.session.post.call_args.args[0].startswith('https://odoo.example.test/'))
        self.response.json.assert_not_called()

    def test_http_failure_omits_response_details_and_does_not_retry(self):
        self.response.status_code = 403
        self.response.text = 'synthetic error with private fields'
        client = self.client()
        with self.assertRaises(OdooJson2Error) as caught:
            client.call('account.move', 'action_post', ids=[12])
        self.assertEqual(str(caught.exception), 'ODOO_JSON2_HTTP_403')
        self.assertFalse(caught.exception.outcome_unknown)
        self.response.status_code = 503
        with self.assertRaises(OdooJson2Error) as caught:
            client.call('account.move', 'action_post', ids=[12])
        self.assertEqual(str(caught.exception), 'ODOO_JSON2_HTTP_503')
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(self.session.post.call_count, 2)
        self.response.json.assert_not_called()

    def test_timeout_marks_effect_unknown_and_does_not_retry(self):
        self.session.post.side_effect = requests.Timeout('synthetic transport detail')
        with self.assertRaises(OdooJson2Error) as caught:
            self.client().call('account.move', 'action_post', ids=[12])
        self.assertEqual(str(caught.exception), 'ODOO_JSON2_TRANSPORT_FAILED')
        self.assertTrue(caught.exception.outcome_unknown)
        self.session.post.assert_called_once()

    def test_malformed_success_marks_effect_unknown(self):
        self.response.json.side_effect = ValueError('synthetic invalid document detail')
        with self.assertRaises(OdooJson2Error) as caught:
            self.client().call('account.move', 'action_post', ids=[12])
        self.assertEqual(str(caught.exception), 'ODOO_JSON2_INVALID_RESPONSE')
        self.assertEqual(caught.exception.status, 200)
        self.assertTrue(caught.exception.outcome_unknown)
        self.session.post.assert_called_once()

    def test_read_requires_fields_and_record_response(self):
        client = self.client()
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_FIELDS_REQUIRED$'):
            client.read('account.move', [12], [])
        self.session.post.assert_not_called()
        self.response.json.return_value = {'error': 'synthetic response detail'}
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_INVALID_RECORDS$'):
            client.read('account.move', [12], ['id'])

    def test_close_prevents_reuse(self):
        client = self.client()
        client.close()
        self.session.close.assert_called_once()
        with self.assertRaisesRegex(OdooJson2Error, '^ODOO_JSON2_CLIENT_CLOSED$'):
            client.read('account.move', [12], ['id'])
        self.session.post.assert_not_called()
