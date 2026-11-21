import json
import uuid
from unittest.mock import MagicMock

import pymysql
from django.test import SimpleTestCase, TestCase

from bridge.article_contract import digest
from bridge.models import GatewayInbox
from bridge.services_exchange import exchange_once
from bridge.supplier_update_contract import SupplierResultUnverified, validate_update_request
from bridge.supplier_update_sql import JOURNAL_TABLE, READ_COLUMNS, WRITE_COLUMNS, SupplierUpdateSql


def operation(**changes):
    payload = {
        'contract': 1, 'action': 'supplier.update', 'application_id': str(uuid.uuid4()),
        'operation_id': str(uuid.uuid4()), 'source_instance': 'isolated-sga',
        'relation': {'article_id': 498, 'vendor_code': '318', 'product_code': '000498', 'priority': 1,
                     'disabled': False, 'vendor_active': True},
        'changes': changes or {'product_code': '000498-A', 'priority': 2},
    }
    payload['request_checksum'] = digest({key: payload[key] for key in ('relation', 'changes')})
    return payload


def writer_for(payload, connect):
    source = {'instance': payload['source_instance'], 'database': 'sgadata', 'host': '127.0.0.1', 'user': 'reader'}
    config = {**source, 'enabled': True, 'application_id': payload['application_id'],
              'source_instance': payload['source_instance'], 'user': 'supplier_writer', 'password': 'lab-only',
              'integration_schema': 'sga_supplier_integration', 'audit_user': 'ODOO', 'audit_station': 4}
    return SupplierUpdateSql(config, source, payload['application_id'], connect=connect)


class SupplierNativeWriterTests(SimpleTestCase):
    def setUp(self):
        self.payload = operation()
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.connect = MagicMock(return_value=self.connection)
        self.writer = writer_for(self.payload, self.connect)
        self.writer._assert_database = MagicMock()
        self.native = {'Articulo': 498, 'Proveedor': '318', 'CodigoArticulo': '000498', 'Prioridad': 1,
                       'Deshabilitado': b'\x00', 'Activo': b'\x01', 'Trabajado': 255}
        self.cursor.rowcount = 1

    def run_native(self):
        after = {**self.native, 'CodigoArticulo': '000498-A', 'Prioridad': 2}
        self.cursor.fetchone.side_effect = [None, self.native, after]
        self.cursor.fetchall.side_effect = [[{'Articulo': 498}], [], []]
        return self.writer.apply(self.payload)

    def test_validates_checksum_and_native_types_before_connecting(self):
        for changes in ({'priority': True}, {'priority': 32768}, {'product_code': ''}, {'product_code': ' 日本 '},
                        {'unit_price': 23}, {'priority': 1}, {'product_code': '000498'}):
            invalid = {**self.payload, 'changes': changes}
            invalid['request_checksum'] = digest({key: invalid[key] for key in ('relation', 'changes')})
            self.assertEqual('INVALID_SUPPLIER_UPDATE_CHANGES', self.writer.apply(invalid)['code'])
        self.assertEqual('INVALID_SUPPLIER_UPDATE_CHECKSUM',
                         self.writer.apply({**self.payload, 'request_checksum': '0' * 64})['code'])
        self.connect.assert_not_called()
        self.assertEqual('é', validate_update_request(operation(product_code='é'))['changes']['product_code'])

    def test_commits_exact_readback_and_journal_then_replays_without_writing(self):
        result = self.run_native()
        self.assertEqual({'product_code': '000498-A', 'priority': 2},
                         {key: result['relation'][key] for key in ('product_code', 'priority')})
        updates = [call.args for call in self.cursor.execute.call_args_list if call.args[0].startswith('UPDATE ')]
        self.assertEqual(1, len(updates))
        self.assertEqual((2, '000498-A', 'ODOO', 4, 498, '318'), updates[0][1])
        self.assertIn('WHERE Articulo=%s AND Proveedor=%s', updates[0][0])
        insert = next(call.args for call in self.cursor.execute.call_args_list if call.args[0].startswith('INSERT '))
        self.assertEqual(result, json.loads(insert[1][-1]))
        self.connection.commit.assert_called_once()
        self.connection.reset_mock()
        self.cursor.fetchone.side_effect = None
        self.cursor.fetchone.return_value = {'request_hash': self.payload['request_checksum'], 'result': insert[1][-1]}
        self.assertEqual(result, self.writer.apply(self.payload))
        self.assertFalse(any(call.args[0].startswith(('INSERT ', 'UPDATE ')) for call in self.cursor.execute.call_args_list))
        self.connection.commit.assert_not_called()

    def test_ambiguous_sku_and_stale_and_inactive_never_write(self):
        cases = (
            (self.native, [{'Articulo': 999}], 'SGA_SUPPLIER_PRODUCT_CODE_AMBIGUOUS'),
            ({**self.native, 'Prioridad': 3}, [], 'SGA_SUPPLIER_RELATION_STALE'),
            ({**self.native, 'Deshabilitado': b'\x01'}, [], 'SGA_SUPPLIER_RELATION_INACTIVE'),
            ({**self.native, 'Trabajado': 1}, [], 'SGA_SUPPLIER_ARTICLE_INACTIVE'),
            ({**self.native, 'Proveedor': '319'}, [], 'SGA_SUPPLIER_RELATION_NOT_FOUND'),
        )
        for row, duplicates, code in cases:
            with self.subTest(code=code):
                self.connection.reset_mock()
                self.cursor.fetchone.side_effect = [None, row]
                self.cursor.fetchall.side_effect = None
                self.cursor.fetchall.return_value = duplicates
                result = self.writer.apply(self.payload)
                self.assertEqual(('conflict', code), (result['status'], result['code']))
                self.assertFalse(any(call.args[0].startswith(('INSERT ', 'UPDATE '))
                                     for call in self.cursor.execute.call_args_list))
                self.connection.commit.assert_not_called()
                self.connection.close.assert_called_once()

    def test_supplier_snapshot_alias_blocks_even_when_native_comparison_is_distinct(self):
        self.cursor.fetchone.side_effect = [None, self.native]
        self.cursor.fetchall.side_effect = [[], [], [{'Articulo': 759, 'SnapshotCode': b' 000498-A'}]]
        result = self.writer.apply(self.payload)
        self.assertEqual(('conflict', 'SGA_SUPPLIER_PRODUCT_CODE_AMBIGUOUS'), (result['status'], result['code']))
        self.connection.commit.assert_not_called()

    def test_priority_only_leaves_native_empty_code_untouched(self):
        self.payload['relation']['product_code'] = ''
        self.payload['changes'] = {'priority': 0}
        self.payload['request_checksum'] = digest({key: self.payload[key] for key in ('relation', 'changes')})
        row = {**self.native, 'CodigoArticulo': None}
        self.cursor.fetchone.side_effect = [None, row, {**row, 'Prioridad': 0}]
        result = self.writer.apply(self.payload)
        self.assertEqual(0, result['relation']['priority'])
        change = next(call.args for call in self.cursor.execute.call_args_list if call.args[0].startswith('UPDATE '))
        self.assertNotIn('CodigoArticulo', change[0])
        self.assertEqual((0, 'ODOO', 4, 498, '318'), change[1])

    def test_native_readback_failure_closes_uncommitted_write(self):
        self.cursor.fetchone.side_effect = [None, self.native, self.native]
        self.cursor.fetchall.return_value = []
        result = self.writer.apply(self.payload)
        self.assertEqual('SGA_SUPPLIER_RELATION_READBACK_MISMATCH', result['code'])
        self.assertFalse(any(call.args[0].startswith('INSERT ') for call in self.cursor.execute.call_args_list))
        self.connection.commit.assert_not_called()
        self.connection.close.assert_called_once()

    def test_native_lock_retries_retain_identity_and_have_a_bounded_failure(self):
        self.writer._apply = MagicMock(side_effect=pymysql.err.OperationalError(1213, 'lab deadlock'))
        result = self.writer.apply(self.payload)
        self.assertEqual(('unavailable', 'SGA_SUPPLIER_WRITER_BUSY', self.payload['operation_id']),
                         (result['status'], result['code'], result['operation_id']))
        self.assertEqual([self.payload] * 3, [call.args[0] for call in self.writer._apply.call_args_list])

    def test_lost_commit_and_corrupt_journal_do_not_return_rejection(self):
        self.connection.commit.side_effect = pymysql.err.OperationalError(2013, 'lost native response')
        with self.assertRaisesRegex(SupplierResultUnverified, 'COMMIT_UNCONFIRMED'):
            self.run_native()
        self.cursor.fetchone.side_effect = None
        self.cursor.fetchone.return_value = {'request_hash': self.payload['request_checksum'], 'result': '{bad'}
        with self.assertRaisesRegex(SupplierResultUnverified, 'NATIVE_RESULT_INVALID'):
            self.writer.apply(self.payload)

    def test_credentials_must_match_exact_column_privileges(self):
        writer = writer_for(self.payload, self.connect)
        grants = [{'grant': 'GRANT USAGE ON *.* TO sample'}]
        for table, columns in READ_COLUMNS.items():
            grants.append({'grant': f'GRANT SELECT ({", ".join(sorted(columns))}) ON `sgadata`.`{table}` TO sample'})
        grants.append({'grant': f'GRANT UPDATE ({", ".join(sorted(WRITE_COLUMNS))}) '
                               'ON `sgadata`.`articulosproveedor` TO sample'})
        grants.append({'grant': f'GRANT SELECT, INSERT ON `sga_supplier_integration`.`{JOURNAL_TABLE}` TO sample'})
        metadata = [{'TABLE_SCHEMA': 'sgadata', 'TABLE_NAME': table, 'ENGINE': 'InnoDB'} for table in READ_COLUMNS]
        metadata.append({'TABLE_SCHEMA': 'sga_supplier_integration', 'TABLE_NAME': JOURNAL_TABLE, 'ENGINE': 'InnoDB'})
        self.cursor.fetchall.side_effect = [grants, metadata]
        self.cursor.fetchone.side_effect = [{'db': 'sgadata', 'socket': '/private/lab.sock'}, {'Value': 'OFF'}]
        writer._assert_database(self.cursor)
        self.cursor.fetchall.side_effect = None
        self.cursor.fetchall.return_value = [*grants, {'grant': 'GRANT UPDATE (PrecioCotiz) '
                                                             'ON `sgadata`.`articulosproveedor` TO sample'}]
        writer = writer_for(self.payload, self.connect)
        self.assertEqual('SGA_SUPPLIER_WRITER_ACCOUNT_TOO_BROAD', writer.apply(self.payload)['code'])
        self.connection.begin.assert_not_called()


class SupplierExchangeTests(TestCase):
    def setUp(self):
        self.payload = operation()
        self.client = MagicMock()
        self.client.config = {'application_id': self.payload['application_id']}
        self.envelope = {'application_id': self.payload['application_id'], 'messages': [
            {'message_id': self.payload['operation_id'], 'kind': 'request', 'payload': self.payload, 'lease': 'lease'}]}
        self.client.call.side_effect = lambda method, **kwargs: self.envelope if method == 'poll' else {}
        self.writer = MagicMock(application_id=self.payload['application_id'])

    def test_disabled_adapter_leaves_message_waiting_and_enabled_dispatches_once(self):
        self.assertEqual(0, exchange_once(self.client))
        self.assertFalse(GatewayInbox.objects.exists())
        self.writer.apply.return_value = {'status': 'synchronized'}
        self.assertEqual(1, exchange_once(self.client, supplier_writer=self.writer))
        self.assertEqual(1, exchange_once(self.client, supplier_writer=self.writer))
        self.writer.apply.assert_called_once_with(self.payload)
        self.assertEqual(1, GatewayInbox.objects.count())

    def test_unavailable_and_commit_uncertain_never_poison_the_inbox(self):
        self.writer.apply.return_value = {'status': 'unavailable', 'code': 'SGA_SUPPLIER_WRITER_BUSY'}
        self.assertEqual(1, exchange_once(self.client, supplier_writer=self.writer))
        self.assertFalse(GatewayInbox.objects.exists())
        self.client.reset_mock()
        self.writer.apply.side_effect = SupplierResultUnverified('commit outcome unknown')
        with self.assertRaises(SupplierResultUnverified):
            exchange_once(self.client, supplier_writer=self.writer)
        self.client.call.assert_called_once_with('poll')
        self.assertFalse(GatewayInbox.objects.exists())
