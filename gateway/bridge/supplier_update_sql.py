"""Atomic SGA supplier updates with independent column privileges and a native receipt."""

import json
import re
from contextlib import closing

import pymysql

from .catalog_source import bit, text
from .supplier_update_contract import (
    SupplierResultUnverified, SupplierUpdateError, canonical_uuid, update_result, valid_relation,
    validate_update_request, verify_native_result,
)

JOURNAL_TABLE = 'supplier_relation_application'
READ_COLUMNS = {
    'articulosproveedor': {'Articulo', 'Proveedor', 'CodigoArticulo', 'Prioridad', 'Deshabilitado'},
    'proveedores': {'Proveedor', 'Activo'},
    'artsservs': {'Articulo', 'Trabajado'},
}
WRITE_COLUMNS = {'CodigoArticulo', 'Prioridad', 'Usuario', 'Estacion', 'FechaActualizacion'}
GRANT_RE = re.compile(r'(SELECT|INSERT|UPDATE)(?: \(([^)]*)\))?(?:, |$)')
RETRYABLE = {1205, 1213}
MAX_VENDOR_ALIAS_ROWS = 20000
NATIVE_COLUMNS = {'product_code': 'CodigoArticulo', 'priority': 'Prioridad'}


def _unavailable(code):
    return SupplierUpdateError(code, 'unavailable')


def _conflict(code):
    return SupplierUpdateError(code, 'conflict')


class SupplierUpdateSql:
    def __init__(self, config, source, application_id, *, connect=pymysql.connect):
        self.config = dict(config)
        self.connect = connect
        self.application_id = application_id
        self.source_instance = source.get('instance') or source.get('source_instance')
        self.database = source.get('database', 'sgadata')
        self.integration = config.get('integration_schema', '')
        self.port = config.get('port', 3306)
        if (config.get('enabled') is not True or not canonical_uuid(application_id)
                or config.get('application_id') != application_id
                or not isinstance(self.source_instance, str) or not 1 <= len(self.source_instance) <= 80
                or config.get('source_instance') != self.source_instance or config.get('database') != self.database
                or not isinstance(self.database, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', self.database)
                or not isinstance(self.integration, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', self.integration)
                or self.integration == self.database or not config.get('host') or config['host'] != source.get('host')
                or type(self.port) is not int or not 1 <= self.port <= 65535 or self.port != int(source.get('port', 3306))
                or not config.get('user') or config['user'] == source.get('user') or not config.get('password')
                or not isinstance(config.get('audit_user'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,8}', config['audit_user'])
                or type(config.get('audit_station')) is not int or not 0 <= config['audit_station'] < 255):
            raise _unavailable('SGA_SUPPLIER_WRITER_CONFIGURATION_INVALID')
        self.journal = f'`{self.integration}`.`{JOURNAL_TABLE}`'

    def _assert_database(self, cursor):
        expected = {(self.database, table, 'SELECT'): {name.lower() for name in columns}
                    for table, columns in READ_COLUMNS.items()}
        expected[(self.database, 'articulosproveedor', 'UPDATE')] = {name.lower() for name in WRITE_COLUMNS}
        for privilege in ('SELECT', 'INSERT'):
            expected[(self.integration, JOURNAL_TABLE, privilege)] = set()
        parsed = {}
        cursor.execute('SHOW GRANTS FOR CURRENT_USER()')
        for row in cursor.fetchall():
            value = str(next(iter(row.values())))
            match = re.match(r'^GRANT (.*?) ON ([^ ]+) TO ', value)
            if not match or 'WITH GRANT OPTION' in value.upper():
                raise _unavailable('SGA_SUPPLIER_WRITER_ACCOUNT_TOO_BROAD')
            privileges, resource = match.groups()
            if privileges == 'USAGE' and resource == '*.*':
                continue
            scope = resource.replace('`', '').split('.')
            matches = list(GRANT_RE.finditer(privileges))
            if len(scope) != 2 or ''.join(item[0] for item in matches) != privileges:
                raise _unavailable('SGA_SUPPLIER_WRITER_ACCOUNT_TOO_BROAD')
            for item in matches:
                privilege, raw = item.groups()
                key = (*scope, privilege)
                columns = {part.strip().strip('`').lower() for part in raw.split(',')} if raw else set()
                if key not in expected or bool(raw) != bool(expected[key]) or not columns <= expected[key]:
                    raise _unavailable('SGA_SUPPLIER_WRITER_ACCOUNT_TOO_BROAD')
                parsed.setdefault(key, set()).update(columns)
        if parsed != expected:
            raise _unavailable('SGA_SUPPLIER_WRITER_GRANTS_INVALID')
        cursor.execute('SELECT DATABASE() AS db,@@socket AS socket')
        identity = cursor.fetchone()
        if identity['db'] != self.database or (self.config.get('unix_socket')
                                               and identity['socket'] != self.config['unix_socket']):
            raise _unavailable('SGA_SUPPLIER_WRITER_DATABASE_MISMATCH')
        cursor.execute('SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE FROM information_schema.TABLES '
                       'WHERE TABLE_SCHEMA IN (%s,%s)', (self.database, self.integration))
        engines = {(row['TABLE_SCHEMA'], row['TABLE_NAME']): row['ENGINE'] for row in cursor.fetchall()}
        required = {(self.database, table) for table in READ_COLUMNS} | {(self.integration, JOURNAL_TABLE)}
        if any(engines.get(table) != 'InnoDB' for table in required):
            raise _unavailable('SGA_SUPPLIER_WRITER_REQUIRES_INNODB')
        cursor.execute("SHOW VARIABLES LIKE 'innodb_locks_unsafe_for_binlog'")
        lock_mode = cursor.fetchone()
        if lock_mode and lock_mode['Value'] != 'OFF':
            raise _unavailable('SGA_SUPPLIER_WRITER_REQUIRES_NEXT_KEY_LOCKS')

    def apply(self, payload):
        if (not isinstance(payload, dict) or payload.get('application_id') != self.application_id
                or payload.get('source_instance') != self.source_instance):
            raise SupplierUpdateError('SGA_SUPPLIER_WRITER_SCOPE_MISMATCH')
        try:
            operation = validate_update_request(payload)
        except SupplierUpdateError as exc:
            return update_result(payload, exc)
        for _ in range(3):
            try:
                return self._apply(operation)
            except SupplierUpdateError as exc:
                return update_result(operation, exc)
            except pymysql.err.OperationalError as exc:
                if exc.args[0] not in RETRYABLE:
                    raise
            except pymysql.err.IntegrityError as exc:
                if exc.args[0] != 1062:
                    raise
        return update_result(operation, _unavailable('SGA_SUPPLIER_WRITER_BUSY'))

    def _apply(self, operation):
        options = {name: self.config[name] for name in ('host', 'database', 'user', 'password')}
        if self.config.get('unix_socket'):
            options['unix_socket'] = self.config['unix_socket']
        with closing(self.connect(**options, port=self.port, charset='utf8', cursorclass=pymysql.cursors.DictCursor,
                                  autocommit=False, connect_timeout=3, read_timeout=6, write_timeout=5)) as connection:
            with connection.cursor() as cursor:
                self._assert_database(cursor)
                cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                connection.begin()
                key = (operation['application_id'], operation['operation_id'])
                cursor.execute(f'SELECT request_hash,result FROM {self.journal} '
                               'WHERE application_uuid=%s AND operation_uuid=%s FOR UPDATE', key)
                previous = cursor.fetchone()
                if previous:
                    if previous['request_hash'] != operation['request_checksum']:
                        raise _conflict('SGA_SUPPLIER_OPERATION_CONTENT_CONFLICT')
                    try:
                        stored = json.loads(previous['result'])
                    except (ValueError, TypeError):
                        raise SupplierResultUnverified('SGA_SUPPLIER_UPDATE_NATIVE_RESULT_INVALID') from None
                    return verify_native_result(stored, operation)
                expected = operation['relation']
                native_key = (expected['article_id'], expected['vendor_code'])
                actual = self._current(cursor, native_key)
                if actual['disabled'] or not actual['vendor_active']:
                    raise _conflict('SGA_SUPPLIER_RELATION_INACTIVE')
                if actual != expected:
                    raise _conflict('SGA_SUPPLIER_RELATION_STALE')
                target = {**expected, **operation['changes']}
                self._assert_unambiguous(cursor, native_key, actual['product_code'], target['product_code'])
                self._save(cursor, native_key, operation['changes'])
                confirmed = self._current(cursor, native_key)
                if confirmed != target:
                    raise _conflict('SGA_SUPPLIER_RELATION_READBACK_MISMATCH')
                result = update_result(operation, relation=confirmed)
                cursor.execute(f'INSERT INTO {self.journal} '
                               '(application_uuid,operation_uuid,request_hash,article_id,vendor_code,result,created_utc) '
                               'VALUES (%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())',
                               (*key, operation['request_checksum'], *native_key, json.dumps(result, ensure_ascii=True)))
                try:
                    connection.commit()
                except pymysql.MySQLError:
                    raise SupplierResultUnverified('SGA_SUPPLIER_UPDATE_COMMIT_UNCONFIRMED') from None
                return result

    @staticmethod
    def _current(cursor, native_key):
        cursor.execute('SELECT r.Articulo,r.Proveedor,r.CodigoArticulo,r.Prioridad,r.Deshabilitado,p.Activo,a.Trabajado '
                       'FROM articulosproveedor r JOIN proveedores p ON p.Proveedor=r.Proveedor '
                       'JOIN artsservs a ON a.Articulo=r.Articulo '
                       'WHERE r.Articulo=%s AND r.Proveedor=%s FOR UPDATE', native_key)
        row = cursor.fetchone()
        if not row or (row['Articulo'], row['Proveedor']) != native_key:
            raise _conflict('SGA_SUPPLIER_RELATION_NOT_FOUND')
        if row['Trabajado'] != 255:
            raise _conflict('SGA_SUPPLIER_ARTICLE_INACTIVE')
        raw_code = (row['CodigoArticulo'] or '').strip()
        if text(raw_code) != raw_code:
            raise _conflict('SGA_SUPPLIER_RELATION_ENCODING_UNCERTAIN')
        actual = {'article_id': row['Articulo'], 'vendor_code': row['Proveedor'], 'product_code': raw_code,
                  'priority': row['Prioridad'], 'disabled': bit(row['Deshabilitado']),
                  'vendor_active': bit(row['Activo'])}
        if not valid_relation(actual):
            raise _conflict('SGA_SUPPLIER_RELATION_NATIVE_VALUES_INVALID')
        return actual

    @staticmethod
    def _assert_unambiguous(cursor, native_key, before, after):
        article, vendor = native_key
        codes = {before, after} - {''}
        if not codes:
            return
        # The native collation may merge spellings that are distinct in Odoo.
        for code in sorted(codes):
            cursor.execute('SELECT Articulo FROM articulosproveedor FORCE INDEX (`Codigo`) '
                           'WHERE Proveedor=%s AND CodigoArticulo=%s LIMIT 2 FOR UPDATE', (vendor, code))
            if any(row['Articulo'] != article for row in cursor.fetchall()):
                raise _conflict('SGA_SUPPLIER_PRODUCT_CODE_AMBIGUOUS')
        # The supplier reader decodes the connection's Latin-1 bytes and
        # applies text(), so whitespace and legacy encodings can also alias.
        # This covering index range holds concurrent same-vendor edits until
        # the transaction ends, including insertions of previously absent codes.
        cursor.execute('SELECT Articulo,CAST(CONVERT(CodigoArticulo USING latin1) AS BINARY) AS SnapshotCode '
                       'FROM articulosproveedor FORCE INDEX (`Codigo`) '
                       'WHERE Proveedor=%s AND CodigoArticulo IS NOT NULL LIMIT %s LOCK IN SHARE MODE',
                       (vendor, MAX_VENDOR_ALIAS_ROWS + 1))
        rows = cursor.fetchall()
        if len(rows) > MAX_VENDOR_ALIAS_ROWS:
            raise _unavailable('SGA_SUPPLIER_ALIAS_CHECK_TOO_BROAD')
        for row in rows:
            if row['Articulo'] != article and text(row['SnapshotCode'].decode('latin-1')) in codes:
                raise _conflict('SGA_SUPPLIER_PRODUCT_CODE_AMBIGUOUS')

    def _save(self, cursor, native_key, changes):
        fields = sorted(changes)
        assignments = ','.join(f'{NATIVE_COLUMNS[field]}=%s' for field in fields)
        cursor.execute(f'UPDATE articulosproveedor SET {assignments},Usuario=%s,Estacion=%s,FechaActualizacion=NOW() '
                       'WHERE Articulo=%s AND Proveedor=%s',
                       (*(changes[field] for field in fields), self.config['audit_user'],
                        self.config['audit_station'], *native_key))
        if cursor.rowcount != 1:
            raise _conflict('SGA_SUPPLIER_RELATION_WRITE_MISMATCH')
