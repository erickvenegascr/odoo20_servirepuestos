"""Run the price writer with exact native DDL in a private MariaDB 5.5 lab."""

import os
import secrets
import unittest
import uuid
from pathlib import Path

import pymysql

from bridge.purchase_effects_contract import NativeResultUnverified, digest
from bridge.purchase_price_source import FIXED_MARGIN_PROFILE, MAINTAIN_PROFILE, describe_native_price, price_query
from bridge.purchase_price_sql import JOURNAL_TABLE, READ_COLUMNS, WRITE_COLUMNS, PurchasePriceSql


SOCKET = os.environ.get('SGA_RETAIL_PRICE_LAB_SOCKET')
ARTICLE = 1129


def columns(values):
    return ','.join(f'`{name}`' for name in sorted(values))


@unittest.skipUnless(SOCKET, 'A private SGA MariaDB socket was not configured')
class PurchasePriceSqlLabTests(unittest.TestCase):
    def test_normal_retail_native_write_controls_and_replay(self):
        suffix = uuid.uuid4().hex[:8]
        source_db = f'retail_source_lab_{suffix}'
        journal_db = f'retail_journal_lab_{suffix}'
        username = f'rp_lab_{suffix}'
        password = secrets.token_hex(24)
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
            self._seed(cursor, source_db, journal_db)
            cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s', (username, 'localhost', password))
            self.addCleanup(self._drop_user, admin, username)
            for table, allowed in READ_COLUMNS.items():
                cursor.execute(f'GRANT SELECT ({columns(allowed)}) ON `{source_db}`.`{table}` TO %s@%s',
                               (username, 'localhost'))
            cursor.execute(f'GRANT UPDATE ({columns(WRITE_COLUMNS)}) ON `{source_db}`.artsservsprecios TO %s@%s',
                           (username, 'localhost'))
            cursor.execute(f'GRANT SELECT,INSERT ON `{journal_db}`.`{JOURNAL_TABLE}` TO %s@%s',
                           (username, 'localhost'))
            before = self._read(cursor)
            cursor.execute('SELECT * FROM artsservs WHERE Articulo=%s', (ARTICLE,))
            unchanged_stock = cursor.fetchone()

        app = str(uuid.uuid4())
        source = {'instance': 'private-price-lab', 'database': source_db, 'host': 'localhost', 'user': 'reader'}
        config = {**source, 'application_id': app, 'source_instance': source['instance'], 'enabled': True,
                  'integration_schema': journal_db, 'user': username, 'password': password,
                  'unix_socket': str(socket), 'audit_user': 'ODOOPR', 'audit_station': 4,
                  'profile': MAINTAIN_PROFILE, 'evidence_reference': 'Private MariaDB price transaction fixture'}
        writer = PurchasePriceSql(config, source, app)
        baseline = describe_native_price(source['instance'], ARTICLE, before, profile=MAINTAIN_PROFILE)
        self.assertTrue(baseline['publication_supported'], baseline)
        initial = self._approval(app, source['instance'], baseline['native_checksum'], 8134.3456)

        class LostCommitResponse:
            def __init__(self, **kwargs):
                self.connection = pymysql.connect(**kwargs)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                self.connection.commit()
                raise pymysql.err.OperationalError(2013, 'Private lab lost the reply after commit')

        with self.assertRaisesRegex(NativeResultUnverified, 'COMMIT_UNCONFIRMED'):
            PurchasePriceSql(config, source, app, connect=LostCommitResponse).apply(initial)
        applied = writer.apply(initial)
        self.assertEqual(('synchronized', 8134.3456),
                         (applied['status'], applied['native_price']['unit_price_excluded_tax']))
        self.assertEqual(applied, writer.apply(initial))
        modified = self._approval(app, source['instance'], baseline['native_checksum'], 8123)
        self.assertEqual('SGA_PRICE_SOURCE_CHANGED', writer.apply(modified)['code'])
        modified['operation_id'] = initial['operation_id']
        self.assertEqual('SGA_PRICE_OPERATION_CONTENT_CONFLICT', writer.apply(modified)['code'])
        below_cost = self._approval(app, source['instance'], applied['native_price']['native_checksum'], 5000)
        self.assertEqual('SGA_PRICE_BELOW_CURRENT_NATIVE_COST', writer.apply(below_cost)['code'])

        with admin.cursor() as cursor:
            cursor.execute('SELECT PrecioSugeridoDet,PrecioSugerido,Usuario,Estacion,ConImptoDet,CambioPrecio '
                           'FROM artsservsprecios WHERE Articulo=%s AND Nivel=1', (ARTICLE,))
            self.assertEqual({'PrecioSugeridoDet': 9191.8105, 'PrecioSugerido': before['PrecioSugerido'],
                              'Usuario': 'ODOOPR', 'Estacion': 4, 'ConImptoDet': b'\1', 'CambioPrecio': b'\1'},
                             cursor.fetchone())
            cursor.execute('SELECT * FROM artsservs WHERE Articulo=%s', (ARTICLE,))
            self.assertEqual(unchanged_stock, cursor.fetchone())
            cursor.execute(f'SELECT COUNT(*) AS total FROM `{journal_db}`.`{JOURNAL_TABLE}`')
            self.assertEqual(1, cursor.fetchone()['total'])
            cursor.execute('UPDATE artsservsprecios SET CalculoPrecio=0 WHERE Articulo=%s AND Nivel=1', (ARTICLE,))
            markup = describe_native_price(source['instance'], ARTICLE, self._read(cursor), profile=MAINTAIN_PROFILE)
        unsupported = self._approval(app, source['instance'], markup['native_checksum'], 8200)
        self.assertFalse(markup['publication_supported'])
        self.assertEqual(markup['publication_block_code'], writer.apply(unsupported)['code'])

        with admin.cursor() as cursor:
            cursor.execute(f'GRANT UPDATE (VariacionDet) ON `{source_db}`.artsservsprecios TO %s@%s',
                           (username, 'localhost'))
            cursor.execute('UPDATE artsservsprecios SET PorcGanancia=.47,VariacionDet=.03,PrecioSugeridoDet=8000 '
                           'WHERE Articulo=%s AND Nivel=1', (ARTICLE,))
            fixed_before = self._read(cursor)
        fixed = PurchasePriceSql({**config, 'profile': FIXED_MARGIN_PROFILE}, source, app)
        fixed_current = describe_native_price(source['instance'], ARTICLE, fixed_before, profile=FIXED_MARGIN_PROFILE)
        self.assertEqual((10650.00, True), (fixed_current['unit_price_excluded_tax'],
                                          fixed_current['publication_supported']))
        fixed_approval = self._approval(app, source['instance'], fixed_current['native_checksum'], 10543.12)
        fixed_applied = fixed.apply(fixed_approval)
        self.assertEqual(('synchronized', 10543.12),
                         (fixed_applied['status'], fixed_applied['effective_price']['unit_price_excluded_tax']))
        self.assertEqual(fixed_applied, fixed.apply(fixed_approval))
        with admin.cursor() as cursor:
            fixed_after = self._read(cursor)
            self.assertEqual((0, fixed_before['PorcGanancia'], fixed_before['PrecioSugerido'], 11913.7256), (
                fixed_after['CalculoPrecio'], fixed_after['PorcGanancia'], fixed_after['PrecioSugerido'],
                fixed_after['PrecioSugeridoDet']))
            self.assertNotEqual(fixed_before['VariacionDet'], fixed_after['VariacionDet'])
            cursor.execute('SELECT * FROM artsservs WHERE Articulo=%s', (ARTICLE,))
            self.assertEqual(unchanged_stock, cursor.fetchone())
            cursor.execute(f'SELECT COUNT(*) AS total FROM `{journal_db}`.`{JOURNAL_TABLE}`')
            self.assertEqual(2, cursor.fetchone()['total'])
        print(f'RETAIL_PRICE_LAB_PROOF source={source_db} journal={journal_db} maintained=8134.3456 fixed=10543.12')

    def _seed(self, cursor, source_db, journal_db):
        cursor.execute(f'CREATE DATABASE `{source_db}` CHARACTER SET utf8')
        cursor.execute(f'USE `{source_db}`')
        cursor.execute('SET FOREIGN_KEY_CHECKS=0')
        for table, where, args in (('artsservs', 'Articulo=%s', (ARTICLE,)),
                                  ('preciosniveles', 'Nivel=1', ()),
                                  ('artsservsprecios', 'Articulo=%s AND Nivel=1', (ARTICLE,))):
            cursor.execute(f'SHOW CREATE TABLE sgadata_lab.`{table}`')
            cursor.execute(cursor.fetchone()['Create Table'].replace('`sgadata_lab`.', f'`{source_db}`.'))
            cursor.execute('SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s '
                           'AND TABLE_NAME=%s AND DATA_TYPE=%s', ('sgadata_lab', table, 'bit'))
            bits = {row['COLUMN_NAME'] for row in cursor.fetchall()}
            cursor.execute(f'SELECT * FROM sgadata_lab.`{table}` WHERE {where}', args)
            rows = cursor.fetchall()
            self.assertEqual(1, len(rows))
            row = rows[0]
            row = {name: int.from_bytes(value, 'big') if name in bits and isinstance(value, bytes) else value
                   for name, value in row.items()}
            names = sorted(row)
            cursor.execute(f'INSERT INTO `{table}` ({columns(names)}) VALUES ({",".join(["%s"] * len(names))})',
                           [row[name] for name in names])
        cursor.execute('SET FOREIGN_KEY_CHECKS=1')
        cursor.execute('UPDATE artsservs SET CostoUnitDet=7100,TipoIVA=%s,IVA=.13,Exento=FALSE,Trabajado=255,'
                       'Retenido=FALSE,MateriaPrima=FALSE,Servicio=FALSE,NumeroUnidades=1 WHERE Articulo=%s', ('08', ARTICLE))
        cursor.execute('UPDATE artsservsprecios SET CalculoPrecio=1,ConImptoDet=TRUE,PrecioSugeridoDet=9039.9999,'
                       'NoVender=FALSE,MargenMinDet=0,FechaTopePrecio=NULL,PorcDesctoPromo=0,AgotaExistPromo=FALSE '
                       'WHERE Articulo=%s AND Nivel=1', (ARTICLE,))
        ddl = Path(__file__).resolve().parents[3] / 'deploy' / 'retail-price-application.sql'
        for statement in ddl.read_text().replace('sga_retail_price_integration', journal_db).split(';'):
            if statement.strip():
                cursor.execute(statement)

    @staticmethod
    def _read(cursor):
        cursor.execute(price_query('%s'), (ARTICLE, 1))
        return cursor.fetchone()

    @staticmethod
    def _drop_user(admin, username):
        with admin.cursor() as cursor:
            cursor.execute('DROP USER %s@%s', (username, 'localhost'))

    @staticmethod
    def _approval(app, source, checksum, target):
        body = {'article_id': ARTICLE, 'level': 1, 'currency': 'CRC', 'exchange_rate': 1,
                'vat_code': '08', 'vat_rate': 13, 'unit_price_excluded_tax': target, 'document_id': 481,
                'expected_native_checksum': checksum}
        return {'contract': 2, 'action': 'price.publish', 'application_id': app, 'source_instance': source,
                'operation_id': str(uuid.uuid4()), 'request_checksum': digest(body), 'price': body}
