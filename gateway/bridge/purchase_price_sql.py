"""Scoped transaction for an approved Normal retail price."""

import json
import re
from contextlib import closing
from decimal import ROUND_HALF_UP

import pymysql

from .catalog_source import bit
from .purchase_effects_contract import (
    COMMON, NativeResultUnverified, PurchaseEffectsError, reconcile_price, request,
)
from .purchase_order_sql import GRANT, RETRYABLE
from .purchase_price_source import (
    CENT, FIXED_MARGIN, FIXED_MARGIN_PROFILE, LEVEL, MAINTAIN_PROFILE, MAX_PRICE, READ_COLUMNS,
    describe_native_price, four, native_number, native_snapshot, price_query,
)


JOURNAL_TABLE = 'retail_price_application'
WRITE_COLUMNS = {'PrecioSugeridoDet', 'CambioPrecio', 'Usuario', 'Estacion', 'FechaActualizacion'}


def _status(operation, code):
    return {**{key: operation[key] for key in COMMON | {'request_checksum'}},
            'status': 'blocked', 'code': code}


def _positive_four(value):
    amount = native_number(value)
    return 0 < amount <= MAX_PRICE and amount == four(amount)


class PurchasePriceSql:
    def __init__(self, config, source, application_id, *, connect=pymysql.connect):
        self.config = dict(config)
        self.application_id = application_id
        self.source_instance = source.get('instance') or source.get('source_instance')
        self.database = source.get('database', 'sgadata')
        self.integration = config.get('integration_schema', '')
        self.port = config.get('port', 3306)
        self.profile = config.get('profile')
        self.connect = connect
        if (config.get('enabled') is not True or not self.source_instance
                or config.get('application_id') != application_id or config.get('source_instance') != self.source_instance
                or config.get('database') != self.database
                or not isinstance(self.database, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', self.database)
                or not isinstance(self.integration, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', self.integration)
                or self.integration == self.database or not config.get('host') or config['host'] != source.get('host')
                or type(self.port) is not int or not 1 <= self.port <= 65535 or self.port != int(source.get('port', 3306))
                or not config.get('user') or config['user'] == source.get('user') or not config.get('password')
                or not isinstance(config.get('audit_user'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,8}', config['audit_user'])
                or type(config.get('audit_station')) is not int or not 0 <= config['audit_station'] < 255
                or self.profile not in (MAINTAIN_PROFILE, FIXED_MARGIN_PROFILE)
                or not isinstance(config.get('evidence_reference'), str) or not config['evidence_reference'].strip()):
            raise PurchaseEffectsError('SGA_PRICE_WRITER_CONFIGURATION_INVALID')
        self.journal = f'`{self.integration}`.`{JOURNAL_TABLE}`'
        self.write_columns = WRITE_COLUMNS | ({'VariacionDet'} if self.profile == FIXED_MARGIN_PROFILE else set())

    def _assert_database(self, cursor):
        expected = {(self.database, table, 'SELECT'): {name.lower() for name in columns}
                    for table, columns in READ_COLUMNS.items()}
        expected[(self.database, 'artsservsprecios', 'UPDATE')] = {name.lower() for name in self.write_columns}
        for privilege in ('SELECT', 'INSERT'):
            expected[(self.integration, JOURNAL_TABLE, privilege)] = set()
        actual = {}
        cursor.execute('SHOW GRANTS FOR CURRENT_USER()')
        for row in cursor.fetchall():
            grant = str(next(iter(row.values())))
            match = re.match(r'^GRANT (.*?) ON ([^ ]+) TO ', grant)
            if not match or 'WITH GRANT OPTION' in grant.upper():
                raise PurchaseEffectsError('SGA_PRICE_WRITER_ACCOUNT_TOO_BROAD')
            privileges, resource = match.groups()
            if privileges == 'USAGE' and resource == '*.*':
                continue
            scope = resource.replace('`', '').split('.')
            items = list(GRANT.finditer(privileges))
            if len(scope) != 2 or ''.join(item[0] for item in items) != privileges:
                raise PurchaseEffectsError('SGA_PRICE_WRITER_ACCOUNT_TOO_BROAD')
            for item in items:
                privilege, raw = item.groups()
                key = (*scope, privilege)
                columns = {name.strip().strip('`').lower() for name in raw.split(',')} if raw else set()
                if key not in expected or bool(raw) != bool(expected[key]) or not columns <= expected[key]:
                    raise PurchaseEffectsError('SGA_PRICE_WRITER_ACCOUNT_TOO_BROAD')
                actual.setdefault(key, set()).update(columns)
        if actual != expected:
            raise PurchaseEffectsError('SGA_PRICE_WRITER_GRANTS_INVALID')
        cursor.execute('SELECT DATABASE() AS db,@@socket AS socket')
        database = cursor.fetchone()
        if (database['db'] != self.database or self.config.get('unix_socket')
                and database['socket'] != self.config['unix_socket']):
            raise PurchaseEffectsError('SGA_PRICE_WRITER_DATABASE_MISMATCH')
        cursor.execute('SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE FROM information_schema.TABLES '
                       'WHERE TABLE_SCHEMA IN (%s,%s)', (self.database, self.integration))
        engines = {(row['TABLE_SCHEMA'], row['TABLE_NAME']): row['ENGINE'] for row in cursor.fetchall()}
        required = {(self.database, name) for name in READ_COLUMNS} | {(self.integration, JOURNAL_TABLE)}
        if any(engines.get(key) != 'InnoDB' for key in required):
            raise PurchaseEffectsError('SGA_PRICE_WRITER_REQUIRES_INNODB')

    def apply(self, payload):
        operation = request(payload)
        if (operation['action'] != 'price.publish' or operation['application_id'] != self.application_id
                or operation['source_instance'] != self.source_instance):
            raise PurchaseEffectsError('SGA_PRICE_WRITER_SCOPE_MISMATCH')
        if operation['contract'] != 2:
            return _status(operation, 'BLOCKED_PURCHASE_FISCAL_V2_REQUIRED')
        price = operation['price']
        if 'expected_native_checksum' not in price:
            return _status(operation, 'SGA_PRICE_APPROVED_NATIVE_BASELINE_REQUIRED')
        if price['level'] != LEVEL or not _positive_four(price['unit_price_excluded_tax']):
            return _status(operation, 'SGA_PRICE_REQUEST_PROFILE_UNSUPPORTED')
        for _ in range(3):
            try:
                return self._apply(operation)
            except pymysql.err.OperationalError as exc:
                if exc.args[0] not in RETRYABLE:
                    raise
            except pymysql.err.IntegrityError as exc:
                if exc.args[0] != 1062:
                    raise
        # A second worker may have committed this UUID while ours retried.
        # The next lease must reconcile the same journal receipt.
        raise NativeResultUnverified('SGA_PRICE_WRITER_BUSY')

    def _locked_price(self, cursor, article):
        cursor.execute(price_query('%s') + ' FOR UPDATE', (article, LEVEL))
        rows = cursor.fetchall()
        if len(rows) > 1 or rows and rows[0]['Articulo'] != article:
            raise PurchaseEffectsError('SGA_PRICE_NATIVE_IDENTITY_AMBIGUOUS')
        return rows[0] if rows else None

    def _apply(self, operation):
        options = {name: self.config[name] for name in ('host', 'database', 'user', 'password')}
        with closing(self.connect(**options, port=self.port, unix_socket=self.config.get('unix_socket'),
                                  charset='utf8', cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                                  connect_timeout=3, read_timeout=6, write_timeout=5)) as connection:
            with connection.cursor() as cursor:
                self._assert_database(cursor)
                cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                connection.begin()
                return self._publish(connection, cursor, operation)

    def _publish(self, connection, cursor, operation):
        key = (operation['application_id'], operation['operation_id'])
        cursor.execute(f'SELECT request_hash,result FROM {self.journal} '
                       'WHERE application_uuid=%s AND operation_uuid=%s FOR UPDATE', key)
        prior = cursor.fetchone()
        if prior:
            if prior['request_hash'] != operation['request_checksum']:
                return _status(operation, 'SGA_PRICE_OPERATION_CONTENT_CONFLICT')
            try:
                result = json.loads(prior['result'])
            except (TypeError, ValueError):
                raise NativeResultUnverified('SGA_PRICE_NATIVE_JOURNAL_INVALID') from None
            return reconcile_price(operation, result)

        price = operation['price']
        article = price['article_id']
        before = self._locked_price(cursor, article)
        current = describe_native_price(self.source_instance, article, before, profile=self.profile)
        if current['native_checksum'] != price['expected_native_checksum']:
            return _status(operation, 'SGA_PRICE_SOURCE_CHANGED')
        if not current['publication_supported']:
            return _status(operation, current['publication_block_code'])
        if any(current[name] != price[name] for name in ('vat_rate', 'vat_code', 'currency', 'exchange_rate')):
            return _status(operation, 'SGA_PRICE_NATIVE_TAX_OR_CURRENCY_MISMATCH')
        target = native_number(price['unit_price_excluded_tax'])
        cost = native_number(before['CostoUnitDet'])
        fixed_margin = before['CalculoPrecio'] == FIXED_MARGIN
        if fixed_margin and target != target.quantize(CENT, rounding=ROUND_HALF_UP):
            return _status(operation, 'SGA_PRICE_NATIVE_ROUNDING_MISMATCH')
        if not fixed_margin and target < cost:
            return _status(operation, 'SGA_PRICE_BELOW_CURRENT_NATIVE_COST')
        new_native_price = (four(target * (1 + native_number(price['vat_rate']) / 100))
                            if bit(before['ConImptoDet']) else target)
        after = before
        expected_variation = None
        if fixed_margin and target != native_number(current['unit_price_excluded_tax']):
            expected_variation = target / cost - 1 - native_number(before['PorcGanancia'])
        if expected_variation is not None or new_native_price != four(native_number(before['PrecioSugeridoDet'])):
            variance_assignment = 'VariacionDet=%s,' if expected_variation is not None else ''
            values = (expected_variation,) if expected_variation is not None else ()
            cursor.execute(f'UPDATE artsservsprecios SET {variance_assignment}PrecioSugeridoDet=%s,CambioPrecio=TRUE,'
                           'Usuario=%s,Estacion=%s,FechaActualizacion=NOW() WHERE Articulo=%s AND Nivel=%s',
                           (*values, new_native_price, self.config['audit_user'], self.config['audit_station'], article, LEVEL))
            if cursor.rowcount != 1:
                raise PurchaseEffectsError('SGA_PRICE_NATIVE_WRITE_MISMATCH')
            after = self._locked_price(cursor, article)
        confirmed = describe_native_price(self.source_instance, article, after, profile=self.profile)
        unchanged = set(before) - {'PrecioSugeridoDet', 'FechaActualizacion', 'PriceRevision'}
        if expected_variation is not None:
            unchanged.remove('VariacionDet')
        if (not confirmed['publication_supported'] or not after
                or any(after[name] != before[name] for name in unchanged)
                or four(native_number(after['PrecioSugeridoDet'])) != new_native_price
                or native_number(confirmed['unit_price_excluded_tax']) != target):
            raise PurchaseEffectsError('SGA_PRICE_NATIVE_READBACK_MISMATCH')
        effective = {field: confirmed[field] for field in (
            'article_id', 'level', 'currency', 'exchange_rate', 'vat_code', 'vat_rate', 'unit_price_excluded_tax')}
        result = reconcile_price(operation, {**{key: operation[key] for key in COMMON | {'request_checksum'}},
            'status': 'synchronized', 'price_confirmed': True, 'effective_price': effective, 'native_price': confirmed})
        cursor.execute(f'INSERT INTO {self.journal} (application_uuid,operation_uuid,request_hash,article_id,level,'
                       'before_snapshot,after_snapshot,result,created_utc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())',
                       (*key, operation['request_checksum'], article, LEVEL,
                        json.dumps(native_snapshot(self.source_instance, before)),
                        json.dumps(native_snapshot(self.source_instance, after)), json.dumps(result)))
        try:
            connection.commit()
        except pymysql.MySQLError:
            raise NativeResultUnverified('SGA_PRICE_NATIVE_COMMIT_UNCONFIRMED') from None
        return result
