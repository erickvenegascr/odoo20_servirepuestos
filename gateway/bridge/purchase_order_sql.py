"""Transactional draft purchase-order writer for an approved SGA schema."""

import json
import re

import pymysql

from .catalog_source import bit
from .purchase_effects_contract import COMMON, MAX_ID, NativeResultUnknown, NativeResultUnverified, PurchaseEffectsError
from .purchase_order_contract import validate_order_request, validate_order_result
from .purchase_source import PurchaseSource


HEADER_COLUMNS = {'Pedido', 'Proveedor', 'FechaPedido', 'DiasEntrega', 'Usuario', 'Estacion', 'FechaActualizacion'}
LINE_COLUMNS = {'Pedido', 'PorMayor', 'Articulo', 'CantPedida', 'UltCostoCotizado', 'Usuario', 'Estacion',
                'FechaActualizacion'}
READ_TABLES = {'pgconsecutivos', 'pedidos', 'pedidosarticulos', 'proveedores', 'proveedoresimport',
               'artsservs', 'articulosproveedor'}
WRITE_COLUMNS = {
    ('INSERT', 'pedidos'): HEADER_COLUMNS,
    ('INSERT', 'pedidosarticulos'): LINE_COLUMNS,
    ('UPDATE', 'pgconsecutivos'): {'Pedido'},
    ('UPDATE', 'artsservs'): {'CantPedidaDet'},
    ('UPDATE', 'articulosproveedor'): {'CantPedidaDet'},
}
GRANT = re.compile(r'(SELECT|INSERT|UPDATE)(?: \(([^)]*)\))?(?:, |$)')
RETRYABLE = {1205, 1213}


class NativeOrderNumberConflict(RuntimeError):
    def __init__(self, number):
        self.number = number
        super().__init__(number)


class PurchaseOrderSql:
    def __init__(self, config, source_config, application_id, *, connect=pymysql.connect):
        self.config = dict(config)
        self.source_config = dict(source_config)
        self.application_id = application_id
        self.source_instance = source_config.get('instance') or source_config.get('source_instance')
        self.database = source_config.get('database', 'sgadata')
        self.integration = config.get('integration_schema', '')
        if (not self.source_instance or config.get('source_instance') != self.source_instance
                or config.get('application_id') != application_id
                or config.get('database') != self.database or config.get('host') != source_config.get('host')
                or int(config.get('port', 3306)) != int(source_config.get('port', 3306))
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', self.integration)
                or self.integration == self.database or config.get('user') == source_config.get('user')
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,8}', config.get('audit_user', ''))
                or type(config.get('audit_station')) is not int or not 1 <= config['audit_station'] <= 32767):
            raise PurchaseEffectsError('PURCHASE_ORDER_WRITER_CONFIGURATION_INVALID')
        self.connect = connect
        self.receipts = f'`{self.integration}`.purchase_order_receipt'

    def _assert_grants(self, cursor):
        cursor.execute('SHOW GRANTS FOR CURRENT_USER()')
        has_receipt_access = set()
        for row in cursor.fetchall():
            value = str(next(iter(row.values())))
            match = re.match(r'^GRANT (.*?) ON ([^ ]+) TO ', value)
            if not match or 'WITH GRANT OPTION' in value.upper():
                raise PurchaseEffectsError('PURCHASE_ORDER_WRITER_ACCOUNT_TOO_BROAD')
            privileges, resource = match.groups()
            if privileges == 'USAGE' and resource == '*.*':
                continue
            parts = resource.replace('`', '').split('.')
            parsed = list(GRANT.finditer(privileges))
            if len(parts) != 2 or ''.join(item[0] for item in parsed) != privileges:
                raise PurchaseEffectsError('PURCHASE_ORDER_WRITER_ACCOUNT_TOO_BROAD')
            schema, table = parts
            for item in parsed:
                privilege, raw_columns = item.groups()
                if schema == self.integration and table == 'purchase_order_receipt' and not raw_columns:
                    has_receipt_access.add(privilege)
                    continue
                columns = {part.strip().strip('`') for part in raw_columns.split(',')} if raw_columns else set()
                if (schema != self.database or table not in READ_TABLES
                        or privilege != 'SELECT' and (not columns or not columns <= WRITE_COLUMNS.get((privilege, table), set()))):
                    raise PurchaseEffectsError('PURCHASE_ORDER_WRITER_ACCOUNT_TOO_BROAD')
        if has_receipt_access != {'SELECT', 'INSERT'}:
            raise PurchaseEffectsError('PURCHASE_ORDER_RECEIPT_GRANTS_INVALID')
        cursor.execute('SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE FROM information_schema.TABLES '
                       'WHERE TABLE_SCHEMA IN (%s,%s)', (self.database, self.integration))
        engines = {(row['TABLE_SCHEMA'], row['TABLE_NAME']): row['ENGINE'] for row in cursor.fetchall()}
        required = {(self.database, table) for table in READ_TABLES} | {(self.integration, 'purchase_order_receipt')}
        if any(engines.get(table) != 'InnoDB' for table in required):
            raise PurchaseEffectsError('PURCHASE_ORDER_WRITER_REQUIRES_INNODB')

    def apply(self, payload):
        request = validate_order_request(payload)
        if request['application_id'] != self.application_id or request['source_instance'] != self.source_instance:
            raise PurchaseEffectsError('PURCHASE_ORDER_WRITER_SCOPE_MISMATCH')
        conflict_number = None
        for attempt in range(3):
            try:
                return self._apply(request, conflict_number)
            except NativeOrderNumberConflict as exc:
                conflict_number = exc.number
                if attempt == 2:
                    return self._number_conflict(request)
            except pymysql.err.OperationalError as exc:
                if exc.args[0] not in RETRYABLE:
                    raise
                if attempt == 2:
                    return self._rejection(request, exc_code='SGA_PURCHASE_ORDER_BUSY', status='blocked')

    def _rejection(self, request, *, exc_code, status='rejected'):
        return {key: request[key] for key in COMMON | {'request_checksum'}} | {'status': status, 'code': exc_code}

    def _number_conflict(self, request):
        return self._rejection(request, exc_code='SGA_PURCHASE_ORDER_NUMBER_CONFLICT', status='blocked')

    def _apply(self, request, conflict_number=None):
        connection = self.connect(
            host=self.config['host'], port=int(self.config.get('port', 3306)),
            user=self.config['user'], password=self.config['password'], database=self.database,
            unix_socket=self.config.get('unix_socket'), charset='latin1', cursorclass=pymysql.cursors.DictCursor,
            autocommit=False, connect_timeout=3, read_timeout=8, write_timeout=8,
        )
        committing = False
        try:
            with connection.cursor() as cursor:
                self._assert_grants(cursor)
                cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
                connection.begin()
                cursor.execute('SELECT Pedido FROM pgconsecutivos WHERE Llave=0 FOR UPDATE')
                counter = cursor.fetchone()
                if counter is None or not 0 <= counter['Pedido'] < MAX_ID:
                    raise PurchaseEffectsError('SGA_PURCHASE_ORDER_COUNTER_UNAVAILABLE')
                identity = (request['application_id'], request['operation_id'])
                cursor.execute(f'SELECT request_hash,result FROM {self.receipts} '
                               'WHERE application_uuid=%s AND order_uuid=%s', identity)
                prior = cursor.fetchone()
                if prior:
                    if prior['request_hash'] != request['request_checksum']:
                        raise PurchaseEffectsError('SGA_PURCHASE_ORDER_IDENTITY_CONFLICT')
                    return validate_order_result(json.loads(prior['result']), request)
                before = counter['Pedido']
                cursor.execute('SELECT Pedido FROM pedidos ORDER BY Pedido DESC LIMIT 1')
                last = cursor.fetchone()
                out_of_sync = last is not None and last['Pedido'] > before
                # A native client may have reserved the number without taking
                # our counter lock. After rollback only trust its committed counter.
                if conflict_number is not None and (before < conflict_number or out_of_sync):
                    return self._number_conflict(request)
                if out_of_sync:
                    raise PurchaseEffectsError('SGA_PURCHASE_ORDER_COUNTER_OUT_OF_SYNC')
                number = before + 1
                result = self._save(connection, cursor, request, number)
                serialized = json.dumps(result, ensure_ascii=True)
                if len(serialized.encode('ascii')) > 240000:
                    raise PurchaseEffectsError('SGA_PURCHASE_ORDER_RESULT_TOO_LARGE')
                cursor.execute('UPDATE pgconsecutivos SET Pedido=%s WHERE Llave=0 AND Pedido=%s', (number, before))
                if cursor.rowcount != 1:
                    raise PurchaseEffectsError('SGA_PURCHASE_ORDER_COUNTER_CHANGED')
                cursor.execute(f'INSERT INTO {self.receipts} '
                               '(application_uuid,order_uuid,request_hash,pedido,result,created_utc) '
                               'VALUES (%s,%s,%s,%s,%s,UTC_TIMESTAMP())',
                               (*identity, request['request_checksum'], number, serialized))
                committing = True
                try:
                    connection.commit()
                except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
                    raise NativeResultUnknown('Reconcile the same purchase order UUID') from None
                return result
        except NativeResultUnverified:
            raise
        except PurchaseEffectsError as exc:
            return self._rejection(request, exc_code=exc.code)
        finally:
            try:
                if not committing:
                    try:
                        connection.rollback()
                    except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
                        pass
            finally:
                connection.close()

    def _save(self, connection, cursor, request, number):
        order = request['order']
        vendor = order['vendor_code']
        actor = self.config['audit_user'], self.config['audit_station']
        try:
            cursor.execute('INSERT INTO pedidos '
                           '(Pedido,Proveedor,FechaPedido,DiasEntrega,Usuario,Estacion,FechaActualizacion) '
                           'VALUES (%s,%s,%s,%s,%s,%s,NOW())',
                           (number, vendor, order['date_order'], order['delivery_days'], *actor))
        except pymysql.err.IntegrityError as exc:
            if exc.args[0] != 1062:
                raise
            # The approved native schema has only Pedido's PK on this table.
            # Leave all other writes and collision types to their own handling.
            raise NativeOrderNumberConflict(number) from None
        for line in order['lines']:
            article, quantity = line['article_id'], line['quantity']
            cursor.execute('SELECT a.Trabajado,a.Servicio,v.Deshabilitado FROM artsservs a '
                           'JOIN articulosproveedor v ON v.Articulo=a.Articulo '
                           'WHERE a.Articulo=%s AND v.Proveedor=%s FOR UPDATE', (article, vendor))
            row = cursor.fetchone()
            if not row or not row['Trabajado'] or bit(row['Servicio']) or bit(row['Deshabilitado']):
                raise PurchaseEffectsError('SGA_PURCHASE_ORDER_ARTICLE_VENDOR_UNAVAILABLE')
            cursor.execute('INSERT INTO pedidosarticulos '
                           '(Pedido,PorMayor,Articulo,CantPedida,UltCostoCotizado,Usuario,Estacion,FechaActualizacion) '
                           'VALUES (%s,0,%s,%s,%s,%s,%s,NOW())', (number, article, quantity, line['unit_cost'], *actor))
            cursor.execute('UPDATE artsservs SET CantPedidaDet=CantPedidaDet+%s WHERE Articulo=%s',
                           (quantity, article))
            if cursor.rowcount != 1:
                raise PurchaseEffectsError('SGA_PURCHASE_ORDER_ARTICLE_PENDING_NOT_UPDATED')
            cursor.execute('UPDATE articulosproveedor SET CantPedidaDet=CantPedidaDet+%s '
                           'WHERE Articulo=%s AND Proveedor=%s', (quantity, article, vendor))
            if cursor.rowcount != 1:
                raise PurchaseEffectsError('SGA_PURCHASE_ORDER_VENDOR_PENDING_NOT_UPDATED')
        reader = PurchaseSource(self.source_config)
        reader.connection = connection
        readback = reader.search(vendor, order_id=number, include_closed=True, limit=1)
        if readback['status'] != 'found' or len(readback['orders']) != 1:
            raise PurchaseEffectsError('SGA_PURCHASE_ORDER_READBACK_MISSING')
        result = {key: request[key] for key in COMMON | {'request_checksum'}}
        result.update(status='synchronized', order_confirmed=True, order_uuid=request['operation_id'],
                      order=readback['orders'][0])
        return validate_order_result(result, request)
