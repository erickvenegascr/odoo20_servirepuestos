"""Inventory effects for a domestic SGA purchase receipt in a caller-held transaction."""

from datetime import date, datetime, time
from decimal import Decimal

from .catalog_source import text
from .purchase_effects_contract import MAX_ID, MAX_LINES, MAX_MONEY, PurchaseEffectsError
from .purchase_source import PurchaseSourceError, _bit
from .purchase_valuation_snapshot import weighted_unit_cost


INVENTORY_READ_TABLES = {'pedidos', 'pedidosarticulos', 'artsservs', 'articulosbodegas', 'articulosproveedor',
                         'artsservsprecios', 'artsservsprecioshi', 'preciosniveles'}
AUDIT_COLUMNS = {'Usuario', 'Estacion', 'FechaActualizacion'}
INVENTORY_WRITE_COLUMNS = {
    ('UPDATE', 'pedidosarticulos'): {'CantRecibida', 'Saldada'} | AUDIT_COLUMNS,
    ('UPDATE', 'artsservs'): {'CantDisponible', 'CantPedida', 'CostoUnit', 'CostoUnitDet',
                            'FechaUltCompra', 'UltCantComprada', 'UltProveedor'} | AUDIT_COLUMNS,
    ('UPDATE', 'articulosbodegas'): {'Cant', 'FechaUltIngreso'} | AUDIT_COLUMNS,
    ('UPDATE', 'articulosproveedor'): {'CantPedida', 'FechaUltCompra', 'UltCantComprada'} | AUDIT_COLUMNS,
    ('INSERT', 'artsservsprecioshi'): {'Articulo', 'Nivel', 'Fecha', 'CostoUnit', 'CostoUnitDet',
                                    'VariacionDet', 'PorcGanancia', 'TipoCambio'} | AUDIT_COLUMNS,
}

LOCK_ORDER = (
    'SELECT p.Proveedor AS OrderVendor,pa.PorMayor,pa.Articulo,'
    'pa.CantPedida AS Ordered,pa.CantRecibida AS OrderReceived,pa.Saldada,'
    'pa.Consignacion AS OrderConsignment,pa.UltCostoCotizado,'
    'pa.Usuario AS OrderUser,pa.Estacion AS OrderStation,pa.FechaActualizacion AS OrderUpdated,pa.PGTS AS OrderTS,'
    'a.Articulo AS CatalogArticle,a.NumeroUnidades,a.CantDisponible,a.CantDisponibleDet,'
    'a.CostoUnit,a.CostoUnitDet,a.CantPedida AS GlobalOrdered,a.CantPedidaDet AS GlobalOrderedDetail,'
    'a.Trabajado,a.Servicio,a.Combo,a.PorLote,a.Consignacion AS CatalogConsignment,'
    'a.UltProveedor,a.FechaUltCompra AS CatalogLastPurchase,a.UltCantComprada AS CatalogLastQty,a.CantCompradaDet,'
    'a.PrecioCotiz,a.PrecioCotizDet,a.PrecioCotizDol,a.PrecioCotizDolDet,'
    'a.Usuario AS CatalogUser,a.Estacion AS CatalogStation,a.FechaActualizacion AS CatalogUpdated,a.PGTS AS CatalogTS,'
    'b.Articulo AS WarehouseArticle,b.Cant,b.CantDet,b.FechaUltIngreso,b.Deshabilitado AS WarehouseDisabled,'
    'b.Usuario AS WarehouseUser,b.Estacion AS WarehouseStation,b.FechaActualizacion AS WarehouseUpdated,'
    'b.PGTS AS WarehouseTS,'
    'ap.Articulo AS SupplierArticle,ap.CantPedida AS SupplierOrdered,ap.CantPedidaDet AS SupplierOrderedDetail,'
    'ap.Deshabilitado AS SupplierDisabled,ap.FechaUltCompra AS SupplierLastPurchase,'
    'ap.UltCantComprada AS SupplierLastQty,ap.PrecioCotiz AS SupplierQuote,ap.PrecioCotizDet AS SupplierQuoteDetail,'
    'ap.PrecioCotizDol AS SupplierQuoteUSD,ap.PrecioCotizDolDet AS SupplierQuoteDetailUSD,'
    'ap.Usuario AS SupplierUser,ap.Estacion AS SupplierStation,ap.FechaActualizacion AS SupplierUpdated,'
    'ap.PGTS AS SupplierTS '
    'FROM pedidos p JOIN pedidosarticulos pa ON pa.Pedido=p.Pedido '
    'LEFT JOIN artsservs a ON a.Articulo=pa.Articulo '
    'LEFT JOIN articulosbodegas b ON b.Articulo=pa.Articulo AND b.Bodega=%s '
    'LEFT JOIN articulosproveedor ap ON ap.Articulo=pa.Articulo AND ap.Proveedor=p.Proveedor '
    'WHERE p.Pedido=%s ORDER BY pa.PorMayor,pa.Articulo LIMIT %s FOR UPDATE'
)

AUTO_TIMESTAMPS = {'OrderTS', 'CatalogTS', 'WarehouseTS', 'SupplierTS'}


def _order_rows(cursor, receipt):
    order_id = receipt['order_id']
    cursor.execute(LOCK_ORDER, (receipt['warehouse'], order_id, MAX_LINES + 1))
    rows = cursor.fetchall()
    if not 1 <= len(rows) <= MAX_LINES or any(text(row['OrderVendor']) != receipt['vendor_code'] for row in rows):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_ORDER_UNAVAILABLE')
    keyed = {(order_id, _native_bit(row['PorMayor']), row['Articulo']): row for row in rows}
    # An article shared by two order lines would share stock and pending quantities.
    if len({key[2] for key in keyed}) != len(rows):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_ARTICLE_AMBIGUOUS')
    return keyed


def _native_bit(value):
    try:
        return _bit(value)
    except PurchaseSourceError:
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_SOURCE_INVALID') from None


def _cost(value):
    if type(value) not in (int, float, Decimal):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_COST_INVALID')
    cost = Decimal(str(value))
    if not cost.is_finite() or not 0 <= cost <= MAX_MONEY or cost != cost.quantize(Decimal('.0001')):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_COST_INVALID')
    return cost


def _next_values(row, line, physical_receipt, vendor):
    quantity = int(line['quantity'])
    received_cost = _cost(line['unit_cost'])
    if (not line['por_mayor'] or line['unit_factor'] != 1 or line['quantity'] != line['quantity_base']
            or received_cost != _cost(line['unit_cost_base'])):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_UNIT_PROFILE_UNSUPPORTED')
    if not received_cost:
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_COST_PROFILE_UNSUPPORTED')

    article = line['article_id']
    if any(row[key] != article for key in ('CatalogArticle', 'WarehouseArticle', 'SupplierArticle')):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_ARTICLE_RELATION_MISSING')
    if (row['NumeroUnidades'] != 1 or row['Trabajado'] != 255
            or any(_native_bit(row[key]) for key in (
                'OrderConsignment', 'Servicio', 'Combo', 'PorLote', 'CatalogConsignment',
                'WarehouseDisabled', 'SupplierDisabled'))):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_ARTICLE_PROFILE_UNSUPPORTED')

    numeric = ('Ordered', 'OrderReceived', 'CantDisponible', 'Cant', 'GlobalOrdered', 'SupplierOrdered',
               'CantDisponibleDet', 'CantDet', 'GlobalOrderedDetail', 'SupplierOrderedDetail')
    if any(type(row[key]) is not int or not 0 <= row[key] <= MAX_ID for key in numeric):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_QUANTITY_UNSUPPORTED')
    if (row['CantDisponibleDet'] != 0 or row['CantDet'] != 0 or row['Cant'] > row['CantDisponible']
            or row['CantDisponible'] + quantity > MAX_ID):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_STOCK_PROFILE_UNSUPPORTED')

    remaining = row['Ordered'] - row['OrderReceived']
    if (quantity > remaining or _native_bit(row['Saldada']) or row['GlobalOrdered'] < row['SupplierOrdered']
            or row['SupplierOrdered'] < remaining):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_PENDING_MISMATCH')
    current_cost = _cost(row['CostoUnit'])
    if current_cost != _cost(row['CostoUnitDet']):
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_COST_PROFILE_UNSUPPORTED')
    for field in ('FechaUltIngreso', 'SupplierLastPurchase', 'CatalogLastPurchase'):
        previous = row[field]
        if previous is not None and (type(previous) is not datetime or previous > physical_receipt):
            raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_LAST_PURCHASE_NEWER')
    average = weighted_unit_cost(row['CantDisponible'], current_cost, quantity, received_cost)
    return {
        'OrderReceived': row['OrderReceived'] + quantity,
        'Saldada': quantity == remaining,
        'CantDisponible': row['CantDisponible'] + quantity,
        'GlobalOrdered': row['GlobalOrdered'] - quantity,
        'CostoUnit': average,
        'CostoUnitDet': average,
        'CatalogLastPurchase': physical_receipt,
        'CatalogLastQty': quantity,
        'UltProveedor': vendor,
        'Cant': row['Cant'] + quantity,
        'FechaUltIngreso': physical_receipt,
        'SupplierOrdered': row['SupplierOrdered'] - quantity,
        'SupplierLastPurchase': physical_receipt,
        'SupplierLastQty': quantity,
    }


def lock_receipt_inventory(cursor, receipt):
    """Lock the native order and stock rows and validate all allocations before writes."""
    before = _order_rows(cursor, receipt)
    allocations = {tuple(line['order_line_key']): line for line in receipt['lines']}
    if len(allocations) != len(receipt['lines']) or not allocations.keys() <= before.keys():
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_ALLOCATION_MISMATCH')
    physical_receipt = datetime.combine(date.fromisoformat(receipt['fiscal']['physical_receipt_date']), time.min)
    changes = {key: _next_values(before[key], line, physical_receipt, receipt['vendor_code'])
               for key, line in allocations.items()}
    return before, changes


def _write_one(cursor, sql, params):
    cursor.execute(sql, params)
    if cursor.rowcount != 1:
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_WRITE_MISMATCH')


def _lock_cost_history(cursor, article, history_date):
    cursor.execute('SELECT p.Nivel,p.TipoCambio,n.EnDolares FROM artsservsprecios p '
                   'LEFT JOIN preciosniveles n ON n.Nivel=p.Nivel '
                   'WHERE p.Articulo=%s ORDER BY p.Nivel LIMIT 2 FOR UPDATE', (article,))
    levels = cursor.fetchall()
    if len(levels) != 1 or levels[0]['Nivel'] != 1 or levels[0]['TipoCambio'] != 1 or (
            levels[0]['EnDolares'] is None or _native_bit(levels[0]['EnDolares'])):
        raise PurchaseEffectsError('SGA_RECEIPT_COST_HISTORY_PRICE_PROFILE_UNSUPPORTED')
    cursor.execute('SELECT Articulo FROM artsservsprecioshi '
                   'WHERE Articulo=%s AND Nivel=1 AND Fecha=%s FOR UPDATE', (article, history_date))
    if cursor.fetchone():
        raise PurchaseEffectsError('SGA_RECEIPT_COST_HISTORY_DAY_OCCUPIED')


def _save_cost_history(cursor, article, history_date, cost, audit):
    # Copy native single-precision pricing values inside MariaDB so that the
    # historic double preserves the original value (not its display precision).
    _write_one(cursor, 'INSERT INTO artsservsprecioshi '
               '(Articulo,Nivel,Fecha,CostoUnit,CostoUnitDet,VariacionDet,PorcGanancia,TipoCambio,'
               'Usuario,Estacion,FechaActualizacion) '
               'SELECT Articulo,Nivel,%s,%s,%s,VariacionDet,PorcGanancia,TipoCambio,%s,%s,%s '
               'FROM artsservsprecios WHERE Articulo=%s AND Nivel=1', (history_date, cost, cost, *audit, article))
    cursor.execute('SELECT h.CostoUnit,h.CostoUnitDet,h.VariacionDet=p.VariacionDet AS VariationMatches,'
                   'h.PorcGanancia=p.PorcGanancia AS MarginMatches,h.TipoCambio=p.TipoCambio AS RateMatches,'
                   'h.Usuario,h.Estacion,h.FechaActualizacion FROM artsservsprecioshi h '
                   'JOIN artsservsprecios p ON p.Articulo=h.Articulo AND p.Nivel=h.Nivel '
                   'WHERE h.Articulo=%s AND h.Nivel=1 AND h.Fecha=%s', (article, history_date))
    result = cursor.fetchone()
    if (not result or _cost(result['CostoUnit']) != cost or _cost(result['CostoUnitDet']) != cost
            or not all(result[key] == 1 for key in ('VariationMatches', 'MarginMatches', 'RateMatches'))
            or (result['Usuario'], result['Estacion'], result['FechaActualizacion']) != audit):
        raise PurchaseEffectsError('SGA_RECEIPT_COST_HISTORY_READBACK_MISMATCH')


def apply_receipt_inventory(cursor, receipt, snapshot, *, audit_user, audit_station, applied_at):
    """Write and read back each planned change, returning confirmed average costs."""
    before, changes = snapshot
    audit = (audit_user, audit_station, applied_at)
    history_date = datetime.combine(applied_at.date(), time.min)
    changed_costs = {key[2]: value['CostoUnit'] for key, value in changes.items()
                     if value['CostoUnit'] != _cost(before[key]['CostoUnit'])}
    for article in sorted(changed_costs):
        _lock_cost_history(cursor, article, history_date)
    for key, expected in sorted(changes.items()):
        order_id, bulk, article = key
        _write_one(cursor, 'UPDATE pedidosarticulos SET CantRecibida=%s,Saldada=%s,Usuario=%s,'
                   'Estacion=%s,FechaActualizacion=%s WHERE Pedido=%s AND PorMayor=%s AND Articulo=%s',
                   (expected['OrderReceived'], int(expected['Saldada']), *audit, order_id, int(bulk), article))
        _write_one(cursor, 'UPDATE artsservs SET CantDisponible=%s,CantPedida=%s,CostoUnit=%s,CostoUnitDet=%s,'
                    'FechaUltCompra=%s,UltCantComprada=%s,UltProveedor=%s,Usuario=%s,Estacion=%s,'
                    'FechaActualizacion=%s WHERE Articulo=%s',
                   (expected['CantDisponible'], expected['GlobalOrdered'], expected['CostoUnit'],
                    expected['CostoUnitDet'], expected['CatalogLastPurchase'], expected['CatalogLastQty'],
                    expected['UltProveedor'], *audit, article))
        _write_one(cursor, 'UPDATE articulosbodegas SET Cant=%s,FechaUltIngreso=%s,'
                   'Usuario=%s,Estacion=%s,FechaActualizacion=%s WHERE Articulo=%s AND Bodega=%s',
                   (expected['Cant'], expected['FechaUltIngreso'], *audit, article, receipt['warehouse']))
        _write_one(cursor, 'UPDATE articulosproveedor SET CantPedida=%s,FechaUltCompra=%s,UltCantComprada=%s,'
                   'Usuario=%s,Estacion=%s,FechaActualizacion=%s WHERE Articulo=%s AND Proveedor=%s',
                   (expected['SupplierOrdered'], expected['SupplierLastPurchase'], expected['SupplierLastQty'],
                    *audit, article, receipt['vendor_code']))
        if article in changed_costs:
            _save_cost_history(cursor, article, history_date, changed_costs[article], audit)

    after = _order_rows(cursor, receipt)
    if after.keys() != before.keys():
        raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_READBACK_MISMATCH')
    for key, old in before.items():
        actual = after[key]
        expected = dict(old)
        if key in changes:
            expected.update(changes[key])
            actual = {field: value for field, value in actual.items() if field not in AUTO_TIMESTAMPS}
            expected = {field: value for field, value in expected.items() if field not in AUTO_TIMESTAMPS}
            actual['Saldada'] = _native_bit(actual['Saldada'])
            for field in ('CostoUnit', 'CostoUnitDet'):
                actual[field] = _cost(actual[field])
            for prefix in ('Order', 'Catalog', 'Warehouse', 'Supplier'):
                expected.update({prefix + 'User': audit_user, prefix + 'Station': audit_station,
                                 prefix + 'Updated': applied_at})
        if actual != expected:
            raise PurchaseEffectsError('SGA_RECEIPT_INVENTORY_READBACK_MISMATCH')
    return {key: values['CostoUnit'] for key, values in changes.items()}
