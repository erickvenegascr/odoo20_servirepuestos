import copy
import hashlib
import json
import uuid
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from bridge.models import Operation, OperationEvent
from bridge.services import process_next

KEYS = {'odoo': 'test-odoo-key', 'other': 'test-other-key', 'operator': 'test-operator-key'}
CLIENTS = [
    {'client_id': 'odoo', 'origin_instance_id': 'odoo-test', 'token_sha256': hashlib.sha256(KEYS['odoo'].encode()).hexdigest(), 'roles': ['submit', 'read'], 'companies': ['company-a', 'company-b']},
    {'client_id': 'other', 'origin_instance_id': 'other-test', 'token_sha256': hashlib.sha256(KEYS['other'].encode()).hexdigest(), 'roles': ['submit', 'read'], 'companies': ['company-a']},
    {'client_id': 'operator', 'origin_instance_id': 'operator-test', 'token_sha256': hashlib.sha256(KEYS['operator'].encode()).hexdigest(), 'roles': ['read', 'read_any', 'retry'], 'companies': ['company-a']},
]

def envelope():
    return {'schema_version': '1', 'operation_id': str(uuid.uuid4()), 'event_id': str(uuid.uuid4()),
            'aggregate_id': str(uuid.uuid4()), 'origin_instance_id': 'odoo-test',
            'target_company_key': 'company-a', 'revision': 1, 'action': 'receipt.apply',
            'origin_system': 'odoo', 'actor_user_id': '42', 'occurred_at_utc': '2026-10-27T12:00:00Z',
            'expected_target_fingerprint': 'ABSENT', 'payload': {'lines': [{'product': 'test-product', 'quantity': '4'}]}}

def client(name='odoo'):
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION='Bearer ' + KEYS[name])
    return api

@override_settings(GATEWAY_CLIENTS=CLIENTS)
class APITests(TestCase):
    def setUp(self):
        self.api, self.body = client(), envelope()

    def submit(self, body=None):
        return self.api.post('/api/v1/operations/', body or self.body, format='json')

    def test_authentication_is_required(self):
        self.assertEqual(APIClient().get('/api/v1/operations/').status_code, 401)
        invalid = APIClient(); invalid.credentials(HTTP_AUTHORIZATION='Bearer wrong')
        self.assertEqual(invalid.get('/api/v1/operations/').status_code, 401)

    def test_health_and_readiness_are_public_without_details(self):
        self.assertEqual(APIClient().get('/health/live/').json(), {'status': 'ok'})
        self.assertEqual(APIClient().get('/health/ready/').json(), {'status': 'ready'})

    def test_submission_is_durable_and_creates_audit_event(self):
        res = self.submit()
        self.assertEqual(res.status_code, 201, res.data)
        op = Operation.objects.get(pk=res.data['id'])
        self.assertEqual(op.payload, self.body['payload'])
        self.assertEqual(op.events.count(), 1)
        self.assertEqual(op.state, 'EN_COLA')
        self.assertNotIn('payload', res.data)

    def test_retry_of_same_business_content_returns_original(self):
        first = self.submit()
        second_body = copy.deepcopy(self.body)
        second_body['event_id'] = str(uuid.uuid4())
        second_body['occurred_at_utc'] = '2026-10-27T12:01:00Z'
        second = self.submit(second_body)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data['id'], second.data['id'])
        self.assertEqual(Operation.objects.count(), 1)
        self.assertEqual(OperationEvent.objects.count(), 1)

    def test_changed_payload_same_identity_conflicts(self):
        self.submit()
        self.body['payload']['lines'][0]['quantity'] = '5'
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(Operation.objects.get().payload['lines'][0]['quantity'], '4')

    def test_changed_company_or_aggregate_cannot_reuse_identity(self):
        self.submit(); self.body['target_company_key'] = 'company-b'
        self.assertEqual(self.submit().status_code, 409)
        self.body['target_company_key'] = 'company-a'; self.body['aggregate_id'] = str(uuid.uuid4())
        self.assertEqual(self.submit().status_code, 409)

    def test_new_revision_requires_a_new_submission_event(self):
        self.submit(); self.body['revision'] = 2
        self.assertEqual(self.submit().status_code, 409)
        self.body['event_id'] = str(uuid.uuid4())
        self.assertEqual(self.submit().status_code, 201)

    def test_reused_event_with_other_operation_conflicts(self):
        self.submit(); self.body['operation_id'] = str(uuid.uuid4())
        self.assertEqual(self.submit().status_code, 409)

    def test_wrong_payload_digest_is_rejected(self):
        self.body['payload_sha256'] = '0' * 64
        self.assertEqual(self.submit().status_code, 400)
        self.assertFalse(Operation.objects.exists())

    def test_company_and_origin_cannot_be_spoofed(self):
        self.body['target_company_key'] = 'forbidden-company'
        self.assertEqual(self.submit().status_code, 403)
        self.body['target_company_key'] = 'company-a'; self.body['origin_instance_id'] = 'other-test'
        self.assertEqual(self.submit().status_code, 403)

    def test_client_cannot_read_another_clients_operation(self):
        res = self.submit(); other = client('other')
        self.assertEqual(other.get('/api/v1/operations/' + res.data['id'] + '/').status_code, 404)
        self.assertEqual(other.get('/api/v1/operations/').data['results'], [])
        self.assertEqual(client('operator').get('/api/v1/operations/' + res.data['id'] + '/').status_code, 200)

    def test_operator_cannot_escape_company_scope(self):
        self.body['target_company_key'] = 'company-b'; res = self.submit()
        self.assertEqual(client('operator').get('/api/v1/operations/' + res.data['id'] + '/').status_code, 404)

    def test_operator_cannot_submit(self):
        self.assertEqual(client('operator').post('/api/v1/operations/', self.body, format='json').status_code, 403)

    def test_unknown_actions_and_state_injection_are_rejected(self):
        self.body['action'] = 'sql.execute'
        self.assertEqual(self.submit().status_code, 400)
        self.body['action'] = 'receipt.apply'; self.body['state'] = 'SINCRONIZADO'
        self.assertEqual(self.submit().status_code, 400)

    def test_payload_type_and_revision_are_validated(self):
        self.body['payload'] = []
        self.assertEqual(self.submit().status_code, 400)
        self.body['payload'] = {}; self.body['revision'] = 0
        self.assertEqual(self.submit().status_code, 400)

    def test_naive_dates_are_rejected(self):
        self.body['occurred_at_utc'] = '2026-10-27T12:00:00'
        self.assertEqual(self.submit().status_code, 400)

    def test_request_size_is_bounded(self):
        self.body['payload']['padding'] = 'x' * 270000
        self.assertEqual(self.submit().status_code, 413)
        self.assertFalse(Operation.objects.exists())

    def test_nonfinite_json_is_rejected(self):
        data = json.dumps(self.body).replace('"quantity": "4"', '"quantity": NaN')
        self.assertEqual(self.api.post('/api/v1/operations/', data, content_type='application/json').status_code, 400)

    def test_worker_records_block_instead_of_faking_sga_effects(self):
        res = self.submit()
        self.assertTrue(process_next())
        self.assertFalse(process_next())
        op = Operation.objects.get(pk=res.data['id'])
        self.assertEqual(op.state, 'BLOQUEADO')
        self.assertEqual(op.last_error_code, 'SGA_NATIVE_ADAPTER_NOT_CERTIFIED')
        self.assertEqual(op.native_keys, {})
        self.assertEqual(op.attempts, 1)
        self.assertEqual(list(op.events.values_list('state', flat=True)), ['EN_COLA', 'VALIDANDO', 'BLOQUEADO'])

    def test_retry_requires_role_and_only_safe_states(self):
        res = self.submit(); url = '/api/v1/operations/' + res.data['id'] + '/retry/'
        self.assertEqual(self.api.post(url, {}, format='json').status_code, 403)
        process_next()
        self.assertEqual(client('operator').post(url, {}, format='json').status_code, 200)
        self.assertEqual(Operation.objects.get().state, 'EN_COLA')
        Operation.objects.update(state='RESULTADO_DESCONOCIDO')
        self.assertEqual(client('operator').post(url, {}, format='json').status_code, 409)

    def test_events_are_scoped_and_support_incremental_read(self):
        res = self.submit(); process_next()
        url = '/api/v1/operations/' + res.data['id'] + '/events/'
        events = self.api.get(url).data
        self.assertEqual(len(events['results']), 3)
        self.assertEqual(self.api.get(url, {'after': events['next_after']}).data['results'], [])
        self.assertEqual(client('other').get(url).status_code, 404)

    def test_list_is_bounded_and_invalid_parameters_rejected(self):
        self.submit()
        self.assertEqual(len(self.api.get('/api/v1/operations/', {'limit': 1}).data['results']), 1)
        self.assertEqual(self.api.get('/api/v1/operations/', {'limit': 'invalid'}).status_code, 400)
