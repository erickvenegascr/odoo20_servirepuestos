"""Transactional receipt and payable writer for the domestic IVA credit profile."""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pymysql

from .purchase_credit_receipt import PurchaseCreditReceiptSource, _regular_credit
from .purchase_effects_contract import (
    COMMON, MAX_ID, NativeResultUnknown, NativeResultUnverified, PurchaseEffectsError,
    digest, reconcile_receive, request,
)
from .purchase_order_sql import GRANT, RETRYABLE
from .purchase_receipt_inventory_sql import (
    INVENTORY_READ_TABLES, INVENTORY_WRITE_COLUMNS, apply_receipt_inventory, lock_receipt_inventory,
)
from .purchase_source import PurchaseSource


JOURNAL_TABLE = 'purchase_receipt_application'
RECEIPT_COLUMNS = ('Recibo', 'Proveedor', 'Factura', 'Bodega', 'Pedido', 'FechaRecibo',
                   'ImptoVenta', 'MontoImptoVenta', 'RecibidoCompleto', 'Usuario', 'Estacion', 'FechaActualizacion')
DETAIL_COLUMNS = ('Recibo', 'PorMayor', 'Articulo', 'Sec', 'CantRecibida', 'CostoUnit', 'CostoUnitCIF',
                  'TipoIVA', 'IVA', 'Exento', 'ExentoConsumo', 'Recibido', 'Actualizado', 'NoRecibido',
                  'Usuario', 'Estacion', 'FechaActualizacion')
PAYABLE_COLUMNS = ('Proveedor', 'FacturaPrimaria', 'FechaDeuda', 'FormaPago', 'MontoPagarColones',
                   'MontoPagarDolares', 'MontoDeudaReal', 'Cerrada', 'Usuario', 'Estacion', 'FechaActualizacion')
PAYABLE_DETAIL_COLUMNS = ('Proveedor', 'FacturaPrimaria', 'Acreedor', 'Factura', 'Fecha', 'Monto', 'MontoImptoVenta',
                          'TipoCredito', 'TipoCambio', 'ImptoVenta', 'Generada', 'Actualizada',
                          'Usuario', 'Estacion', 'FechaActualizacion')
READ_TABLES = (set(INVENTORY_READ_TABLES) | PurchaseCreditReceiptSource.snapshot_tables
               | {'pgconsecutivos', 'proveedores', 'proveedoresimport', 'articulosproveedor'})
WRITE_COLUMNS = {
    **INVENTORY_WRITE_COLUMNS,
    ('UPDATE', 'pgconsecutivos'): {'Recibo'},
    ('INSERT', 'recibos'): set(RECEIPT_COLUMNS),
    ('INSERT', 'recibosart'): set(DETAIL_COLUMNS),
    ('INSERT', 'cuentaspagar'): set(PAYABLE_COLUMNS),
    ('INSERT', 'cuentaspagardet'): set(PAYABLE_DETAIL_COLUMNS),
}


def _profile(operation):
    if operation['action'] != 'purchase.receive' or operation['contract'] != 2:
        return False
    receipt = operation['receipt']
    if not _regular_credit(receipt['fiscal']):
        return False
    terms = receipt['fiscal']['payment_terms']
    issue = date.fromisoformat(receipt['invoice_date'])
    if (issue + timedelta(days=terms['credit_days'])).isoformat() != terms['due_date']:
        return False
    if len({line['article_id'] for line in receipt['lines']}) != len(receipt['lines']):
        return False
    if receipt['warehouse'] < 1 or any(
            line['unit_factor'] != 1 or not line['por_mayor']
            or Decimal(str(line['unit_cost'])) != Decimal(str(line['unit_cost'])).quantize(Decimal('.0001'))
            for line in receipt['lines']):
        return False
    totals = {key: Decimal(str(value)) for key, value in receipt['totals'].items()}
    if any(amount != amount.quantize(Decimal('.01')) for amount in totals.values()):
        return False
    net = sum(int(line['quantity']) * Decimal(str(line['unit_cost'])) for line in receipt['lines'])
    return (net.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == totals['untaxed']
            and (net * Decimal('.13')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == totals['tax']
            and totals['untaxed'] + totals['tax'] == totals['total'])


class PurchaseReceiptSql:
    def __init__(self, config, source_config, application_id, *, connect=pymysql.connect):
        self.config = dict(config)
        self.source_config = dict(source_config)
        self.application_id = application_id
        self.source_instance = source_config.get('instance') or source_config.get('source_instance')
        self.database = source_config.get('database', 'sgadata')
        self.integration = config.get('integration_schema', '')
        self.connect = connect
        if (not self.source_instance or config.get('source_instance') != self.source_instance
                or config.get('application_id') != application_id or config.get('database') != self.database
                or config.get('host') != source_config.get('host')
                or int(config.get('port', 3306)) != int(source_config.get('port', 3306))
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', self.integration)
                or self.integration == self.database or config.get('user') == source_config.get('user')
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,8}', config.get('audit_user', ''))
                or type(config.get('audit_station')) is not int or not 1 <= config['audit_station'] < 255
                or config.get('profile') != 'crc_iva13_credit_factor_one'
                or not isinstance(config.get('evidence_reference'), str) or not config['evidence_reference'].strip()):
            raise PurchaseEffectsError('SGA_RECEIPT_WRITER_CONFIGURATION_INVALID')
        self.journal = f'`{self.integration}`.`{JOURNAL_TABLE}`'

    @staticmethod
    def _status(operation, code, status='blocked'):
        return {**{key: operation[key] for key in COMMON | {'request_checksum'}}, 'status': status, 'code': code}

    def _assert_database(self, cursor):
        expected = {(privilege, table): {name.lower() for name in columns}
                    for (privilege, table), columns in WRITE_COLUMNS.items()}
        found, readable, journal_rights = {}, set(), set()
        cursor.execute('SHOW GRANTS FOR CURRENT_USER()')
        for row in cursor.fetchall():
            grant = str(next(iter(row.values())))
            match = re.match(r'^GRANT (.*?) ON ([^ ]+) TO ', grant)
            if not match or 'WITH GRANT OPTION' in grant.upper():
                raise PurchaseEffectsError('SGA_RECEIPT_WRITER_ACCOUNT_TOO_BROAD')
            privileges, resource = match.groups()
            if privileges == 'USAGE' and resource == '*.*':
                continue
            scope = resource.replace('`', '').split('.')
            items = list(GRANT.finditer(privileges))
            if len(scope) != 2 or ''.join(item[0] for item in items) != privileges:
                raise PurchaseEffectsError('SGA_RECEIPT_WRITER_ACCOUNT_TOO_BROAD')
            schema, table = scope
            for item in items:
                privilege, columns = item.groups()
                if (schema, table) == (self.integration, JOURNAL_TABLE) and not columns:
                    journal_rights.add(privilege)
                elif schema == self.database and table in READ_TABLES and privilege == 'SELECT' and not columns:
                    readable.add(table)
                elif schema == self.database and columns and (privilege, table) in expected:
                    found.setdefault((privilege, table), set()).update(
                        name.strip().strip('`').lower() for name in columns.split(','))
                else:
                    raise PurchaseEffectsError('SGA_RECEIPT_WRITER_ACCOUNT_TOO_BROAD')
        if found != expected or readable != READ_TABLES or journal_rights != {'SELECT', 'INSERT'}:
            raise PurchaseEffectsError('SGA_RECEIPT_WRITER_GRANTS_INVALID')
        cursor.execute('SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE FROM information_schema.TABLES '
                       'WHERE TABLE_SCHEMA IN (%s,%s)', (self.database, self.integration))
        engines = {(row['TABLE_SCHEMA'], row['TABLE_NAME']): row['ENGINE'] for row in cursor.fetchall()}
        required = {(self.database, name) for name in READ_TABLES} | {(self.integration, JOURNAL_TABLE)}
        if any(engines.get(key) != 'InnoDB' for key in required):
            raise PurchaseEffectsError('SGA_RECEIPT_WRITER_REQUIRES_INNODB')

    def apply(self, payload):
        operation = request(payload)
        if operation['application_id'] != self.application_id or operation['source_instance'] != self.source_instance:
            raise PurchaseEffectsError('SGA_RECEIPT_WRITER_SCOPE_MISMATCH')
        if not _profile(operation):
            return self._status(operation, 'BLOCKED_SGA_RECEIPT_PROFILE_UNSUPPORTED')
        for _attempt in range(3):
            try:
                return self._apply(operation)
            except pymysql.err.OperationalError as exc:
                if exc.args[0] not in RETRYABLE:
                    raise
        return self._status(operation, 'SGA_RECEIPT_WRITER_BUSY')

    def _apply(self, operation):
        connection = self.connect(
            host=self.config['host'], port=int(self.config.get('port', 3306)),
            user=self.config['user'], password=self.config['password'], database=self.database,
            unix_socket=self.config.get('unix_socket'), cursorclass=pymysql.cursors.DictCursor,
            charset='latin1', autocommit=False, connect_timeout=4, read_timeout=15, write_timeout=10,
        )
        committing = False
        try:
            with connection.cursor() as cursor:
                self._assert_database(cursor)
                cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute('SET SESSION innodb_lock_wait_timeout=3')
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                connection.begin()
                cursor.execute('SELECT Recibo FROM pgconsecutivos WHERE Llave=0 FOR UPDATE')
                counter = cursor.fetchone()
                if not counter or not 0 <= counter['Recibo'] < MAX_ID:
                    raise PurchaseEffectsError('SGA_RECEIPT_COUNTER_UNAVAILABLE')
                receipt = operation['receipt']
                identity = (operation['application_id'], operation['operation_id'])
                cursor.execute(f'SELECT request_hash,result FROM {self.journal} '
                               'WHERE application_uuid=%s AND operation_uuid=%s FOR UPDATE', identity)
                prior = cursor.fetchone()
                if prior:
                    if prior['request_hash'] != operation['request_checksum']:
                        raise PurchaseEffectsError('SGA_RECEIPT_OPERATION_CONTENT_CONFLICT')
                    return reconcile_receive(operation, json.loads(prior['result']))

                self._assert_invoice_absent(cursor, receipt)
                cursor.execute('SELECT Recibo FROM recibos ORDER BY Recibo DESC LIMIT 1 FOR UPDATE')
                highest = cursor.fetchone()
                last = counter['Recibo']
                if highest and highest['Recibo'] > last:
                    raise PurchaseEffectsError('SGA_RECEIPT_COUNTER_OUT_OF_SYNC')
                number = last + 1

                before = lock_receipt_inventory(cursor, receipt)
                self._verify_order_binding(connection, receipt)
                cursor.execute('SELECT NOW() AS applied_at')
                applied_at = cursor.fetchone()['applied_at']
                actor = self.config['audit_user'], self.config['audit_station']
                self._save_documents(cursor, receipt, number, applied_at, actor)
                costs = apply_receipt_inventory(cursor, receipt, before, audit_user=actor[0],
                                                audit_station=actor[1], applied_at=applied_at)
                cursor.execute('UPDATE pgconsecutivos SET Recibo=%s WHERE Llave=0 AND Recibo=%s', (number, last))
                if cursor.rowcount != 1:
                    raise PurchaseEffectsError('SGA_RECEIPT_COUNTER_CHANGED')
                result = self._verify_documents(connection, cursor, operation, number, costs)
                serialized = json.dumps(result, ensure_ascii=True, allow_nan=False, sort_keys=True)
                if len(serialized.encode('ascii')) > 240000:
                    raise PurchaseEffectsError('SGA_RECEIPT_RESULT_TOO_LARGE')
                cursor.execute(f'INSERT INTO {self.journal} '
                               '(application_uuid,operation_uuid,request_hash,invoice_key,vendor,invoice_number,'
                               'recibo,result,created_utc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())',
                               (*identity, operation['request_checksum'], receipt['invoice_key'], receipt['vendor_code'],
                                receipt['invoice_number'], number, serialized))
                committing = True
                try:
                    connection.commit()
                except pymysql.MySQLError:
                    raise NativeResultUnknown('Reconcile the same purchase receipt UUID') from None
                return result
        except NativeResultUnverified:
            raise
        except PurchaseEffectsError as exc:
            return self._status(operation, exc.code)
        except pymysql.err.IntegrityError as exc:
            if exc.args[0] != 1062:
                raise
            return self._status(operation, 'SGA_RECEIPT_NATIVE_IDENTITY_CONFLICT')
        finally:
            try:
                if not committing:
                    try:
                        connection.rollback()
                    except pymysql.MySQLError:
                        pass
            finally:
                connection.close()

    def _assert_invoice_absent(self, cursor, receipt):
        vendor, invoice = receipt['vendor_code'], receipt['invoice_number']
        checks = (
            (f'SELECT recibo FROM {self.journal} WHERE invoice_key=%s FOR UPDATE', (receipt['invoice_key'],)),
            (f'SELECT recibo FROM {self.journal} WHERE vendor=%s AND invoice_number=%s FOR UPDATE', (vendor, invoice)),
            ('SELECT Recibo FROM recibos WHERE Proveedor=%s AND Factura=%s LIMIT 1 FOR UPDATE', (vendor, invoice)),
            ('SELECT Proveedor FROM cuentaspagar WHERE Proveedor=%s AND FacturaPrimaria=%s LIMIT 1 FOR UPDATE',
             (vendor, invoice)),
            ('SELECT Proveedor FROM cuentaspagardet WHERE Acreedor=%s AND Factura=%s LIMIT 1 FOR UPDATE',
             (vendor, invoice)),
            ('SELECT Proveedor FROM cuentaspagardet WHERE Proveedor=%s AND FacturaPrimaria=%s LIMIT 1 FOR UPDATE',
             (vendor, invoice)),
        )
        for statement, params in checks:
            cursor.execute(statement, params)
            if cursor.fetchone():
                raise PurchaseEffectsError('SGA_RECEIPT_INVOICE_ALREADY_PRESENT')

    def _verify_order_binding(self, connection, receipt):
        reader = PurchaseSource(self.source_config)
        reader.connection = connection
        response = reader.search(receipt['vendor_code'], order_id=receipt['order_id'], include_closed=True, limit=1)
        rows = response.get('orders', [])
        if (response.get('status') != 'found' or len(rows) != 1 or not rows[0]['eligible']
                or rows[0]['currency_code'] != 'CRC'
                or rows[0]['checksum'] != receipt['order_checksum']):
            raise PurchaseEffectsError('SGA_RECEIPT_ORDER_VERSION_MISMATCH')

    @staticmethod
    def _save_documents(cursor, receipt, number, applied_at, actor):
        vendor, invoice = receipt['vendor_code'], receipt['invoice_number']
        fiscal, totals = receipt['fiscal'], receipt['totals']
        invoice_time = datetime.fromisoformat(fiscal['issued_at'].replace('Z', '+00:00')).replace(tzinfo=None)
        rate = Decimal('.13')
        audit = (*actor, applied_at)
        _insert(cursor, 'recibos', RECEIPT_COLUMNS, (number, vendor, invoice, receipt['warehouse'], receipt['order_id'],
                fiscal['physical_receipt_date'], rate, totals['tax'], 1, *audit))
        for index, line in enumerate(receipt['lines']):
            cost = line['unit_cost']
            _insert(cursor, 'recibosart', DETAIL_COLUMNS, (number, int(line['por_mayor']), line['article_id'], index,
                    int(line['quantity']), cost, cost, '08', rate, 0, 1, 1, 1, 0, *audit))
        _insert(cursor, 'cuentaspagar', PAYABLE_COLUMNS, (vendor, invoice, invoice_time, 2, totals['total'],
                0, totals['total'], 0, *audit))
        _insert(cursor, 'cuentaspagardet', PAYABLE_DETAIL_COLUMNS, (vendor, invoice, vendor, invoice, invoice_time,
                totals['total'], totals['tax'], fiscal['payment_terms']['credit_days'], 1, rate, 1, 1, *audit))

    def _verify_documents(self, connection, cursor, operation, number, costs):
        reader = PurchaseCreditReceiptSource(self.source_config)
        reader.connection = connection
        proof = reader.verify(operation)
        if proof['status'] != 'matched' or proof['receipt_id'] != number:
            raise PurchaseEffectsError(proof.get('code', 'SGA_RECEIPT_WRITE_READBACK_MISMATCH'))
        receipt = operation['receipt']
        cursor.execute('SELECT h.MontoPagarColones,h.MontoPagarDolares,h.MontoDeudaReal,h.FormaPago,'
                       'h.Cerrada,d.MontoCancelado,d.MontoNotas,d.Retencion,d.MontoPagosPend '
                       'FROM cuentaspagar h JOIN cuentaspagardet d ON h.Proveedor=d.Proveedor '
                       'AND h.FacturaPrimaria=d.FacturaPrimaria '
                       'WHERE h.Proveedor=%s AND h.FacturaPrimaria=%s',
                       (receipt['vendor_code'], receipt['invoice_number']))
        payable = cursor.fetchone()
        total = Decimal(str(receipt['totals']['total']))
        if (not payable or payable['FormaPago'] != 2 or payable['Cerrada'] not in (0, b'\x00')
                or any(Decimal(str(payable[field])) != total for field in ('MontoPagarColones', 'MontoDeudaReal'))
                or any(payable[field] not in (None, 0) for field in (
                    'MontoPagarDolares', 'MontoCancelado', 'MontoNotas', 'Retencion', 'MontoPagosPend'))):
            raise PurchaseEffectsError('SGA_RECEIPT_PAYABLE_BALANCE_MISMATCH')
        lines = []
        for line in receipt['lines']:
            fields = {key: value for key, value in line.items() if key not in ('unit_factor', 'unit_cost')}
            lines.append({**fields, 'receipt_id': number, 'cost_confirmed': True,
                          'average_cost_base': float(costs[tuple(line['order_line_key'])])})
        result = {**{key: operation[key] for key in COMMON | {'request_checksum'}}, 'status': 'synchronized',
                  'receipt_ids': [number], 'order_id': receipt['order_id'], 'ap_confirmed': True,
                  'order_verified': True, 'stock_confirmed': True, 'valuation_confirmed': True,
                  'fiscal_confirmed': True, 'fiscal_checksum': digest(receipt['fiscal']),
                  'physical_receipt_date': receipt['fiscal']['physical_receipt_date'], 'lines': lines,
                  'payable': {'invoice_key': receipt['invoice_key'], 'primary_key': proof['payable']['primary_key'],
                              'detail_keys': [proof['payable']['detail_key']], 'currency': 'CRC',
                              'amount_total': proof['payable']['total'], 'exchange_rate': 1,
                              'payment_terms': receipt['fiscal']['payment_terms']}}
        return reconcile_receive(operation, result)


def _insert(cursor, table, columns, values):
    # Identifiers are constants at call sites; fiscal/business data stays parameterized.
    sql = f'INSERT INTO `{table}` ({",".join(columns)}) VALUES ({",".join(["%s"] * len(columns))})'
    cursor.execute(sql, values)
