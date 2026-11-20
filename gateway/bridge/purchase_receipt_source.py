"""Find historical native receipts in a single SELECT-only snapshot."""

from collections import defaultdict
from decimal import Decimal

from .catalog_source import CatalogSource, text
from .purchase_source import MAX_ID, _bit, _digest, _integer, _iso, _money


MAX_RECEIPTS = 50
MAX_RECEIPT_LINES = 500
MAX_WAREHOUSE = 32767
RECEIPT_TABLES = {'recibos', 'recibosart', 'pedidos', 'pedidosarticulos', 'artsservs'}
SCOPE_FIELDS = {'vendor_code', 'invoice_number', 'warehouse', 'order_id'}


def _native_reference(value, length):
    return (isinstance(value, str) and 1 <= len(value) <= length
            and value == value.strip() and value.isprintable()
            and all(ord(character) <= 255 for character in value))


def valid_scope(scope):
    if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS:
        return False
    order_id = scope['order_id']
    return (_native_reference(scope['vendor_code'], 3)
            and _native_reference(scope['invoice_number'], 20)
            and type(scope['warehouse']) is int and 1 <= scope['warehouse'] <= MAX_WAREHOUSE
            and (order_id is None or type(order_id) is int and 1 <= order_id <= MAX_ID))


class ReceiptConflict(ValueError):
    """The native rows prevent an unambiguous, bounded receipt snapshot."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PurchaseReceiptSource(CatalogSource):
    def _verify_snapshot_tables(self):
        engines = self.rows(
            'SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() '
            'AND TABLE_NAME IN (%s,%s,%s,%s,%s)', tuple(sorted(RECEIPT_TABLES)))
        transactional = {row['TABLE_NAME'].lower() for row in engines if row['ENGINE'] == 'InnoDB'}
        return transactional == RECEIPT_TABLES

    def lookup(self, *, vendor_code, invoice_number, warehouse, order_id):
        scope = {'vendor_code': vendor_code, 'invoice_number': invoice_number,
                 'warehouse': warehouse, 'order_id': order_id}
        if not valid_scope(scope):
            raise ValueError('Invalid purchase receipt scope')
        result = {'source_instance': self.config.get('instance') or self.config.get('source_instance'),
                  'scope': scope}
        if not self._verify_snapshot_tables():
            return {**result, 'status': 'unavailable', 'code': 'SGA_RECEIPT_REQUIRES_INNODB'}

        # Query the full vendor/invoice identity. A receipt bound elsewhere is
        # a scope conflict and must never become an apparent absence.
        headers = self.rows(
            'SELECT r.Recibo,r.Proveedor,r.Factura,r.Bodega,r.Pedido,r.FechaRecibo,'
            'r.RecibidoCompleto,r.Descuento,r.OtrosCargos,p.Proveedor AS OrderVendor '
            'FROM recibos r LEFT JOIN pedidos p ON p.Pedido=r.Pedido '
            'WHERE r.Proveedor=%s AND r.Factura=%s ORDER BY r.Recibo LIMIT %s',
            (vendor_code, invoice_number, MAX_RECEIPTS + 1))
        if not headers:
            return {**result, 'status': 'not_found', 'receipts': []}
        if len(headers) > MAX_RECEIPTS:
            return {**result, 'status': 'conflict', 'code': 'SGA_RECEIPT_LOOKUP_TOO_BROAD'}
        try:
            actual_order, receipts = self._evidence(headers, scope)
        except ReceiptConflict as exc:
            return {**result, 'status': 'conflict', 'code': exc.code}
        result.update(status='found', order_id=actual_order, receipts=receipts)
        result['checksum'] = _digest(result)
        return result

    def _evidence(self, headers, scope):
        receipt_by_id = {}
        order_ids = set()
        for row in headers:
            receipt_id = _integer(row['Recibo'], minimum=1)
            # Verify the exact reference even on a case-insensitive SQL collation.
            if (row['Factura'] != scope['invoice_number'] or text(row['Proveedor']) != scope['vendor_code']
                    or text(row['OrderVendor']) != scope['vendor_code'] or row['Bodega'] != scope['warehouse']
                    or type(row['Pedido']) is not int or not 1 <= row['Pedido'] <= MAX_ID
                    or scope['order_id'] is not None and row['Pedido'] != scope['order_id']):
                raise ReceiptConflict('SGA_RECEIPT_SCOPE_CONFLICT')
            if receipt_id in receipt_by_id or row['FechaRecibo'] is None:
                raise ReceiptConflict('SGA_RECEIPT_HEADER_IDENTITY_CONFLICT')
            # CostoUnit records the supplier line cost. A separate receipt-wide
            # discount or charge has no proven line allocation in this reader.
            if any(_money(row[key] if row[key] is not None else 0) != 0
                   for key in ('Descuento', 'OtrosCargos')):
                raise ReceiptConflict('SGA_RECEIPT_UNALLOCATED_NET_COST')
            receipt_by_id[receipt_id] = {'receipt_id': receipt_id, 'receipt_date': _iso(row['FechaRecibo']),
                                         'complete': _bit(row['RecibidoCompleto']), 'lines': []}
            order_ids.add(row['Pedido'])
        if len(order_ids) != 1:
            raise ReceiptConflict('SGA_RECEIPT_MULTIPLE_ORDERS')
        order_id = order_ids.pop()

        placeholders = ','.join(['%s'] * len(receipt_by_id))
        rows = self.rows(
            'SELECT ra.Recibo,ra.PorMayor,ra.Articulo,ra.CantRecibida,ra.CostoUnit,'
            'ra.Recibido,ra.Actualizado,ra.NoRecibido,pa.Pedido AS OrderId,'
            'pa.CantPedida,pa.CantRecibida AS OrderReceived,pa.Saldada,a.NumeroUnidades '
            'FROM recibosart ra LEFT JOIN pedidosarticulos pa ON pa.Pedido=%s '
            'AND pa.PorMayor=ra.PorMayor AND pa.Articulo=ra.Articulo '
            'LEFT JOIN artsservs a ON a.Articulo=ra.Articulo '
            'WHERE ra.Recibo IN (' + placeholders + ') ORDER BY ra.Recibo,ra.PorMayor,ra.Articulo LIMIT %s',
            (order_id, *receipt_by_id, MAX_RECEIPT_LINES + 1))
        if len(rows) > MAX_RECEIPT_LINES:
            raise ReceiptConflict('SGA_RECEIPT_LOOKUP_TOO_BROAD')
        line_keys = set()
        applied = defaultdict(int)
        for row in rows:
            if row['Recibo'] not in receipt_by_id or row['OrderId'] != order_id:
                raise ReceiptConflict('SGA_RECEIPT_ORDER_LINE_CONFLICT')
            line = _receipt_line(row, order_id)
            line_key = tuple(line['line_key'])
            if line_key in line_keys:
                raise ReceiptConflict('SGA_RECEIPT_LINE_IDENTITY_CONFLICT')
            line_keys.add(line_key)
            if line['applied']:
                key = tuple(line['order_line_key'])
                applied[key] += line['quantity']
                if applied[key] > line['order_received']:
                    raise ReceiptConflict('SGA_RECEIPT_ORDER_BALANCE_CONFLICT')
            receipt_by_id[row['Recibo']]['lines'].append(line)
        receipts = []
        for receipt in sorted(receipt_by_id.values(), key=lambda row: row['receipt_id']):
            if not receipt['lines']:
                raise ReceiptConflict('SGA_RECEIPT_NO_LINES')
            receipt['lines'].sort(key=lambda line: (line['por_mayor'], line['article_id']))
            receipt['checksum'] = _digest(receipt)
            receipts.append(receipt)
        return order_id, receipts


def _receipt_line(row, order_id):
    receipt_id = _integer(row['Recibo'], minimum=1)
    article_id = _integer(row['Articulo'], minimum=1)
    wholesale = _bit(row['PorMayor'])
    unit_factor = _integer(row['NumeroUnidades'], minimum=1, maximum=32767)
    # NumeroUnidades is current catalog evidence. A wholesale mirror must
    # also confirm the conversion against its own purchase/document mapping.
    multiplier = unit_factor if wholesale else 1
    quantity = _integer(row['CantRecibida'], minimum=0)
    quantity_base = _integer(quantity * multiplier, minimum=0)
    ordered = _integer(row['CantPedida'], minimum=0)
    order_received = _integer(row['OrderReceived'], minimum=0)
    if order_received > ordered:
        raise ReceiptConflict('SGA_RECEIPT_ORDER_BALANCE_CONFLICT')
    cost = _money(row['CostoUnit'])
    if cost < 0:
        raise ReceiptConflict('SGA_RECEIPT_INVALID_NET_COST')
    received, updated, not_received = (_bit(row[field])
                                       for field in ('Recibido', 'Actualizado', 'NoRecibido'))
    return {'line_key': [receipt_id, wholesale, article_id], 'order_line_key': [order_id, wholesale, article_id],
            'article_id': article_id, 'por_mayor': wholesale, 'unit_factor': unit_factor,
            'quantity': quantity, 'quantity_base': quantity_base,
            'unit_cost_net': cost, 'unit_cost_net_base': _money(Decimal(str(cost)) / multiplier),
            'received': received, 'updated': updated, 'not_received': not_received,
            'applied': received and updated and not not_received and quantity > 0,
            'ordered': ordered, 'order_received': order_received, 'remaining': ordered - order_received,
            'saldada': _bit(row['Saldada'])}
