import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase, SimpleTestCase

from bridge.models import GatewayInbox
from bridge.purchase_exchange import PurchaseBridge
from bridge.purchase_effects_contract import NativeResultUnknown, NativeResultUnverified, digest
from bridge.tests.test_purchase_effects_contract import receive_request_v2
from bridge.tests.test_purchase_receipt_source import scope as receipt_scope
from bridge.services_exchange import exchange_once


def request(action='purchase.search'):
    return {'contract': 1, 'action': action, 'operation_id': str(uuid.uuid4()),
            'application_id': str(uuid.uuid4()), 'source_instance': 'lab',
            'scope': {'vendor_code': '047', 'date_from': None, 'date_to': None,
                      'order_id': None, 'include_closed': False}, 'limit': 80}


def receipt_request(payload):
    return {**{key: value for key, value in payload.items() if key not in ('scope', 'limit')},
            'action': 'purchase.receipt.lookup', 'scope': receipt_scope()}


class PurchaseBridgeTests(SimpleTestCase):
    def setUp(self):
        self.payload = request()
        self.factory = MagicMock()
        self.source = self.factory.return_value.__enter__.return_value
        self.source.search.return_value = {'contract': 1, 'source_instance': 'lab',
            'status': 'not_found', 'orders': [], 'has_more': False, 'search_complete': True}
        self.bridge = PurchaseBridge(self.payload['application_id'], {'instance': 'lab'},
                                     source_factory=self.factory)

    def test_absence_requires_successful_select_and_keeps_operation_scope(self):
        result = self.bridge.apply(self.payload)
        self.assertEqual('not_found', result['status'])
        self.assertEqual(self.payload['operation_id'], result['operation_id'])
        self.assertEqual(self.payload['application_id'], result['application_id'])
        self.source.search.assert_called_once_with(**self.payload['scope'], limit=80, after=0)

    def test_connection_failure_never_becomes_not_found(self):
        self.factory.return_value.__enter__.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.bridge.apply(self.payload)

    def test_other_source_or_application_never_opens_connection(self):
        for key in ('source_instance', 'application_id'):
            bad = dict(self.payload, **{key: str(uuid.uuid4())})
            with self.assertRaises(ValueError):
                self.bridge.apply(bad)
        self.factory.assert_not_called()

    def test_malformed_scope_is_rejected_before_business_read(self):
        self.payload['scope']['sql'] = 'SELECT secret'
        self.assertEqual('rejected', self.bridge.apply(self.payload)['status'])
        self.factory.assert_not_called()

    def test_wrong_source_result_is_not_accepted(self):
        self.source.search.return_value['source_instance'] = 'other'
        with self.assertRaises(ValueError):
            self.bridge.apply(self.payload)

    def test_native_order_writer_is_explicitly_blocked(self):
        self.payload['action'] = 'purchase.upsert'
        self.assertEqual('BLOCKED_NATIVE_PURCHASE_NOT_CERTIFIED', self.bridge.apply(self.payload)['code'])
        self.factory.assert_not_called()

    def standalone_request(self):
        common = {key: value for key, value in self.payload.items() if key not in ('scope', 'limit')}
        common['action'] = 'purchase.upsert'
        order = {'order_uuid': common['operation_id'], 'odoo_order_id': 31, 'odoo_order_ref': 'P00031',
                 'vendor_code': '047', 'currency': 'CRC', 'date_order': '2026-11-19', 'delivery_days': 2,
                 'lines': [{'article_id': 5, 'source_line_id': 13, 'por_mayor': False,
                            'unit_factor': 1, 'quantity': 3, 'unit_cost': 70.25}]}
        return {**common, 'order': order, 'request_checksum': digest(order)}

    def test_only_valid_standalone_order_reaches_its_own_writer(self):
        payload = self.standalone_request()
        writer = MagicMock(application_id=payload['application_id'], source_instance='lab')
        bridge = PurchaseBridge(payload['application_id'], {'instance': 'lab'}, order_writer=writer)
        proof = {'order_id': 66, 'vendor_code': '047', 'currency_code': 'CRC', 'currency_status': 'resolved',
                 'date_order': '2026-11-19T00:00:00', 'date_approval': None, 'delivery_days': 2,
                 'printed': False, 'processed': False, 'reorder': False, 'withdraw': False, 'eligible': True,
                 'lines': [{'line_key': [66, False, 5], 'article_id': 5, 'por_mayor': False, 'unit_factor': 12,
                            'ordered_base': 3, 'received_base': 0, 'quoted_cost': 70.25,
                            'saldada': False, 'consignment': False}]}
        proof['checksum'] = digest(proof)
        result = {**payload, 'status': 'synchronized', 'order_confirmed': True,
                  'order_uuid': payload['operation_id'], 'order': proof}
        writer.apply.return_value = result
        self.assertEqual(bridge.apply(payload), result)
        writer.apply.assert_called_once_with(payload)
        legacy = {**payload, 'order': {'invoice_revision': 'fiscal-only'}}
        self.assertEqual(bridge.apply(legacy)['status'], 'blocked')
        corrupt = {**payload, 'request_checksum': 'c' * 64}
        self.assertEqual(bridge.apply(corrupt)['status'], 'rejected')
        writer.apply.assert_called_once()

    def test_valid_order_remains_blocked_without_a_writer(self):
        self.assertEqual(self.bridge.apply(self.standalone_request())['status'], 'blocked')

    def test_native_success_without_order_readback_is_retryable(self):
        payload = self.standalone_request()
        writer = MagicMock(application_id=payload['application_id'], source_instance='lab')
        writer.apply.return_value = {**payload, 'status': 'synchronized', 'order_confirmed': True,
                                     'order_uuid': payload['operation_id'], 'order': {}}
        bridge = PurchaseBridge(payload['application_id'], {'instance': 'lab'}, order_writer=writer)
        with self.assertRaises(NativeResultUnverified):
            bridge.apply(payload)

    def test_fiscal_v2_uses_effects_boundary_without_reading_source(self):
        payload = receive_request_v2()
        payload.update(application_id=self.payload['application_id'], source_instance='lab')
        result = self.bridge.apply(payload)
        self.assertEqual('blocked', result['status'])
        self.assertEqual(2, result['contract'])
        self.assertEqual('BLOCKED_NATIVE_PURCHASE_NOT_CERTIFIED', result['code'])
        self.factory.assert_not_called()

    def test_read_contract_cannot_be_upgraded_implicitly(self):
        self.payload['contract'] = 2
        with self.assertRaises(ValueError):
            self.bridge.apply(self.payload)
        self.factory.assert_not_called()

    def test_large_page_is_cut_only_between_complete_orders(self):
        self.source.search.return_value.update(status='found',
            orders=[{'order_id': n, 'comment': 'x' * 100000} for n in (5, 4, 3)],
            has_more=False, search_complete=True)
        result = self.bridge.apply(self.payload)
        self.assertEqual([5, 4], [row['order_id'] for row in result['orders']])
        self.assertTrue(result['has_more'])
        self.assertFalse(result['search_complete'])
        self.assertEqual(4, result['cursor_after'])

    def test_single_oversized_order_is_blocked_without_claiming_absence(self):
        self.source.search.return_value.update(status='found',
            orders=[{'order_id': 5, 'comment': 'x' * 250000}])
        result = self.bridge.apply(self.payload)
        self.assertEqual('blocked', result['status'])
        self.assertNotIn('orders', result)

    def test_replenishment_uses_a_separate_read_contract(self):
        source = MagicMock()
        factory = MagicMock()
        factory.return_value.__enter__.return_value = source
        source.suggest.return_value = {'source_instance': 'lab', 'status': 'found',
            'candidates': [{'article_id': 7, 'ranking_native': 1}]}
        bridge = PurchaseBridge(self.payload['application_id'], {'instance': 'lab'},
                                replenishment_factory=factory)
        payload = dict(self.payload, action='purchase.replenish', scope={
            'vendor_code': None, 'strategy': 'bajo_minimo', 'abc': 'A', 'retained': False})
        result = bridge.apply(payload)
        self.assertEqual('purchase.replenish', result['action'])
        self.assertEqual(7, result['candidates'][0]['article_id'])
        self.factory.assert_not_called()

    def test_receipt_scope_is_validated_before_opening_source(self):
        factory = MagicMock()
        bridge = PurchaseBridge(self.payload['application_id'], {'instance': 'lab'},
                                receipt_factory=factory)
        request_data = receipt_request(self.payload)
        for bad_scope in (receipt_scope(order_id=True), receipt_scope(warehouse=True),
                          receipt_scope(invoice_number=' 01234'), receipt_scope(invoice_number='0\n1'),
                          receipt_scope(invoice_number='1' * 21), receipt_scope(sql='SELECT 1')):
            result = bridge.apply({**request_data, 'scope': bad_scope})
            self.assertEqual('rejected', result['status'])
        with self.assertRaises(ValueError):
            bridge.apply({**request_data, 'source_instance': 'other'})
        factory.assert_not_called()

    def test_receipt_missing_and_database_outage_are_distinct(self):
        factory = MagicMock()
        source = factory.return_value.__enter__.return_value
        source.lookup.return_value = {'source_instance': 'lab', 'scope': receipt_scope(),
                                      'status': 'not_found', 'receipts': []}
        bridge = PurchaseBridge(self.payload['application_id'], {'instance': 'lab'},
                                receipt_factory=factory)
        request_data = receipt_request(self.payload)
        result = bridge.apply(request_data)
        self.assertEqual(('not_found', [], receipt_scope()),
                         (result['status'], result['receipts'], result['scope']))
        source.lookup.assert_called_once_with(**receipt_scope())
        factory.return_value.__enter__.side_effect = TimeoutError('synthetic outage')
        result = bridge.apply(request_data)
        self.assertEqual(('unavailable', 'SGA_RECEIPT_SOURCE_UNAVAILABLE', receipt_scope()),
                         (result['status'], result['code'], result['scope']))
        self.assertNotIn('receipts', result)
        self.factory.assert_not_called()

    @patch('bridge.catalog_source.pymysql.connect')
    def test_native_receipt_lookup_rejects_a_database_account_with_write_grants(self, connect):
        connection = connect.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'grant': 'GRANT SELECT, UPDATE ON `fixture`.* TO `lab`@`localhost`'}]
        bridge = PurchaseBridge(self.payload['application_id'],
                                {'instance': 'lab', 'host': 'localhost', 'user': 'lab', 'password': 'synthetic'})
        result = bridge.apply(receipt_request(self.payload))
        self.assertEqual(('unavailable', 'SGA_SELECT_ONLY_ACCOUNT_REQUIRED'), (result['status'], result['code']))
        connection.close.assert_called_once()
        cursor.execute.assert_called_once_with('SHOW GRANTS FOR CURRENT_USER()')

    def test_receipt_lookup_never_delivers_an_oversized_partial_manifest(self):
        factory = MagicMock()
        source = factory.return_value.__enter__.return_value
        source.lookup.return_value = {'source_instance': 'lab', 'scope': receipt_scope(),
                                      'status': 'found', 'receipts': [{'oversize': 'a' * 241000}]}
        bridge = PurchaseBridge(self.payload['application_id'], {'instance': 'lab'},
                                receipt_factory=factory)
        result = bridge.apply(receipt_request(self.payload))
        self.assertEqual(('conflict', 'SGA_RECEIPT_LOOKUP_TOO_BROAD'), (result['status'], result['code']))
        self.assertNotIn('receipts', result)


class PurchaseExchangeTests(TestCase):
    def setUp(self):
        self.payload = request()
        self.message = {'message_id': self.payload['operation_id'], 'kind': 'request',
                        'lease': str(uuid.uuid4()), 'payload': self.payload}
        self.envelope = {'application_id': self.payload['application_id'], 'messages': [self.message]}
        self.client = MagicMock(config={'application_id': self.payload['application_id']})
        self.bridge = MagicMock(application_id=self.payload['application_id'])
        self.bridge.apply.return_value = {'status': 'found', 'orders': [{'order_id': 1}]}

    def test_disabled_bridge_leaves_original_pending(self):
        self.client.call.side_effect = [self.envelope]
        self.assertEqual(0, exchange_once(self.client))
        self.assertFalse(GatewayInbox.objects.exists())

    def test_lost_ack_reuses_the_exact_search_snapshot(self):
        self.client.call.side_effect = [self.envelope, TimeoutError()]
        with self.assertRaises(TimeoutError):
            exchange_once(self.client, purchase_bridge=self.bridge)
        self.message['lease'] = str(uuid.uuid4())
        self.client.call.side_effect = [self.envelope, {}]
        exchange_once(self.client, purchase_bridge=self.bridge)
        self.bridge.apply.assert_called_once_with(self.payload)
        self.assertTrue(GatewayInbox.objects.get().acknowledged_at)

    def test_receipt_lookup_recovers_after_a_read_outage_then_replays_same_evidence(self):
        self.message['payload'] = receipt_request(self.payload)
        unavailable = {'status': 'unavailable', 'code': 'SGA_RECEIPT_SOURCE_UNAVAILABLE'}
        found = {'status': 'found', 'receipts': [{'receipt_id': 75}, {'receipt_id': 79}]}
        self.bridge.apply.side_effect = [unavailable, found]
        self.client.call.side_effect = [self.envelope, {}, self.envelope, TimeoutError(), self.envelope, {}]
        self.assertEqual(1, exchange_once(self.client, purchase_bridge=self.bridge))
        self.assertFalse(GatewayInbox.objects.exists())
        with self.assertRaises(TimeoutError):
            exchange_once(self.client, purchase_bridge=self.bridge)
        self.message['lease'] = str(uuid.uuid4())
        self.assertEqual(1, exchange_once(self.client, purchase_bridge=self.bridge))
        self.assertEqual(2, self.bridge.apply.call_count)
        self.assertEqual(found, GatewayInbox.objects.get().result)
        self.assertIsNotNone(GatewayInbox.objects.get().acknowledged_at)

    def test_block_is_shown_without_poisoning_native_retry(self):
        self.bridge.apply.return_value = {'status': 'blocked', 'code': 'BLOCKED_NATIVE_PURCHASE_NOT_CERTIFIED'}
        self.client.call.side_effect = [self.envelope, {}]
        self.assertEqual(1, exchange_once(self.client, purchase_bridge=self.bridge))
        self.assertFalse(GatewayInbox.objects.exists())

    def test_unknown_native_commit_keeps_original_pending(self):
        self.bridge.apply.side_effect = NativeResultUnknown()
        self.client.call.side_effect = [self.envelope]
        with self.assertRaises(NativeResultUnknown):
            exchange_once(self.client, purchase_bridge=self.bridge)
        self.assertFalse(GatewayInbox.objects.exists())

    def test_message_and_operation_identity_cannot_diverge(self):
        self.message['message_id'] = str(uuid.uuid4())
        self.client.call.side_effect = [self.envelope]
        with self.assertRaises(ValueError):
            exchange_once(self.client, purchase_bridge=self.bridge)
        self.bridge.apply.assert_not_called()

    def test_existing_article_and_ping_path_still_operates(self):
        self.message['kind'] = 'ping'
        self.message['payload'] = {}
        self.client.call.side_effect = [self.envelope, {}]
        self.assertEqual(1, exchange_once(self.client, purchase_bridge=self.bridge))
        self.assertEqual('pong', GatewayInbox.objects.get().result['status'])
        self.bridge.apply.assert_not_called()
