"""Capture the native costs and quantities around one purchase receipt."""

from decimal import Decimal, ROUND_HALF_UP

from .catalog_source import text
from .purchase_credit_receipt import PurchaseCreditReceiptSource
from .purchase_effects_contract import MAX_LINES, PurchaseEffectsError, request
from .purchase_source import _bit, _digest, _integer, _iso, _money


class PurchaseValuationSnapshot(PurchaseCreditReceiptSource):
    snapshot_tables = PurchaseCreditReceiptSource.snapshot_tables | {'articulosbodegas', 'articulosproveedor'}

    def capture(self, payload):
        verification = self.verify(payload)
        if verification['status'] == 'unavailable':
            return verification
        scope = verification['scope']
        rows = self.rows(
            'SELECT UTC_TIMESTAMP() AS CapturedAt,p.Proveedor AS OrderVendor,pa.PorMayor,pa.Articulo,'
            'pa.CantPedida AS Ordered,pa.CantRecibida AS OrderReceived,pa.Saldada,'
            'a.NumeroUnidades,a.CantDisponible,a.CantDisponibleDet,a.CostoUnit,a.CostoUnitDet,'
            'a.CantPedida AS GlobalOrdered,a.CantPedidaDet AS GlobalOrderedDetail,'
            'b.Articulo AS WarehouseArticle,b.Cant,b.CantDet,b.FechaUltIngreso,'
            'ap.Articulo AS SupplierArticle,ap.CantPedida AS SupplierOrdered,'
            'ap.CantPedidaDet AS SupplierOrderedDetail,ap.FechaUltCompra AS SupplierLastPurchase '
            'FROM pedidos p JOIN pedidosarticulos pa ON pa.Pedido=p.Pedido '
            'LEFT JOIN artsservs a ON a.Articulo=pa.Articulo '
            'LEFT JOIN articulosbodegas b ON b.Articulo=pa.Articulo AND b.Bodega=%s '
            'LEFT JOIN articulosproveedor ap ON ap.Articulo=pa.Articulo AND ap.Proveedor=p.Proveedor '
            'WHERE p.Pedido=%s ORDER BY pa.PorMayor,pa.Articulo LIMIT %s',
            (scope['warehouse'], scope['order_id'], MAX_LINES + 1))
        if not 1 <= len(rows) <= MAX_LINES or any(text(row['OrderVendor']) != scope['vendor_code'] for row in rows):
            return {'status': 'conflict', 'code': 'SGA_VALUATION_ORDER_SCOPE_UNVERIFIED', 'scope': scope}

        lines = [_valuation_line(row, scope['order_id']) for row in rows]
        keys = {tuple(line['order_line_key']) for line in lines}
        expected = {tuple(line['order_line_key']) for line in payload['receipt']['lines']}
        if not expected <= keys or len(keys) != len(lines):
            return {'status': 'conflict', 'code': 'SGA_VALUATION_ORDER_LINE_UNVERIFIED', 'scope': scope}
        snapshot = {'status': 'captured', 'receipt_verification': verification,
                    'captured_at_utc': _iso(rows[0]['CapturedAt']), 'lines': lines}
        snapshot['checksum'] = _digest(snapshot)
        return snapshot


def _valuation_line(row, order_id):
    key = [order_id, _bit(row['PorMayor']), _integer(row['Articulo'], minimum=1)]
    ordered, received = (_integer(row[field], minimum=0) for field in ('Ordered', 'OrderReceived'))
    if row['WarehouseArticle'] is None:
        warehouse = None
    else:
        warehouse = {'quantity_bulk': _integer(row['Cant']), 'quantity_detail': _integer(row['CantDet']),
                     'last_receipt': _iso(row['FechaUltIngreso'])}
    if row['SupplierArticle'] is None:
        supplier = None
    else:
        supplier = {'pending_bulk': _integer(row['SupplierOrdered']),
                    'pending_detail': _integer(row['SupplierOrderedDetail']),
                    'last_purchase': _iso(row['SupplierLastPurchase'])}
    return {'order_line_key': key, 'ordered': ordered, 'received': received, 'remaining': ordered - received,
            'closed': _bit(row['Saldada']), 'unit_factor': _integer(row['NumeroUnidades'], minimum=1),
            'warehouse': warehouse, 'supplier': supplier,
            'global': {'quantity_bulk': _integer(row['CantDisponible']),
                       'quantity_detail': _integer(row['CantDisponibleDet']),
                       'pending_bulk': _integer(row['GlobalOrdered']),
                       'pending_detail': _integer(row['GlobalOrderedDetail']),
                       'unit_cost_bulk': _money(row['CostoUnit']), 'unit_cost_detail': _money(row['CostoUnitDet'])}}


def weighted_unit_cost(before_quantity, before_cost, received_quantity, received_cost):
    """Four-decimal reference for comparing observed, factor-one SGA costs."""
    if before_quantity < 0 or received_quantity <= 0:
        raise PurchaseEffectsError('SGA_VALUATION_QUANTITY_UNSUPPORTED')
    initial = Decimal(str(before_cost)) * before_quantity
    added = Decimal(str(received_cost)) * received_quantity
    return ((initial + added) / (before_quantity + received_quantity)).quantize(
        Decimal('.0001'), rounding=ROUND_HALF_UP)


def compare_purchase_valuation(payload, before, after):
    operation = request(payload)
    if operation['action'] != 'purchase.receive' or operation['contract'] != 2:
        raise PurchaseEffectsError('SGA_VALUATION_REQUIRES_RECEIPT_V2')
    binding = {key: operation[key] for key in ('source_instance', 'application_id', 'operation_id', 'request_checksum')}
    for snapshot, receipt_status in ((before, 'not_found'), (after, 'matched')):
        if (not isinstance(snapshot, dict) or snapshot.get('status') != 'captured'
                or snapshot.get('checksum') != _digest({k: v for k, v in snapshot.items() if k != 'checksum'})):
            raise PurchaseEffectsError('SGA_VALUATION_SNAPSHOT_INVALID')
        verification = snapshot['receipt_verification']
        if verification['status'] != receipt_status or any(verification[k] != v for k, v in binding.items()):
            raise PurchaseEffectsError('SGA_VALUATION_RECEIPT_UNVERIFIED')
    if before['captured_at_utc'] >= after['captured_at_utc']:
        raise PurchaseEffectsError('SGA_VALUATION_SNAPSHOT_ORDER_INVALID')

    opening, closing = [{tuple(line['order_line_key']): line for line in snapshot['lines']}
                        for snapshot in (before, after)]
    allocations = {tuple(line['order_line_key']): line for line in operation['receipt']['lines']}
    if (opening.keys() != closing.keys() or not allocations.keys() <= opening.keys()
            or len({key[2] for key in opening}) != len(opening)):
        raise PurchaseEffectsError('SGA_VALUATION_ORDER_LINES_CHANGED')

    result = []
    for key, first in opening.items():
        last, allocation = closing[key], allocations.get(key)
        row = {'order_line_key': list(key), 'remaining_before': first['remaining'], 'remaining_after': last['remaining']}
        if allocation:
            row.update(_compare_received_line(first, last, allocation))
        else:
            row['matched'] = first == last
        result.append(row)
    return {'status': 'matched' if all(row['matched'] for row in result) else 'mismatch', 'lines': result,
            'remaining_before': sum(row['remaining_before'] for row in result),
            'remaining_after': sum(row['remaining_after'] for row in result),
            'before_checksum': before['checksum'], 'after_checksum': after['checksum']}


def _compare_received_line(first, last, allocation):
    if {first['unit_factor'], last['unit_factor'], allocation['unit_factor']} != {1}:
        raise PurchaseEffectsError('SGA_VALUATION_UNIT_FACTOR_UNSUPPORTED')
    old, new = first['global'], last['global']
    stock_before, stock_after = (row['quantity_bulk'] + row['quantity_detail'] for row in (old, new))
    quantity = int(allocation['quantity_base'])
    cost_expected = weighted_unit_cost(stock_before, old['unit_cost_bulk'], quantity, allocation['unit_cost_base'])
    warehouse_before, warehouse_after = (sum(row['warehouse'][field] for field in ('quantity_bulk', 'quantity_detail'))
                                         if row['warehouse'] else 0 for row in (first, last))
    global_pending_delta = sum(old[field] - new[field] for field in ('pending_bulk', 'pending_detail'))
    supplier_before, supplier_after = first['supplier'], last['supplier']
    supplier_pending_delta = None
    if supplier_before is not None and supplier_after is not None:
        supplier_pending_delta = sum(supplier_before[field] - supplier_after[field]
                                     for field in ('pending_bulk', 'pending_detail'))
    matched = (stock_after - stock_before == warehouse_after - warehouse_before == quantity
               and last['received'] - first['received'] == quantity and first['ordered'] == last['ordered']
               and 0 <= last['remaining'] < first['remaining'] and (last['remaining'] == 0 or not last['closed'])
               and global_pending_delta == supplier_pending_delta == quantity
               and Decimal(str(new['unit_cost_bulk'])) == cost_expected)
    return {'matched': matched, 'quantity_before': stock_before, 'quantity_after': stock_after,
            'warehouse_before': warehouse_before, 'warehouse_after': warehouse_after,
            'global_pending_delta': global_pending_delta, 'supplier_pending_delta': supplier_pending_delta,
            'unit_cost_before': old['unit_cost_bulk'], 'unit_cost_after': new['unit_cost_bulk'],
            'expected_unit_cost': float(cost_expected)}
