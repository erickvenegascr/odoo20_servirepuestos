"""Opt-in MariaDB 5.5 test in new schemas on the private SGA lab instance."""

import os
import secrets
import unittest
import uuid
from pathlib import Path

import pymysql

from bridge.article_contract import digest
from bridge.supplier_update_contract import SupplierResultUnverified
from bridge.supplier_update_sql import JOURNAL_TABLE, READ_COLUMNS, WRITE_COLUMNS, SupplierUpdateSql


SOCKET = os.environ.get('SGA_SUPPLIER_UPDATE_LAB_SOCKET')


@unittest.skipUnless(SOCKET, 'A private SGA MariaDB socket was not configured')
class SupplierUpdateSqlLabTests(unittest.TestCase):
    def test_authorized_edit_readback_lost_ack_stale_and_native_duplicate(self):
        suffix = uuid.uuid4().hex[:8]
        database = f'supplier_source_lab_{suffix}'
        integration = f'supplier_update_lab_{suffix}'
        username = f'su_lab_{suffix}'
        password = secrets.token_hex(24)
        catalog = 'sgadata_lab'
        socket = Path(SOCKET)
        self.assertRegex(str(socket), r'^/var/tmp/sga-r56632-[a-zA-Z0-9-]+/private/lab\.sock$')
        admin = pymysql.connect(unix_socket=str(socket), user='root', autocommit=True, charset='utf8',
                                binary_prefix=True, cursorclass=pymysql.cursors.DictCursor)
        self.addCleanup(admin.close)
        with admin.cursor() as cursor:
            cursor.execute('SELECT @@skip_networking isolated,@@socket socket,@@datadir datadir,VERSION() version')
            server = cursor.fetchone()
            self.assertEqual((1, str(socket)), (server['isolated'], server['socket']))
            self.assertEqual(socket.parent / 'data', Path(server['datadir']).resolve())
            self.assertTrue(server['version'].startswith('5.5.'))
            self._seed_schemas(cursor, database, integration, catalog)
            cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s', (username, 'localhost', password))
            self.addCleanup(self._drop_user, admin, username)
            for table, columns in READ_COLUMNS.items():
                cursor.execute(f'GRANT SELECT ({self._columns(columns)}) ON `{database}`.`{table}` TO %s@%s',
                               (username, 'localhost'))
            cursor.execute(f'GRANT UPDATE ({self._columns(WRITE_COLUMNS)}) '
                           f'ON `{database}`.`articulosproveedor` TO %s@%s', (username, 'localhost'))
            cursor.execute(f'GRANT SELECT,INSERT ON `{integration}`.`{JOURNAL_TABLE}` TO %s@%s',
                           (username, 'localhost'))
            quote_before = self._quote(cursor, database)

        app = str(uuid.uuid4())
        source = {'instance': 'isolated-supplier-source', 'database': database, 'host': 'localhost', 'user': 'reader'}
        config = {**source, 'application_id': app, 'source_instance': source['instance'], 'enabled': True,
                  'integration_schema': integration, 'user': username, 'password': password,
                  'unix_socket': str(socket), 'audit_user': 'ODOO', 'audit_station': 4}
        writer = SupplierUpdateSql(config, source, app)
        previous = {'article_id': 498, 'vendor_code': '318', 'product_code': 'QA-ORIGINAL', 'priority': 1,
                    'disabled': False, 'vendor_active': True}
        initial = self._request(app, source['instance'], previous, {'product_code': 'QA-UPDATED', 'priority': 2})

        class LostCommitResponse:
            def __init__(self, **kwargs):
                self.connection = pymysql.connect(**kwargs)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                self.connection.commit()
                raise pymysql.err.OperationalError(2013, 'Private lab lost the reply after commit')

        with self.assertRaisesRegex(SupplierResultUnverified, 'COMMIT_UNCONFIRMED'):
            SupplierUpdateSql(config, source, app, connect=LostCommitResponse).apply(initial)
        applied = writer.apply(initial)
        confirmed = {**previous, **initial['changes']}
        self.assertEqual(('synchronized', confirmed), (applied['status'], applied['relation']))
        self.assertEqual(applied, writer.apply(initial))
        stale = self._request(app, source['instance'], previous, {'priority': 3})
        self.assertEqual('SGA_SUPPLIER_RELATION_STALE', writer.apply(stale)['code'])
        duplicate = self._request(app, source['instance'], confirmed, {'product_code': 'qa-duplicate'})
        self.assertEqual('SGA_SUPPLIER_PRODUCT_CODE_AMBIGUOUS', writer.apply(duplicate)['code'])
        alias = self._request(app, source['instance'], confirmed, {'product_code': 'QA-ALIAS'})
        self.assertEqual('SGA_SUPPLIER_PRODUCT_CODE_AMBIGUOUS', writer.apply(alias)['code'])
        modified = self._request(app, source['instance'], confirmed, {'priority': 9})
        modified['operation_id'] = initial['operation_id']
        self.assertEqual('SGA_SUPPLIER_OPERATION_CONTENT_CONFLICT', writer.apply(modified)['code'])
        with admin.cursor() as cursor:
            cursor.execute(f'SELECT CodigoArticulo,Prioridad,Usuario,Estacion FROM `{database}`.articulosproveedor '
                           'WHERE Proveedor=%s AND Articulo=%s', ('318', 498))
            self.assertEqual({'CodigoArticulo': 'QA-UPDATED', 'Prioridad': 2, 'Usuario': 'ODOO', 'Estacion': 4},
                             cursor.fetchone())
            cursor.execute(f'SELECT COUNT(*) AS total FROM `{integration}`.`{JOURNAL_TABLE}`')
            self.assertEqual(1, cursor.fetchone()['total'])
            self.assertEqual(quote_before, self._quote(cursor, database))
        print(f'SUPPLIER_UPDATE_LAB_PROOF source={database} journal={integration}')

    def _seed_schemas(self, cursor, database, integration, catalog):
        cursor.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8')
        cursor.execute(f'USE `{database}`')
        cursor.execute('SET FOREIGN_KEY_CHECKS=0')
        for table, where, params in (('proveedores', 'Proveedor=%s', ('318',)),
                                     ('artsservs', 'Articulo IN (%s,%s,%s)', (498, 3674, 5407)),
                                     ('articulosproveedor', 'Articulo IN (%s,%s,%s) AND Proveedor=%s', (498, 3674, 5407, '318'))):
            cursor.execute(f'SHOW CREATE TABLE `{catalog}`.`{table}`')
            cursor.execute(cursor.fetchone()['Create Table'].replace(f'`{catalog}`.', f'`{database}`.'))
            cursor.execute('SELECT COLUMN_NAME FROM information_schema.COLUMNS '
                           'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND DATA_TYPE=%s', (catalog, table, 'bit'))
            bits = {row['COLUMN_NAME'] for row in cursor.fetchall()}
            cursor.execute(f'SELECT * FROM `{catalog}`.`{table}` WHERE {where}', params)
            rows = cursor.fetchall()
            self.assertEqual(1 if table == 'proveedores' else 3, len(rows))
            if table == 'articulosproveedor':
                for row in rows:
                    row.update(CodigoArticulo={498: 'QA-ORIGINAL', 3674: 'QA-DUPLICATE', 5407: ' QA-ALIAS'}[row['Articulo']], Prioridad=1)
            columns = list(rows[0])
            cursor.executemany(f'INSERT INTO `{table}` ({self._columns(columns)}) '
                               f'VALUES ({",".join(["%s"] * len(columns))})',
                               [tuple(int.from_bytes(row[name], 'big') if name in bits and isinstance(row[name], bytes)
                                      else row[name] for name in columns) for row in rows])
        cursor.execute('SET FOREIGN_KEY_CHECKS=1')
        ddl = Path(__file__).resolve().parents[3] / 'deploy' / 'supplier-relation-application.sql'
        code = '\n'.join(line for line in ddl.read_text().splitlines() if not line.startswith('--'))
        for statement in code.replace('sga_supplier_integration', integration).split(';'):
            if statement.strip():
                cursor.execute(statement)

    @staticmethod
    def _columns(columns):
        return ','.join(f'`{name}`' for name in columns)

    @staticmethod
    def _quote(cursor, database):
        cursor.execute(f'SELECT PrecioCotiz,PrecioCotizDet,PrecioCotizDol,PrecioCotizDolDet '
                       f'FROM `{database}`.articulosproveedor WHERE Articulo=%s AND Proveedor=%s', (498, '318'))
        return cursor.fetchone()

    @staticmethod
    def _drop_user(admin, username):
        with admin.cursor() as cursor:
            cursor.execute('DROP USER %s@%s', (username, 'localhost'))

    @staticmethod
    def _request(app, source, relation, changes):
        return {'contract': 1, 'action': 'supplier.update', 'application_id': app, 'operation_id': str(uuid.uuid4()),
                'source_instance': source, 'relation': relation, 'changes': changes,
                'request_checksum': digest({'relation': relation, 'changes': changes})}
