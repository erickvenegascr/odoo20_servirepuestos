from unittest.mock import MagicMock
import uuid
from django.test import TestCase
from bridge.services_exchange import exchange_once
from bridge.models import GatewayInbox


class ExchangeTests(TestCase):
    def envelope(self, kind='ping'):
        return {'application_id': str(uuid.uuid4()), 'messages': [{
            'message_id': str(uuid.uuid4()), 'lease': str(uuid.uuid4()), 'kind': kind, 'payload': {}}]}

    def test_ping_round_trip_and_durable_retry(self):
        envelope = self.envelope()
        client = MagicMock()
        client.call.side_effect = [envelope, {'status': 'acknowledged'}, envelope, {'status': 'acknowledged'}]
        self.assertEqual(exchange_once(client), 1)
        self.assertEqual(exchange_once(client), 1)
        self.assertEqual(GatewayInbox.objects.count(), 1)
        self.assertEqual(GatewayInbox.objects.get().result, {'status': 'pong', 'sga_mode': 'read_only'})
        self.assertTrue(GatewayInbox.objects.get().acknowledged_at)

    def test_inverse_requests_never_write_sga(self):
        client = MagicMock()
        client.call.side_effect = [self.envelope('request'), {}]
        exchange_once(client)
        self.assertEqual(GatewayInbox.objects.get().result['code'], 'SGA_READ_ONLY')

    def test_lost_ack_keeps_result_for_reissued_lease(self):
        envelope = self.envelope()
        client = MagicMock()
        client.call.side_effect = [envelope, TimeoutError('lost ack')]
        with self.assertRaises(TimeoutError):
            exchange_once(client)
        self.assertFalse(GatewayInbox.objects.get().acknowledged_at)
        envelope['messages'][0]['lease'] = str(uuid.uuid4())
        client.call.side_effect = [envelope, {}]
        exchange_once(client)
        self.assertEqual(GatewayInbox.objects.count(), 1)

    def test_changed_content_under_same_identity_rejected(self):
        envelope = self.envelope()
        client = MagicMock()
        client.call.side_effect = [envelope, {}]
        exchange_once(client)
        envelope['messages'][0]['payload'] = {'changed': True}
        client.call.side_effect = [envelope]
        with self.assertRaises(ValueError):
            exchange_once(client)
