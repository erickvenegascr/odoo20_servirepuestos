import copy
import json
import uuid
from unittest import TestCase
from unittest.mock import MagicMock, patch

from pymysql.err import IntegrityError, OperationalError

from bridge.purchase_effects_contract import NativeResultUnknown, NativeResultUnverified, PurchaseEffectsError, digest
from bridge.purchase_exchange import PurchaseBridge
from bridge.purchase_order_contract import FISCALBOT_BULK_PROFILE, validate_order_request, validate_order_result
from bridge.purchase_order_sql import PurchaseOrderSql


class PurchaseOrderNumberTests(TestCase):
    def setUp(self):
        application = str(uuid.uuid4())
        operation = str(uuid.uuid4())
        order = {'order_uuid': operation, 'odoo_order_id': 130, 'odoo_order_ref': 'LAB130',
                 'vendor_code': '074', 'currency': 'CRC', 'date_order': '2026-11-20', 'delivery_days': 1,
                 'lines': [{'article_id': 7, 'por_mayor': False, 'unit_factor': 1, 'quantity': 2,
                            'unit_cost': 125.01, 'source_line_id': 84}]}
        self.payload = {'contract': 1, 'action': 'purchase.upsert', 'application_id': application,
                        'operation_id': operation, 'source_instance': 'fixture', 'order': order,
                        'request_checksum': digest(order)}
        self.reader = {'host': 'localhost', 'user': 'reader', 'database': 'sgadata', 'instance': 'fixture'}
        self.writer_config = {'host': 'localhost', 'user': 'order_writer', 'password': 'synthetic',
                              'database': 'sgadata', 'integration_schema': 'sga_order_integration',
                              'source_instance': 'fixture', 'application_id': application,
                              'audit_user': 'LAB', 'audit_station': 1}
        self.connect = MagicMock()
        self.writer = PurchaseOrderSql(self.writer_config, self.reader, application, connect=self.connect)
        self.writer._assert_grants = MagicMock()
        self.bulk_readback = False
        readback = patch('bridge.purchase_order_sql.PurchaseSource.search', side_effect=self.readback)
        self.addCleanup(readback.stop)
        readback.start()

    def readback(self, vendor, *, order_id, **kwargs):
        order = {'order_id': order_id, 'vendor_code': vendor, 'currency_code': 'CRC', 'currency_status': 'resolved',
                 'date_order': '2026-11-20T00:00:00', 'date_approval': None, 'delivery_days': 1,
                 'eligible': True, 'printed': False, 'processed': False, 'reorder': False, 'withdraw': False,
                 'lines': [{'line_key': [order_id, self.bulk_readback, 7], 'article_id': 7,
                            'unit_factor': 1 if self.bulk_readback else 12,
                            'por_mayor': self.bulk_readback, 'ordered_base': 2, 'received_base': 0,
                            'saldada': False, 'consignment': False, 'quoted_cost': 125.01}]}
        order['checksum'] = digest(order)
        return {'status': 'found', 'orders': [order]}

    def connection(self, counter, last, *, failure_at=None, prior=None):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [{'Pedido': counter}, prior, {'Pedido': last},
                                       {'Trabajado': 255, 'Servicio': False, 'Deshabilitado': False}]
        cursor.rowcount = 1
        if failure_at:
            def execute(sql, params=()):
                if sql.startswith(failure_at):
                    raise IntegrityError(1062, 'synthetic duplicate')
            cursor.execute.side_effect = execute
        return connection, cursor

    def writes(self, cursor):
        return [call.args for call in cursor.execute.call_args_list
                if call.args[0].startswith(('INSERT ', 'UPDATE '))]

    def test_retries_same_uuid_after_native_counter_moves_beyond_colliding_header(self):
        first, first_cursor = self.connection(100, 100, failure_at='INSERT INTO pedidos ')
        second, second_cursor = self.connection(101, 101)
        self.connect.side_effect = [first, second]
        result = self.writer.apply(self.payload)
        self.assertEqual(('synchronized', 102), (result['status'], result['order']['order_id']))
        self.assertEqual(self.payload['operation_id'], result['order_uuid'])
        first.rollback.assert_called_once_with()
        first.close.assert_called_once_with()
        first.commit.assert_not_called()
        self.assertEqual(1, len(self.writes(first_cursor)))
        self.assertEqual(101, self.writes(first_cursor)[0][1][0])
        self.assertEqual(102, self.writes(second_cursor)[0][1][0])
        receipt = self.writes(second_cursor)[-1][1]
        self.assertEqual((self.payload['application_id'], self.payload['operation_id'],
                          self.payload['request_checksum'], 102), receipt[:4])
        second.commit.assert_called_once_with()

    def test_blocks_without_writes_if_fresh_snapshot_has_unadvanced_or_divergent_counter(self):
        for counter, last in ((100, 100), (100, 101), (101, 102)):
            with self.subTest(counter=counter, last=last):
                first, first_cursor = self.connection(100, 100, failure_at='INSERT INTO pedidos ')
                second, second_cursor = self.connection(counter, last)
                self.connect.side_effect = [first, second]
                result = self.writer.apply(self.payload)
                self.assertEqual(('blocked', 'SGA_PURCHASE_ORDER_NUMBER_CONFLICT'),
                                 (result['status'], result['code']))
                self.assertEqual(self.payload['operation_id'], result['operation_id'])
                first.rollback.assert_called_once_with()
                second.rollback.assert_called_once_with()
                first.commit.assert_not_called()
                second.commit.assert_not_called()
                self.assertEqual(1, len(self.writes(first_cursor)))
                self.assertEqual([], self.writes(second_cursor))

    def test_stops_after_three_native_header_collisions(self):
        attempts = [self.connection(n, n, failure_at='INSERT INTO pedidos ') for n in (100, 101, 102)]
        self.connect.side_effect = [conn for conn, _cursor in attempts]
        result = self.writer.apply(self.payload)
        self.assertEqual(('blocked', 'SGA_PURCHASE_ORDER_NUMBER_CONFLICT'),
                         (result['status'], result['code']))
        for conn, cursor in attempts:
            conn.rollback.assert_called_once_with()
            conn.commit.assert_not_called()
            self.assertEqual(1, len(self.writes(cursor)))

    def test_line_duplicate_is_not_treated_as_a_native_header_collision(self):
        connection, cursor = self.connection(100, 100, failure_at='INSERT INTO pedidosarticulos ')
        self.connect.side_effect = [connection]
        with self.assertRaises(IntegrityError):
            self.writer.apply(self.payload)
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        self.assertEqual(2, len(self.writes(cursor)))

    def test_lost_ack_replays_the_original_receipt_without_another_reservation(self):
        first, cursor = self.connection(100, 100)
        first.commit.side_effect = OperationalError(2013, 'synthetic connection loss')
        self.connect.side_effect = [first]
        with self.assertRaises(NativeResultUnknown):
            self.writer.apply(self.payload)
        first.rollback.assert_not_called()
        first.close.assert_called_once_with()
        receipt = self.writes(cursor)[-1][1]
        second, second_cursor = self.connection(101, 101, prior={'request_hash': receipt[2], 'result': receipt[4]})
        self.connect.side_effect = [second]
        result = self.writer.apply(self.payload)
        self.assertEqual(json.loads(receipt[4]), result)
        self.assertEqual(self.payload['operation_id'], result['order_uuid'])
        self.assertEqual([], self.writes(second_cursor))
        second.commit.assert_not_called()

    def bulk_payload(self):
        payload = copy.deepcopy(self.payload)
        payload['contract'] = 2
        payload['order'].update(profile=FISCALBOT_BULK_PROFILE, fiscalbot_document_id=15838,
                                invoice_key='50621112600310149566000100001010000005767199999999',
                                invoice_number='00100001010000005767')
        payload['order']['lines'][0]['por_mayor'] = True
        payload['request_checksum'] = digest(payload['order'])
        return payload

    def bulk_writer(self):
        self.bulk_readback = True
        config = dict(self.writer_config, fiscalbot_bulk_enabled=True)
        writer = PurchaseOrderSql(config, self.reader, config['application_id'], connect=self.connect)
        writer._assert_grants = MagicMock()
        return writer

    def bulk_connection(self, *, prior=None, fiscal_identity=None, existing_invoice_at=None):
        connection, cursor = self.connection(100, 100)
        opening = {'NumeroUnidades': 1, 'Trabajado': 255, 'Servicio': b'\x00', 'Combo': b'\x00',
                   'PorLote': b'\x00', 'Consignacion': b'\x00', 'Deshabilitado': b'\x00', 'GlobalBulk': 6,
                   'VendorBulk': 4, 'GlobalDetail': 8, 'VendorDetail': 3}
        closing = dict(opening, GlobalBulk=8, VendorBulk=6)
        native_invoices = [{'found': 1} if index == existing_invoice_at else None for index in range(4)]
        cursor.fetchone.side_effect = [
            {'Pedido': 100}, prior, fiscal_identity, *native_invoices, {'Pedido': 100}, opening, closing,
        ]
        return connection, cursor

    def test_fiscalbot_bulk_requires_opt_in_before_opening_connection(self):
        result = self.writer.apply(self.bulk_payload())
        self.assertEqual(('blocked', 'BLOCKED_FISCALBOT_BULK_ORDER_NOT_ENABLED'), (result['status'], result['code']))
        self.connect.assert_not_called()

    def test_fiscalbot_bulk_increments_only_wholesale_and_records_invoice_identity(self):
        writer = self.bulk_writer()
        first, cursor = self.bulk_connection()
        self.connect.side_effect = [first]
        payload = self.bulk_payload()
        bridge = PurchaseBridge(payload['application_id'], self.reader, order_writer=writer)
        result = bridge.apply(payload)
        self.assertEqual(('synchronized', 101), (result['status'], result['order']['order_id']))
        line = result['order']['lines'][0]
        self.assertEqual(([101, True, 7], 1), (line['line_key'], line['unit_factor']))
        writes = self.writes(cursor)
        self.assertEqual((101, 1, 7, 2), writes[1][1][:4])
        self.assertEqual(['CantPedida=CantPedida+%s'] * 2, [write[0].split(' SET ')[1].split(' WHERE ')[0]
                                                         for write in writes[2:4]])
        journal_args = writes[-1][1]
        self.assertEqual((payload['application_id'], payload['operation_id'], 130, 15838,
                          payload['order']['invoice_key'], '074', payload['order']['invoice_number']), journal_args)
        first.commit.assert_called_once_with()

    def test_fiscalbot_bulk_same_uuid_replays_and_other_uuid_cannot_create_second_order(self):
        writer = self.bulk_writer()
        first, cursor = self.bulk_connection()
        replay, replay_cursor = self.bulk_connection(
            prior={'request_hash': self.bulk_payload()['request_checksum'],
                   'result': json.dumps({'unused': True})})
        duplicate, duplicate_cursor = self.bulk_connection(fiscal_identity={'order_uuid': self.payload['operation_id']})
        self.connect.side_effect = [first, replay, duplicate]
        payload = self.bulk_payload()
        result = writer.apply(payload)
        self.assertEqual('synchronized', result['status'])
        replay_cursor.fetchone.side_effect = [{'Pedido': 101}, {'request_hash': payload['request_checksum'],
                                                                'result': json.dumps(result)}]
        self.assertEqual(result, writer.apply(payload))
        duplicate_payload = self.bulk_payload()
        duplicate_payload['operation_id'] = duplicate_payload['order']['order_uuid'] = str(uuid.uuid4())
        duplicate_payload['request_checksum'] = digest(duplicate_payload['order'])
        rejected = writer.apply(duplicate_payload)
        self.assertEqual('SGA_FISCALBOT_PURCHASE_ORDER_ALREADY_CREATED', rejected['code'])
        self.assertEqual([], self.writes(replay_cursor))
        self.assertEqual([], self.writes(duplicate_cursor))
        duplicate.rollback.assert_called_once_with()

    def test_fiscalbot_contract_rejects_unapproved_presentation_factor_or_invoice(self):
        for field, value in [('por_mayor', False), ('unit_factor', 2), ('unit_cost', 0)]:
            with self.subTest(field=field):
                payload = self.bulk_payload()
                payload['order']['lines'][0][field] = value
                payload['request_checksum'] = digest(payload['order'])
                with self.assertRaisesRegex(PurchaseEffectsError, 'INVALID_PURCHASE_ORDER_UNIT_OR_COST'):
                    validate_order_request(payload)
        payload = self.bulk_payload()
        payload['order']['invoice_key'] = 'INVALID'
        payload['request_checksum'] = digest(payload['order'])
        with self.assertRaisesRegex(PurchaseEffectsError, 'INVALID_FISCALBOT_PURCHASE_ORDER_PROFILE'):
            validate_order_request(payload)

    def test_fiscalbot_bulk_refuses_existing_native_receipt_or_ap_before_counter_or_order_write(self):
        writer = self.bulk_writer()
        for index, table in enumerate(('recibos', 'cuentaspagar', 'cuentaspagardet', 'cuentaspagardet')):
            with self.subTest(index=index):
                connection, cursor = self.bulk_connection(existing_invoice_at=index)
                self.connect.side_effect = [connection]
                result = writer.apply(self.bulk_payload())
                self.assertEqual(('blocked', 'SGA_FISCALBOT_INVOICE_ALREADY_PRESENT'),
                                 (result['status'], result['code']))
                self.assertEqual([], self.writes(cursor))
                self.assertIn('FROM ' + table + ' ', cursor.execute.call_args.args[0])
                self.assertTrue(cursor.execute.call_args.args[0].endswith('LOCK IN SHARE MODE'))
                connection.rollback.assert_called_once_with()

    def test_fiscalbot_result_rejects_legacy_retail_readback(self):
        payload = self.bulk_payload()
        result = {key: payload[key] for key in ('contract', 'action', 'application_id', 'operation_id',
                                                'source_instance', 'request_checksum')}
        result.update(status='synchronized', order_uuid=payload['operation_id'], order_confirmed=True,
                      order=self.readback('074', order_id=101)['orders'][0])
        with self.assertRaisesRegex(NativeResultUnverified, 'SGA_PURCHASE_ORDER_LINE_MISMATCH'):
            validate_order_result(result, payload)
