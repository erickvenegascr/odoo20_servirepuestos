import json
import uuid
from unittest import TestCase
from unittest.mock import MagicMock, patch

from pymysql.err import IntegrityError, OperationalError

from bridge.purchase_effects_contract import NativeResultUnknown, digest
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
        reader = {'host': 'localhost', 'user': 'reader', 'database': 'sgadata', 'instance': 'fixture'}
        writer = {'host': 'localhost', 'user': 'order_writer', 'password': 'synthetic', 'database': 'sgadata',
                  'integration_schema': 'sga_order_integration', 'source_instance': 'fixture',
                  'application_id': application, 'audit_user': 'LAB', 'audit_station': 1}
        self.connect = MagicMock()
        self.writer = PurchaseOrderSql(writer, reader, application, connect=self.connect)
        self.writer._assert_grants = MagicMock()
        readback = patch('bridge.purchase_order_sql.PurchaseSource.search', side_effect=self.readback)
        self.addCleanup(readback.stop)
        readback.start()

    def readback(self, vendor, *, order_id, **kwargs):
        order = {'order_id': order_id, 'vendor_code': vendor, 'currency_code': 'CRC', 'currency_status': 'resolved',
                 'date_order': '2026-11-20T00:00:00', 'date_approval': None, 'delivery_days': 1,
                 'eligible': True, 'printed': False, 'processed': False, 'reorder': False, 'withdraw': False,
                 'lines': [{'line_key': [order_id, False, 7], 'article_id': 7, 'unit_factor': 12,
                            'por_mayor': False, 'ordered_base': 2, 'received_base': 0,
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
