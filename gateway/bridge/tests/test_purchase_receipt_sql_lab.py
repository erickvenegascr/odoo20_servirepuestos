"""End-to-end MariaDB test for a private copy of the selected receipt case."""

import copy
import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

import pymysql

from bridge.purchase_effects_contract import NativeResultUnknown, request


SOCKET = os.environ.get('SGA_RECEIPT_LAB_SOCKET')
FIXTURE = os.environ.get('SGA_RECEIPT_LAB_REQUEST')


@unittest.skipUnless(SOCKET and FIXTURE, 'Private SGA receipt lab was not selected')
class PurchaseReceiptSqlLabTests(unittest.TestCase):
    def test_atomic_receipt_credit_valuation_and_duplicate_replay(self):
        from bridge.purchase_receipt_sql import JOURNAL_TABLE, READ_TABLES, WRITE_COLUMNS, PurchaseReceiptSql
        from bridge.purchase_source import PurchaseSource
        from bridge.purchase_effects_contract import digest
        from bridge.purchase_supplier_balance import supplier_money
        from bridge.purchase_valuation_snapshot import PurchaseValuationSnapshot, compare_purchase_valuation

        operation = request(json.loads(Path(FIXTURE).read_text()))
        receipt = operation['receipt']
        database, journal = 'sgadata_lab', 'sga_receipt_integration'
        admin = pymysql.connect(unix_socket=SOCKET, user='root', database=database, charset='latin1',
                                cursorclass=pymysql.cursors.DictCursor, autocommit=True)
        self.addCleanup(admin.close)
        actor = 'receipt_lab_' + uuid4().hex[:4]
        password = uuid4().hex
        reader = {'host': 'localhost', 'database': database, 'user': 'unused-read-only',
                  'instance': operation['source_instance'], 'currency_map': {'¢': 'CRC'},
                  'national_currency_code': '¢'}
        config = {**reader, 'user': actor, 'password': password, 'unix_socket': SOCKET,
                  'application_id': operation['application_id'], 'source_instance': operation['source_instance'],
                  'integration_schema': journal, 'profile': 'crc_iva13_credit_factor_one',
                  'evidence_reference': 'isolated MariaDB fixture', 'audit_user': 'ODOOSGA', 'audit_station': 250}

        with admin.cursor() as cursor:
            cursor.execute('SELECT @@skip_networking AS no_network, DATABASE() AS db, @@datadir AS dir')
            server = cursor.fetchone()
            self.assertEqual((1, database), (server['no_network'], server['db']))
            self.assertIn('/var/tmp/sga-r56632-', server['dir'])
            cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s', (actor, 'localhost', password))
            self.addCleanup(self._drop_lab_account, admin, actor)
            for table in sorted(READ_TABLES):
                cursor.execute(f'GRANT SELECT ON `{database}`.`{table}` TO %s@%s', (actor, 'localhost'))
            for (privilege, table), columns in sorted(WRITE_COLUMNS.items()):
                names = ','.join(f'`{name}`' for name in sorted(columns))
                cursor.execute(f'GRANT {privilege} ({names}) ON `{database}`.`{table}` TO %s@%s',
                               (actor, 'localhost'))
            cursor.execute(f'GRANT SELECT,INSERT ON `{journal}`.`{JOURNAL_TABLE}` TO %s@%s', (actor, 'localhost'))
            cursor.execute('SELECT Recibo FROM pgconsecutivos WHERE Llave=0')
            counter = cursor.fetchone()['Recibo']
            cursor.execute('SELECT MontoDeuda FROM proveedores WHERE Proveedor=%s', (receipt['vendor_code'],))
            opening_debt = supplier_money(cursor.fetchone()['MontoDeuda'])

        # Approval in production must compare the original checksum. This copy
        # can normalize transport timestamps and intentionally isolates the order.
        order_reader = PurchaseSource(reader)
        order_reader.connection = admin
        source = order_reader.search(receipt['vendor_code'], order_id=receipt['order_id'], include_closed=True, limit=1)
        receipt['order_checksum'] = source['orders'][0]['checksum']
        operation['request_checksum'] = digest(receipt)

        proof = PurchaseValuationSnapshot(reader)
        proof.connection = admin
        before = proof.capture(operation)
        self.assertEqual('not_found', before['receipt_verification']['status'])
        self.assertEqual(65, sum(line['remaining'] for line in before['lines']))

        class RollbackBeforeCommit:
            def __init__(self, **kwargs):
                self.connection = pymysql.connect(**kwargs)

            def __getattr__(self, attribute):
                return getattr(self.connection, attribute)

            def commit(self):
                self.connection.rollback()
                raise pymysql.err.OperationalError(2013, 'Lab rolled back before commit')

        with self.assertRaises(NativeResultUnknown):
            PurchaseReceiptSql(config, reader, operation['application_id'], connect=RollbackBeforeCommit).apply(operation)
        unchanged = proof.capture(operation)
        self.assertEqual(before['lines'], unchanged['lines'])
        self.assertEqual('not_found', unchanged['receipt_verification']['status'])
        with admin.cursor() as cursor:
            cursor.execute('SELECT Recibo FROM pgconsecutivos WHERE Llave=0')
            self.assertEqual(counter, cursor.fetchone()['Recibo'])
            cursor.execute('SELECT MontoDeuda FROM proveedores WHERE Proveedor=%s', (receipt['vendor_code'],))
            self.assertEqual(opening_debt, supplier_money(cursor.fetchone()['MontoDeuda']))

        class LostCommitResponse:
            def __init__(self, **kwargs):
                self.connection = pymysql.connect(**kwargs)

            def __getattr__(self, attribute):
                return getattr(self.connection, attribute)

            def commit(self):
                self.connection.commit()
                raise pymysql.err.OperationalError(2013, 'Lab lost commit response')

        with self.assertRaises(NativeResultUnknown):
            PurchaseReceiptSql(config, reader, operation['application_id'], connect=LostCommitResponse).apply(operation)
        writer = PurchaseReceiptSql(config, reader, operation['application_id'])
        result = writer.apply(operation)
        self.assertEqual('synchronized', result['status'])
        self.assertEqual([counter + 1], result['receipt_ids'])
        self.assertEqual(33, sum(line['quantity_base'] for line in result['lines']))
        self.assertEqual(72125.64, result['payable']['amount_total'])
        self.assertEqual('2026-12-19', result['payable']['payment_terms']['due_date'])
        self.assertEqual(result, writer.apply(operation))

        other_uuid = copy.deepcopy(operation)
        other_uuid['operation_id'] = str(uuid4())
        duplicate = writer.apply(other_uuid)
        self.assertEqual(('blocked', 'SGA_RECEIPT_INVOICE_ALREADY_PRESENT'), (duplicate['status'], duplicate['code']))

        after = proof.capture(operation)
        # MariaDB 5.5 tracks datetime to one second. A test run can legitimately
        # capture both observations in that same second.
        if before['captured_at_utc'] == after['captured_at_utc']:
            import time

            time.sleep(1.05)
            after = proof.capture(operation)
        comparison = compare_purchase_valuation(operation, before, after)
        self.assertEqual(('matched', 65, 32), (comparison['status'], comparison['remaining_before'],
                                             comparison['remaining_after']), comparison)
        with admin.cursor() as cursor:
            cursor.execute('SELECT MontoDeuda FROM proveedores WHERE Proveedor=%s', (receipt['vendor_code'],))
            self.assertEqual(opening_debt + supplier_money(result['payable']['amount_total']),
                             supplier_money(cursor.fetchone()['MontoDeuda']))
            cursor.execute(f'SELECT COUNT(*) AS total FROM `{journal}`.`{JOURNAL_TABLE}`')
            self.assertEqual(1, cursor.fetchone()['total'])
            cursor.execute('SELECT COUNT(*) AS total FROM recibos WHERE Proveedor=%s AND Factura=%s',
                           (receipt['vendor_code'], receipt['invoice_number']))
            self.assertEqual(1, cursor.fetchone()['total'])

    @staticmethod
    def _drop_lab_account(admin, actor):
        with admin.cursor() as cursor:
            cursor.execute('DROP USER %s@%s', (actor, 'localhost'))
