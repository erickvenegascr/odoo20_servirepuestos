"""Capture the native costs and quantities around one purchase receipt."""

from .catalog_source import text
from .purchase_credit_receipt import PurchaseCreditReceiptSource
from .purchase_effects_contract import MAX_LINES
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
