"""Exercise order creation and receipt posting on an isolated SGA MariaDB fixture."""

import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import unittest
from uuid import uuid4

import pymysql

from bridge.purchase_effects_contract import digest
from bridge.purchase_order_contract import FISCALBOT_BULK_PROFILE
from bridge.purchase_order_sql import (
    FISCAL_INVOICE_READ_COLUMNS, FISCAL_ORDER_TABLE, READ_TABLES, WRITE_COLUMNS, PurchaseOrderSql,
)
from bridge.purchase_receipt_sql import (
    JOURNAL_TABLE, READ_TABLES as RECEIPT_READ_TABLES, WRITE_COLUMNS as RECEIPT_WRITE_COLUMNS, PurchaseReceiptSql,
)
from bridge.purchase_valuation_snapshot import PurchaseValuationSnapshot, compare_purchase_valuation
from bridge.tests.test_purchase_credit_receipt import liquid_credit_discount


SOCKET = os.environ.get('SGA_FISCAL_ORDER_LAB_SOCKET')
FIXTURE = os.environ.get('SGA_FISCAL_ORDER_LAB_REQUEST')
DISCOUNT_CASE = os.environ.get('SGA_FISCAL_DISCOUNT_LAB') == '1'


@unittest.skipUnless(SOCKET and FIXTURE, 'Isolated fiscal order laboratory was not selected')
class FiscalPurchaseOrderLabTests(unittest.TestCase):
    @unittest.skipIf(DISCOUNT_CASE, 'Isolated fixture selected for XML discount')
    def test_new_wholesale_order_receives_invoice_and_increments_supplier_debt_once(self):
        self._exercise_receipt(liquid_discount=False)

    @unittest.skipUnless(DISCOUNT_CASE, 'A fresh isolated XML discount fixture was not selected')
    def test_wholesale_xml_discount_writes_liquid_cost_and_credit_once(self):
        self._exercise_receipt(liquid_discount=True)

    def _exercise_receipt(self, *, liquid_discount):
        database = 'sgadata_lab'
        admin = pymysql.connect(unix_socket=SOCKET, user='root', database=database, charset='latin1',
                                cursorclass=pymysql.cursors.DictCursor, autocommit=True)
        self.addCleanup(admin.close)
        suffix = uuid4().hex[:6]
        order_journal, receipt_journal = 'sga_order_lab_' + suffix, 'sga_recv_lab_' + suffix
        app, operation = str(uuid4()), str(uuid4())
        fixture = json.loads(Path(FIXTURE).read_text())
        receipt = fixture.get('payload', fixture)['receipt']
        receipt.update(invoice_key='50621112600310149566000100001010000005767199999999',
                       invoice_number='00100001010000005767', document_id=456)
        if liquid_discount:
            receipt['lines'] = sorted(receipt['lines'], key=lambda row: row['article_id'])[:2]
            self.assertEqual(2, len(receipt['lines']))
            liquid_credit_discount(receipt)
        expected_quantity = 4 if liquid_discount else 33
        expected_total = Decimal('30103.20') if liquid_discount else Decimal('72125.64')
        vendor = receipt['vendor_code']
        articles = sorted(line['article_id'] for line in receipt['lines'])
        quantities = {line['article_id']: int(line['quantity']) for line in receipt['lines']}
        self.assertEqual(expected_quantity, sum(quantities.values()))
        order_columns = {key: columns | {'CantPedida'} if key in (
            ('UPDATE', 'artsservs'), ('UPDATE', 'articulosproveedor')) else columns
            for key, columns in WRITE_COLUMNS.items()}
        order_columns.update({('SELECT', table): columns for table, columns in FISCAL_INVOICE_READ_COLUMNS.items()})

        with admin.cursor() as cursor:
            cursor.execute('SELECT DATABASE() AS db,@@skip_networking AS isolated,@@datadir AS path')
            scope = cursor.fetchone()
            self.assertEqual((database, 1), (scope['db'], scope['isolated']))
            self.assertTrue(scope['path'].startswith('/var/tmp/sga-r56632-'))
            order_login = self.prepare_writer(
                admin, order_journal, database,
                ('purchase-order-receipt.sql', 'purchase-order-fiscal-identity.sql'),
                'sga_order_integration', READ_TABLES, order_columns, ('purchase_order_receipt', FISCAL_ORDER_TABLE))
            receipt_login = self.prepare_writer(
                admin, receipt_journal, database, ('purchase-receipt-application.sql',),
                'sga_receipt_integration', RECEIPT_READ_TABLES, RECEIPT_WRITE_COLUMNS, (JOURNAL_TABLE,))
            cursor.execute('UPDATE pgconsecutivos SET Pedido=(SELECT MAX(Pedido) FROM pedidos) '
                           'WHERE Llave=0 AND Pedido<(SELECT MAX(Pedido) FROM pedidos)')
            cursor.execute('SELECT Pedido,Recibo FROM pgconsecutivos WHERE Llave=0')
            counters = cursor.fetchone()
            before_pending = {article: self.pending_stock(cursor, article, vendor) for article in articles}
            before_debt = self.supplier_debt(cursor, vendor)

        source = {'host': 'localhost', 'database': database, 'user': 'not-used-by-writer', 'instance': 'fixture',
                  'currency_map': {'¢': 'CRC'}, 'national_currency_code': '¢'}
        config = {**source, 'application_id': app, 'source_instance': source['instance'],
                  'audit_user': 'ODOOSGA', 'audit_station': 250, 'unix_socket': SOCKET}
        order = {'order_uuid': operation, 'odoo_order_id': 123, 'odoo_order_ref': 'LAB-P000123',
                 'vendor_code': vendor, 'currency': 'CRC', 'date_order': receipt['invoice_date'], 'delivery_days': 1,
                 'profile': FISCALBOT_BULK_PROFILE, 'fiscalbot_document_id': receipt['document_id'],
                 'invoice_key': receipt['invoice_key'], 'invoice_number': receipt['invoice_number'],
                 'lines': [{'article_id': line['article_id'], 'por_mayor': True, 'unit_factor': 1,
                            'quantity': int(line['quantity']), 'unit_cost': line['unit_cost'],
                            'source_line_id': line['xml_line_id']} for line in
                           sorted(receipt['lines'], key=lambda value: value['article_id'])]}
        payload = {'contract': 2, 'action': 'purchase.upsert', 'application_id': app,
                   'operation_id': operation, 'source_instance': 'fixture',
                   'order': order, 'request_checksum': digest(order)}
        order_writer = PurchaseOrderSql({**config, **order_login, 'integration_schema': order_journal,
                                        'fiscalbot_bulk_enabled': True}, source, app)
        order_result = order_writer.apply(payload)
        self.assertEqual('synchronized', order_result['status'], order_result)
        order_no = counters['Pedido'] + 1
        self.assertEqual([[order_no, True, article] for article in articles],
                         [line['line_key'] for line in order_result['order']['lines']])
        self.assertEqual(order_result, order_writer.apply(payload))
        duplicate_order = copy.deepcopy(payload)
        duplicate_order['operation_id'] = duplicate_order['order']['order_uuid'] = str(uuid4())
        duplicate_order['request_checksum'] = digest(duplicate_order['order'])
        self.assertEqual('SGA_FISCALBOT_PURCHASE_ORDER_ALREADY_CREATED',
                         order_writer.apply(duplicate_order)['code'])
        with admin.cursor() as cursor:
            for article, (global_bulk, global_detail, vendor_bulk, vendor_detail) in before_pending.items():
                quantity = quantities[article]
                self.assertEqual((global_bulk + quantity, global_detail, vendor_bulk + quantity, vendor_detail),
                                 self.pending_stock(cursor, article, vendor))
            self.assertEqual(before_debt, self.supplier_debt(cursor, vendor))

        receipt.update(order_id=order_no, order_checksum=order_result['order']['checksum'])
        for row in receipt['lines']:
            row['order_line_key'] = [order_no, True, row['article_id']]
        receipt_payload = {'contract': 2, 'action': 'purchase.receive', 'application_id': app,
                           'operation_id': str(uuid4()), 'source_instance': 'fixture',
                           'receipt': receipt, 'request_checksum': digest(receipt)}
        receipt_writer = PurchaseReceiptSql({**config, **receipt_login, 'integration_schema': receipt_journal,
                                             'profile': 'crc_iva13_credit_factor_one',
                                             'evidence_reference': 'isolated MariaDB fiscal order flow'}, source, app)
        proof = PurchaseValuationSnapshot(source)
        proof.connection = admin
        before = proof.capture(receipt_payload)
        self.assertEqual('not_found', before['receipt_verification']['status'])
        self.assertEqual(expected_quantity, sum(row['remaining'] for row in before['lines']))
        result = receipt_writer.apply(receipt_payload)
        self.assertEqual('synchronized', result['status'], result)
        self.assertEqual([counters['Recibo'] + 1], result['receipt_ids'])
        self.assertEqual(expected_quantity, sum(row['quantity_base'] for row in result['lines']))
        self.assertEqual(expected_total, Decimal(str(result['payable']['amount_total'])))
        self.assertEqual(result, receipt_writer.apply(receipt_payload))
        duplicate_receipt = {**receipt_payload, 'operation_id': str(uuid4())}
        self.assertEqual('SGA_RECEIPT_INVOICE_ALREADY_PRESENT', receipt_writer.apply(duplicate_receipt)['code'])
        fresh_journal = 'sga_order_verify_' + suffix
        fresh_login = self.prepare_writer(
            admin, fresh_journal, database, ('purchase-order-receipt.sql', 'purchase-order-fiscal-identity.sql'),
            'sga_order_integration', READ_TABLES, order_columns, ('purchase_order_receipt', FISCAL_ORDER_TABLE))
        native_check_writer = PurchaseOrderSql({**config, **fresh_login, 'integration_schema': fresh_journal,
                                              'fiscalbot_bulk_enabled': True}, source, app)
        duplicate_native = native_check_writer.apply(duplicate_order)
        self.assertEqual(('blocked', 'SGA_FISCALBOT_INVOICE_ALREADY_PRESENT'),
                         (duplicate_native['status'], duplicate_native['code']))
        after = proof.capture(receipt_payload)
        if before['captured_at_utc'] == after['captured_at_utc']:
            time.sleep(1.05)
            after = proof.capture(receipt_payload)
        comparison = compare_purchase_valuation(receipt_payload, before, after)
        self.assertEqual(('matched', expected_quantity, 0), (comparison['status'], comparison['remaining_before'],
                                            comparison['remaining_after']), comparison)
        with admin.cursor() as cursor:
            self.assertEqual(before_pending, {article: self.pending_stock(cursor, article, vendor)
                                              for article in articles})
            self.assertEqual(before_debt + expected_total, self.supplier_debt(cursor, vendor))
            cursor.execute('SELECT Pedido,Recibo FROM pgconsecutivos WHERE Llave=0')
            self.assertEqual({'Pedido': order_no, 'Recibo': counters['Recibo'] + 1}, cursor.fetchone())
            if liquid_discount:
                self._verify_liquid_discount(cursor, result['receipt_ids'][0], receipt, articles)
            for schema, table in ((order_journal, FISCAL_ORDER_TABLE), (receipt_journal, JOURNAL_TABLE)):
                cursor.execute(f'SELECT COUNT(*) AS total FROM `{schema}`.`{table}`')
                self.assertEqual(1, cursor.fetchone()['total'])

    def _verify_liquid_discount(self, cursor, number, receipt, articles):
        cursor.execute('SELECT Descuento,MontoImptoVenta FROM recibos WHERE Recibo=%s', (number,))
        self.assertEqual({'Descuento': 0.0, 'MontoImptoVenta': 3463.20}, cursor.fetchone())
        cursor.execute('SELECT Articulo,CantRecibida,CostoUnit,CostoUnitCIF,TipoIVA,IVA FROM recibosart '
                       'WHERE Recibo=%s ORDER BY Articulo', (number,))
        rows = cursor.fetchall()
        expected = [{'Articulo': article, 'CantRecibida': 2, 'CostoUnit': 6660.0, 'CostoUnitCIF': 6660.0,
                     'TipoIVA': '08', 'IVA': 0.13} for article in articles]
        self.assertEqual(expected, rows)
        self.assertEqual(Decimal('26640'), sum(row['CantRecibida'] * Decimal(str(row['CostoUnit'])) for row in rows))
        cursor.execute('SELECT h.MontoDeudaReal,h.Cerrada+0 AS Cerrada,d.Monto,d.MontoImptoVenta,d.TipoCredito, '
                       'DATE(DATE_ADD(d.Fecha,INTERVAL d.TipoCredito DAY)) AS Vence '
                       'FROM cuentaspagar h JOIN cuentaspagardet d ON (d.Proveedor=h.Proveedor '
                       'AND d.FacturaPrimaria=h.FacturaPrimaria) WHERE h.Proveedor=%s AND h.FacturaPrimaria=%s',
                       (receipt['vendor_code'], receipt['invoice_number']))
        payable = cursor.fetchall()
        self.assertEqual(1, len(payable))
        row = payable[0]
        self.assertEqual((30103.20, 0, 30103.20, 3463.20, 30), tuple(row[key] for key in (
            'MontoDeudaReal', 'Cerrada', 'Monto', 'MontoImptoVenta', 'TipoCredito')))
        self.assertEqual(receipt['fiscal']['payment_terms']['due_date'], row['Vence'].isoformat())
        print(f'LIQUID_DISCOUNT_LAB receipt={number} quantity=4 net=26640 iva=3463.20 payable=30103.20 credit=30')

    def prepare_writer(self, admin, schema, database, files, original, read_tables, write_columns, journals):
        writer_user, password = 'bulk_' + uuid4().hex[:8], uuid4().hex
        root = Path(__file__).resolve().parents[3]
        with admin.cursor() as cursor:
            for file in files:
                source = root.joinpath('deploy', file).read_text()
                sql = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
                for statement in sql.replace(original, schema).split(';'):
                    if statement.strip():
                        cursor.execute(statement)
            cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s', (writer_user, 'localhost', password))
            self.addCleanup(self.drop_user, admin, writer_user)
            for table in sorted(read_tables):
                cursor.execute(f'GRANT SELECT ON `{database}`.`{table}` TO %s@%s', (writer_user, 'localhost'))
            for (privilege, table), names in sorted(write_columns.items()):
                columns = ','.join(f'`{name}`' for name in sorted(names))
                cursor.execute(f'GRANT {privilege} ({columns}) ON `{database}`.`{table}` TO %s@%s',
                               (writer_user, 'localhost'))
            for table in journals:
                cursor.execute(f'GRANT SELECT, INSERT ON `{schema}`.`{table}` TO %s@%s', (writer_user, 'localhost'))
        return {'user': writer_user, 'password': password}

    @staticmethod
    def pending_stock(cursor, article, vendor):
        cursor.execute('SELECT a.CantPedida AS GlobalBulk,a.CantPedidaDet AS GlobalDetail,'
                       'v.CantPedida AS VendorBulk,v.CantPedidaDet AS VendorDetail FROM artsservs a '
                       'JOIN articulosproveedor v ON v.Articulo=a.Articulo WHERE a.Articulo=%s AND v.Proveedor=%s',
                       (article, vendor))
        row = cursor.fetchone()
        return tuple(row[k] for k in ('GlobalBulk', 'GlobalDetail', 'VendorBulk', 'VendorDetail'))

    @staticmethod
    def supplier_debt(cursor, vendor):
        cursor.execute('SELECT MontoDeuda FROM proveedores WHERE Proveedor=%s', (vendor,))
        return Decimal(str(cursor.fetchone()['MontoDeuda']))

    @staticmethod
    def drop_user(admin, name):
        with admin.cursor() as cursor:
            cursor.execute('DROP USER %s@%s', (name, 'localhost'))
