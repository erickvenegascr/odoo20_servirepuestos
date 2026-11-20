import copy
import sqlite3
from datetime import datetime
from unittest import TestCase
from unittest.mock import MagicMock

from bridge.purchase_receipt_source import MAX_RECEIPTS, RECEIPT_TABLES, PurchaseReceiptSource
from bridge.purchase_source import _digest


def scope(**overrides):
    return {'vendor_code': '019', 'invoice_number': '00100001010000240000',
            'warehouse': 1, 'order_id': 42, **overrides}


def header(receipt=101, **overrides):
    return {'Recibo': receipt, 'Proveedor': '019', 'Factura': scope()['invoice_number'],
            'Bodega': 1, 'Pedido': 42, 'FechaRecibo': datetime(2026, 11, 18, 8, 30),
            'RecibidoCompleto': 1, 'OrderVendor': '019', 'Descuento': 0,
            'OtrosCargos': 0, **overrides}


def line(receipt=101, **overrides):
    return {'Recibo': receipt, 'PorMayor': 0, 'Articulo': 7, 'CantRecibida': 1,
            'CostoUnit': 14545.0501, 'Recibido': 1, 'Actualizado': 1, 'NoRecibido': 0,
            'OrderId': 42, 'CantPedida': 10, 'OrderReceived': 4, 'Saldada': 0,
            'NumeroUnidades': 12, **overrides}


class ReceiptSourceTests(TestCase):
    def source(self, headers=None, lines=None, engines=None):
        source = PurchaseReceiptSource({'instance': 'receipt-lab'})
        if engines is None:
            engines = [{'TABLE_NAME': table, 'ENGINE': 'InnoDB'} for table in RECEIPT_TABLES]
        source.rows = MagicMock(side_effect=[engines,
            [header()] if headers is None else headers, [line()] if lines is None else lines])
        return source

    def test_history_keeps_current_balances_separate_from_each_received_quantity(self):
        source = self.source(headers=[header(102), header(101)],
                             lines=[line(102, CantRecibida=3), line(101)])
        lookup = source.lookup(**scope(order_id=None))
        self.assertEqual(('found', 42), (lookup['status'], lookup['order_id']))
        first, second = lookup['receipts']
        self.assertEqual((101, 102), (first['receipt_id'], second['receipt_id']))
        first_line, second_line = first['lines'][0], second['lines'][0]
        self.assertTrue(first['complete'] and first_line['applied'] and second_line['applied'])
        self.assertEqual([101, False, 7], first_line['line_key'])
        self.assertEqual([42, False, 7], first_line['order_line_key'])
        self.assertEqual((1, 3), (first_line['quantity_base'], second_line['quantity_base']))
        self.assertEqual((10, 4, 6), (first_line['ordered'], first_line['order_received'], first_line['remaining']))
        self.assertEqual(14545.0501, first_line['unit_cost_net_base'])
        for evidence in (first, second, lookup):
            self.assertEqual(evidence['checksum'], _digest({
                key: value for key, value in evidence.items() if key != 'checksum'}))

    def test_wholesale_quantity_and_effective_base_cost_convert_by_pack(self):
        first = self.source(lines=[line(PorMayor=1, CostoUnit=120.0001, CantRecibida=2)])
        value = first.lookup(**scope())['receipts'][0]['lines'][0]
        self.assertEqual((2, 24, 120.0001, 10.0), tuple(value[key] for key in (
            'quantity', 'quantity_base', 'unit_cost_net', 'unit_cost_net_base')))

    def test_zero_pending_does_not_supply_receipt_evidence(self):
        source = self.source(headers=[])
        self.assertEqual({'source_instance': 'receipt-lab', 'scope': scope(), 'status': 'not_found',
                          'receipts': []}, source.lookup(**scope()))
        self.assertEqual(2, source.rows.call_count)
        found = self.source(lines=[line(Actualizado=0, OrderReceived=10, Saldada=1)]).lookup(**scope())
        self.assertEqual('found', found['status'])
        value = found['receipts'][0]['lines'][0]
        self.assertEqual((0, True, False), (value['remaining'], value['saldada'], value['applied']))

    def test_pending_line_is_distinct_from_an_applied_one(self):
        source = self.source(headers=[header(RecibidoCompleto=0)], lines=[
            line(Recibido=0, NoRecibido=1, Actualizado=0, CantRecibida=0)])
        receipt = source.lookup(**scope())['receipts'][0]
        self.assertFalse(receipt['complete'])
        self.assertTrue(receipt['lines'][0]['not_received'])
        self.assertFalse(receipt['lines'][0]['applied'])

    def test_scope_mismatch_is_conflict_even_when_other_header_matches(self):
        for conflict in (header(102, Bodega=2), header(102, Pedido=43),
                         header(102, Factura=scope()['invoice_number'] + ' '),
                         header(102, OrderVendor='020'), header(102, Pedido=None)):
            with self.subTest(conflict=conflict):
                source = self.source(headers=[header(), conflict])
                result = source.lookup(**scope())
                self.assertEqual('conflict', result['status'])
                self.assertEqual('SGA_RECEIPT_SCOPE_CONFLICT', result['code'])
                self.assertNotIn('receipts', result)
                self.assertEqual(2, source.rows.call_count)
        source = self.source(headers=[header(), header(102, Pedido=43)])
        self.assertEqual('SGA_RECEIPT_MULTIPLE_ORDERS', source.lookup(**scope(order_id=None))['code'])

    def test_outstanding_quantity_can_be_closed_separately(self):
        result = self.source(lines=[line(Saldada=1)]).lookup(**scope())
        value = result['receipts'][0]['lines'][0]
        self.assertTrue(value['saldada'])
        self.assertEqual(6, value['remaining'])

    def test_invalid_material_evidence_is_never_an_absence(self):
        examples = [
            (self.source(headers=[header(Descuento=1)]), 'SGA_RECEIPT_UNALLOCATED_NET_COST'),
            (self.source(lines=[line(OrderId=None)]), 'SGA_RECEIPT_ORDER_LINE_CONFLICT'),
            (self.source(lines=[line(), line()]), 'SGA_RECEIPT_LINE_IDENTITY_CONFLICT'),
            (self.source(lines=[]), 'SGA_RECEIPT_NO_LINES'),
            (self.source(headers=[header()] * (MAX_RECEIPTS + 1)), 'SGA_RECEIPT_LOOKUP_TOO_BROAD'),
            (self.source(headers=[header(101), header(102)], lines=[line(101, CantRecibida=3),
                 line(102, CantRecibida=2)]), 'SGA_RECEIPT_ORDER_BALANCE_CONFLICT')]
        for source, code in examples:
            with self.subTest(code=code):
                value = source.lookup(**scope())
                self.assertEqual(('conflict', code), (value['status'], value['code']))
                self.assertNotIn('receipts', value)

    def test_requires_transactional_snapshot_before_reading_business_tables(self):
        source = self.source(engines=[{'TABLE_NAME': 'recibos', 'ENGINE': 'MyISAM'}])
        result = source.lookup(**scope())
        self.assertEqual(('unavailable', 'SGA_RECEIPT_REQUIRES_INNODB'), (result['status'], result['code']))
        source.rows.assert_called_once()

    def test_scope_is_parameterized_and_preserves_invoice_leading_zeros(self):
        source = self.source()
        source.lookup(**scope())
        for sql, _params in (call.args for call in source.rows.call_args_list):
            self.assertTrue(sql.startswith('SELECT '))
            self.assertNotIn(';', sql)
            self.assertNotIn('FOR UPDATE', sql)
            self.assertNotIn(scope()['invoice_number'], sql)
        lookup_sql, lookup_params = source.rows.call_args_list[1].args
        self.assertIn('r.Proveedor=%s AND r.Factura=%s', lookup_sql)
        self.assertEqual(('019', scope()['invoice_number']), lookup_params[:2])
        self.assertNotIn('r.Bodega=%s', lookup_sql)
        quoted = scope(invoice_number="x' OR 1=1")
        source = self.source(headers=[])
        self.assertEqual('not_found', source.lookup(**quoted)['status'])
        self.assertIn(quoted['invoice_number'], source.rows.call_args_list[1].args[1])
        self.assertNotIn(quoted['invoice_number'], source.rows.call_args_list[1].args[0])

    def test_deterministic_snapshot_and_detection_of_changed_native_balance(self):
        before = [line(101, Articulo=9), line(101), line(102, CantRecibida=3)]
        headers = [header(102), header()]
        first = self.source(headers=headers, lines=before).lookup(**scope())
        same = self.source(headers=list(reversed(headers)), lines=list(reversed(before))).lookup(**scope())
        self.assertEqual(first, same)
        amended = copy.deepcopy(before)
        for row in amended:
            row['OrderReceived'] += 1
        after = self.source(headers=headers, lines=amended).lookup(**scope())
        self.assertNotEqual(first['checksum'], after['checksum'])


class ReceiptQueryFixtureTests(TestCase):
    def test_selects_enforce_vendor_invoice_and_order_line_join_in_a_database(self):
        db = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.create_function('DATABASE', 0, lambda: 'fixture')
        db.executescript("""
            ATTACH DATABASE ':memory:' AS information_schema;
            CREATE TABLE information_schema.TABLES (TABLE_SCHEMA TEXT, TABLE_NAME TEXT, ENGINE TEXT);
            CREATE TABLE pedidos (Pedido INTEGER PRIMARY KEY, Proveedor TEXT);
            CREATE TABLE artsservs (Articulo INTEGER PRIMARY KEY, NumeroUnidades INTEGER);
            CREATE TABLE pedidosarticulos (Pedido INTEGER, PorMayor INTEGER, Articulo INTEGER,
                CantPedida INTEGER, CantRecibida INTEGER, Saldada INTEGER);
            CREATE TABLE recibos (Recibo INTEGER PRIMARY KEY, Proveedor TEXT, Factura TEXT COLLATE NOCASE,
                Bodega INTEGER, Pedido INTEGER, FechaRecibo TIMESTAMP, RecibidoCompleto INTEGER,
                Descuento NUMERIC, OtrosCargos NUMERIC);
            CREATE TABLE recibosart (Recibo INTEGER, PorMayor INTEGER, Articulo INTEGER, CantRecibida INTEGER,
                CostoUnit NUMERIC, Recibido INTEGER, Actualizado INTEGER, NoRecibido INTEGER);
            INSERT INTO pedidos VALUES (42, '019'), (77, '020');
            INSERT INTO artsservs VALUES (7, 12);
            INSERT INTO pedidosarticulos VALUES (42, 0, 7, 10, 4, 0), (77, 0, 7, 1, 0, 0);
            INSERT INTO recibos VALUES
                (101, '019', '00100001010000240000', 1, 42, '2026-11-18 08:30:00', 1, 0, 0),
                (102, '019', '00100001010000240000', 1, 42, '2026-11-19 08:30:00', 1, 0, 0),
                (103, '020', '00100001010000240000', 1, 77, '2026-11-19 08:30:00', 1, 0, 0),
                (104, '019', '100001010000240000', 1, 42, '2026-11-19 08:30:00', 1, 0, 0),
                (105, '019', 'Case-Number', 1, 42, '2026-11-19 08:30:00', 1, 0, 0);
            INSERT INTO recibosart VALUES (101, 0, 7, 1, 14545.0501, 1, 1, 0),
                (102, 0, 7, 3, 14545.0501, 1, 1, 0), (103, 0, 7, 1, 14545.0501, 1, 1, 0),
                (105, 0, 7, 1, 14545.0501, 1, 1, 0);
        """)
        db.executemany('INSERT INTO information_schema.TABLES VALUES (?, ?, ?)',
                       [('fixture', name, 'InnoDB') for name in RECEIPT_TABLES])
        source = PurchaseReceiptSource({'instance': 'receipt-lab'})
        source.rows = lambda sql, params: db.execute(sql.replace('%s', '?'), params).fetchall()

        result = source.lookup(**scope())
        self.assertEqual('found', result['status'])
        self.assertEqual([101, 102], [receipt['receipt_id'] for receipt in result['receipts']])
        self.assertEqual([1, 3], [receipt['lines'][0]['quantity'] for receipt in result['receipts']])
        self.assertEqual('not_found', source.lookup(**scope(invoice_number="x' OR 1=1"))['status'])
        self.assertEqual('SGA_RECEIPT_SCOPE_CONFLICT',
                         source.lookup(**scope(invoice_number='case-number'))['code'])
        self.assertEqual('SGA_RECEIPT_SCOPE_CONFLICT', source.lookup(**scope(warehouse=2))['code'])
