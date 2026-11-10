import hashlib
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestApplicationApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = 'sgw1_' + 'a' * 64
        cls.other_token = 'sgw1_' + 'b' * 64
        cls.app = cls.env['sga.gateway.application'].create({
            'name': 'Synthetic SGA app', 'source_instance': 'synthetic-http-sga',
            'token_hash': hashlib.sha256(cls.token.encode()).hexdigest()})
        cls.other = cls.env['sga.gateway.application'].create({
            'name': 'Synthetic other app', 'source_instance': 'synthetic-http-other',
            'token_hash': hashlib.sha256(cls.other_token.encode()).hexdigest()})
        cls.nodes = [{'key': 'root', 'name': 'SGA HTTP', 'parent': None}]
        cls.row = {'article_id': 89001, 'name': 'SYNTHETIC HTTP PRODUCT', 'default_code': 'SYNTH-HTTP',
                   'service': False, 'active': True, 'units_per_pack': 1, 'category_key': 'root'}

    def post(self, path, body=None, token=None, raw=None):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (self.token if token is None else token)}
        return self.url_open('/sga_gateway/api/v1/' + path,
                             data=raw if raw is not None else json.dumps(body or {}).encode(),
                             headers=headers)

    def test_token_info_has_application_scope(self):
        response = self.post('info')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['application_id'], self.app.application_uuid)
        self.assertEqual(response.json()['authentication'], 'application_token')
        self.assertFalse(response.json()['sga_writes_enabled'])
        self.assertTrue(self.app.last_seen_at)

    def test_concurrent_heartbeat_retries_entire_catalog_batch(self):
        Application = type(self.app)
        original = Application._touch
        attempts = []

        def touch_once_conflicted(app):
            attempts.append(True)
            if len(attempts) == 1:
                app.env.cr.execute("""DO $$ BEGIN RAISE EXCEPTION
                    'synthetic heartbeat conflict' USING ERRCODE='40001'; END $$""")
            return original(app)

        body = {'instance': self.app.source_instance, 'categories': self.nodes,
                'products': [self.row], 'dry_run': False}
        with patch.object(Application, '_touch', touch_once_conflicted):
            response = self.post('catalog/batch', body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(response.json()['created'], 1)
        self.assertEqual(self.env['product.template'].with_context(active_test=False).search_count(
            [('sga_catalog_instance', '=', self.app.source_instance)]), 1)

    def test_heartbeat_is_throttled(self):
        self.assertEqual(self.post('info').status_code, 200)
        timestamp = self.app.last_seen_at
        with patch('odoo.addons.sga_gateway_connector.models.application.fields.Datetime.now',
                   return_value=timestamp + timedelta(seconds=5)):
            self.assertEqual(self.post('info').status_code, 200)
        self.assertEqual(self.app.last_seen_at, timestamp)

    def test_missing_invalid_and_native_api_keys_rejected(self):
        for token in ['', 'personal-api-key', 'sgw1_' + 'z' * 64]:
            self.assertEqual(self.post('info', token=token).status_code, 401)

    def test_revocation_and_rotation_are_immediate(self):
        self.app.action_revoke()
        self.assertEqual(self.post('info').status_code, 401)
        rotated = self.app.action_generate_token()['params']['message']
        self.assertEqual(self.post('info').status_code, 401)
        self.assertEqual(self.post('info', token=rotated).status_code, 200)
        self.assertNotEqual(self.app.token_hash, rotated)
        self.assertEqual(self.app.token_hash, hashlib.sha256(rotated.encode()).hexdigest())

    def test_scopes_and_company_override_rejected(self):
        self.app.catalog_enabled = False
        self.assertEqual(self.post('catalog/batch', {'instance': self.app.source_instance}).status_code, 403)
        self.app.catalog_enabled = True
        for body in [{'instance': 'another-source'},
                     {'instance': self.app.source_instance, 'context': {'allowed_company_ids': [999]}}]:
            self.assertEqual(self.post('catalog/batch', body).status_code, 403)
        self.app.exchange_enabled = False
        self.assertEqual(self.post('exchange/poll').status_code, 403)

    def test_catalog_dry_run_and_idempotent_real_import(self):
        body = {'instance': self.app.source_instance, 'categories': self.nodes, 'products': [self.row]}
        self.assertEqual(self.post('catalog/batch', body).status_code, 200)
        Product = self.env['product.template'].with_context(active_test=False)
        domain = [('sga_catalog_instance', '=', self.app.source_instance)]
        self.assertFalse(Product.search_count(domain))
        body['dry_run'] = False
        self.assertEqual(self.post('catalog/batch', body).json()['created'], 1)
        self.assertEqual(self.post('catalog/batch', body).json()['unchanged'], 1)
        self.assertEqual(Product.search_count(domain), 1)

    def test_invalid_item_rolls_back_all_rows_over_http(self):
        body = {'instance': self.app.source_instance, 'categories': self.nodes,
                'products': [self.row, dict(self.row, article_id=-1)], 'dry_run': False}
        self.assertEqual(self.post('catalog/batch', body).status_code, 409)
        self.assertFalse(self.env['product.template'].search_count([('sga_catalog_instance', '=', self.app.source_instance)]))
        self.assertFalse(self.env['product.category'].search_count([('sga_catalog_instance', '=', self.app.source_instance)]))

    def test_mixed_existing_and_new_articles_in_one_batch(self):
        body = {'instance': self.app.source_instance, 'categories': self.nodes,
                'products': [self.row], 'dry_run': False}
        self.assertEqual(self.post('catalog/batch', body).json()['created'], 1)
        body['products'] = [dict(self.row, name='SYNTHETIC UPDATED'),
                            dict(self.row, article_id=89002, active=False),
                            dict(self.row, article_id=89003)]
        result = self.post('catalog/batch', body).json()
        self.assertEqual((result['created'], result['updated']), (2, 1))
        self.assertEqual(self.post('catalog/batch', body).json()['unchanged'], 3)
        records = self.env['product.template'].with_context(active_test=False).search(
            [('sga_catalog_instance', '=', self.app.source_instance)])
        self.assertEqual(len(records), 3)
        self.assertEqual(len(records.filtered('active')), 2)
        self.assertEqual(len(records.mapped('categ_id')), 1)

    def test_invalid_oversized_and_nonfinite_json_rejected(self):
        for raw in [b'[]', b'{broken', b'{"x":NaN}']:
            self.assertEqual(self.post('info', raw=raw).status_code, 400)
        self.assertEqual(self.post('info', raw=b'{"x":"' + b'x' * 262144 + b'"}').status_code, 413)

    def test_inverse_delivery_ack_and_repeat(self):
        self.app.action_ping()
        messages = self.post('exchange/poll').json()['messages']
        self.assertEqual(len(messages), 1)
        self.assertFalse(self.post('exchange/poll').json()['messages'])
        message = messages[0]
        ack = {'message_id': message['message_id'], 'lease': message['lease'],
               'result': {'status': 'pong', 'sga_mode': 'read_only'}}
        self.assertEqual(self.post('exchange/ack', ack, token=self.other_token).status_code, 404)
        self.assertEqual(self.post('exchange/ack', dict(ack, lease=str(uuid.uuid4()))).status_code, 409)
        self.assertEqual(self.post('exchange/ack', ack).status_code, 200)
        self.assertEqual(self.post('exchange/ack', ack).status_code, 200)
        self.assertEqual(self.post('exchange/ack', dict(ack, result={'status': 'different'})).status_code, 409)
        self.assertEqual(self.app.message_ids.state, 'acknowledged')

    def test_expired_lease_reissued_and_old_ack_rejected(self):
        self.app.action_ping()
        first = self.post('exchange/poll').json()['messages'][0]
        self.env.cr.execute('UPDATE sga_gateway_message SET lease_until=%s WHERE application_id=%s',
                            [fields.Datetime.now() - timedelta(minutes=1), self.app.id])
        second = self.post('exchange/poll').json()['messages'][0]
        self.assertEqual(first['message_id'], second['message_id'])
        self.assertNotEqual(first['lease'], second['lease'])
        self.assertEqual(self.post('exchange/ack', {'message_id': first['message_id'], 'lease': first['lease'], 'result': {}}).status_code, 409)

    def test_empty_ack_result_is_idempotent(self):
        self.app.action_ping()
        message = self.post('exchange/poll').json()['messages'][0]
        body = {'message_id': message['message_id'], 'lease': message['lease'], 'result': {}}
        self.assertEqual(self.post('exchange/ack', body).status_code, 200)
        self.assertEqual(self.post('exchange/ack', body).status_code, 200)

    def test_public_user_cannot_manage_tokens_or_create_messages(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessError):
            self.app.with_user(public).action_generate_token()
        with self.assertRaises(AccessError):
            self.env['sga.gateway.message'].with_user(public).create({'application_id': self.app.id})
