"""Bounded, SELECT-only SGA purchase-order snapshots.

The source preserves native business flags. It neither creates a purchase order
nor equates printing/processing a document with receiving its merchandise.
"""
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .catalog_source import CatalogSource, text

CONTRACT = 1
MAX_ID = 2147483647
MAX_BATCH_LINES = 5000


class PurchaseSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _integer(value, *, minimum=-MAX_ID-1, maximum=MAX_ID, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise PurchaseSourceError('SGA_PURCHASE_SOURCE_INTEGER_INVALID')
    return value


def _bit(value):
    if isinstance(value, bytes) and len(value) == 1:
        value = int.from_bytes(value, 'big')
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise PurchaseSourceError('SGA_PURCHASE_SOURCE_BIT_INVALID')


def _iso(value):
    # Native datetimes deliberately have no invented UTC offset.
    if value is None:
        return None
    if not isinstance(value, (date, datetime)):
        raise PurchaseSourceError('SGA_PURCHASE_SOURCE_DATE_INVALID')
    return value.isoformat()


def _money(value):
    if type(value) not in (int, float, Decimal) or not math.isfinite(value):
        raise PurchaseSourceError('SGA_PURCHASE_SOURCE_COST_INVALID')
    result = Decimal(str(value))
    if abs(result) > Decimal('999999999999999.9999'):
        raise PurchaseSourceError('SGA_PURCHASE_SOURCE_COST_INVALID')
    try:
        result = result.quantize(Decimal('.0001'), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise PurchaseSourceError('SGA_PURCHASE_SOURCE_COST_INVALID') from None
    return float(result)


def _search_date(value):
    if value is None:
        return None
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise PurchaseSourceError('INVALID_PURCHASE_SEARCH_DATE')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise PurchaseSourceError('INVALID_PURCHASE_SEARCH_DATE') from None
    if result.isoformat() != value:
        raise PurchaseSourceError('INVALID_PURCHASE_SEARCH_DATE')
    return result


class PurchaseSource(CatalogSource):
    """Use with CatalogSource's read-only grant guard and consistent snapshot.

    Optional config: ``national_currency_code`` is the *verified native* result
    of PGMonNac; ``currency_map`` maps native codes to ISO codes. Neither defaults
    to CRC or to the Odoo company currency. No native VBA function is executed.
    """

    def _currency(self, native):
        native = text(native) or None
        national = self.config.get('national_currency_code')
        mapping = self.config.get('currency_map', {})
        if (not isinstance(mapping, dict) or any(not isinstance(k, str) or not k
                or not isinstance(v, str) or len(v) != 3 or not v.isascii()
                or not v.isalpha() or v != v.upper() for k, v in mapping.items())
                or national is not None and (not isinstance(national, str) or not national.strip())):
            raise PurchaseSourceError('INVALID_PURCHASE_CURRENCY_MAPPING')
        effective = native or (national.strip() if national else None)
        resolved = mapping.get(effective)
        return {'currency_native': native, 'currency_code': resolved,
            'currency_status': 'resolved' if resolved else 'unresolved',
            'currency_source': ('supplier_import' if native else 'national_configuration')
                               if effective else 'unresolved'}

    def _line(self, row):
        order = _integer(row['Pedido'], minimum=1)
        article = _integer(row['Articulo'], minimum=1)
        pm = _bit(row['PorMayor'])
        ordered = _integer(row['CantPedida'])
        received = _integer(row['CantRecibida'])
        factor = _integer(row['NumeroUnidades'], minimum=-32768, maximum=32767)
        closed = _bit(row['Saldada'])
        worked = _integer(row['Trabajado'], minimum=-32768, maximum=32767)
        quote = _money(row['UltCostoCotizado'])
        remaining = ordered - received
        issues = []
        if factor <= 0:
            issues.append('invalid_unit_factor')
        if ordered < 0 or received < 0:
            issues.append('negative_quantity')
        if received > ordered:
            issues.append('over_received')
        if closed and remaining > 0:
            issues.append('closed_with_remaining')
        if not closed and ordered != 0 and remaining <= 0:
            issues.append('open_without_remaining')
        if not worked and ordered != 0 and remaining > 0:
            issues.append('article_not_worked')
        if quote < 0:
            issues.append('negative_quote')
        multiplier = factor if pm else 1
        return {'line_key': [order, pm, article], 'article_id': article,
            'article_code': str(article).zfill(6), 'name': text(row['ArticuloNomb']),
            'reference': text(row['NombreCorto']) or None, 'por_mayor': pm,
            'unit_factor': factor, 'unit': text(row['Unidad']), 'unit_detail': text(row['UnidadDet']),
            'ordered': ordered, 'received': received, 'remaining': remaining,
            'ordered_base': ordered * multiplier if factor > 0 else None,
            'received_base': received * multiplier if factor > 0 else None,
            'remaining_base': remaining * multiplier if factor > 0 else None,
            'saldada': closed, 'quoted_cost': quote, 'consignment': _bit(row['Consignacion']),
            'worked': worked, 'source_updated_at': _iso(row['LineUpdatedAt']),
            'source_timestamp': _iso(row['LinePGTS']), 'issues': issues}

    def _order(self, row, lines):
        order_id = _integer(row['Pedido'], minimum=1)
        issues = []
        currency = self._currency(row['Moneda'])
        if currency['currency_status'] != 'resolved':
            issues.append('currency_unresolved')
        if row['ProveedorNomb'] is None:
            issues.append('vendor_missing')
        elif not _bit(row['VendorActive']):
            issues.append('vendor_inactive')
        if not lines:
            issues.append('order_has_no_lines')
        if any(line['issues'] for line in lines):
            issues.append('line_requires_review')
        pending = any(line['worked'] and not line['saldada'] and line['remaining'] > 0
                      for line in lines)
        if not pending:
            issues.append('no_pending_quantity')
        result = {'order_id': order_id, 'vendor_code': text(row['Proveedor']),
            'vendor_name': text(row['ProveedorNomb']), 'date_order': _iso(row['FechaPedido']),
            'date_approval': _iso(row['FechaAprobacion']),
            'delivery_days': _integer(row['DiasEntrega'], minimum=-32768, maximum=32767),
            'incoterms': text(row['Incoterms']) or None, 'comment': text(row['Comentario']) or None,
            'printed': _bit(row['Impreso']), 'processed': _bit(row['Procesado']),
            'reorder': _bit(row['Reorden']),
            # The native Retirar flag is preserved; it does not mean cancelled.
            'withdraw': _bit(row['Retirar']), **currency,
            'source_actor': text(row['Usuario']) or None,
            'source_station': _integer(row['Estacion'], minimum=-32768, maximum=32767, nullable=True),
            'source_updated_at': _iso(row['FechaActualizacion']), 'source_timestamp': _iso(row['PGTS']),
            'eligible': pending and not issues, 'issues': issues, 'lines': lines}
        result['checksum'] = _digest(result)
        return result

    def search(self, vendor_code, *, date_from=None, date_to=None, order_id=None,
               after=0, limit=20, include_closed=False):
        """Return highest Pedido IDs first; ``after`` is the last native ID seen.

        Explicit identity lookup ignores the heuristic dates and pending filter,
        so an old/closed order is not misreported as absent. Implicit lookup keeps
        contradictory open/closed balances visible for review. An exception is
        never converted to not_found. not_found is scoped to the returned filters.
        """
        if (not isinstance(vendor_code, str) or not 1 <= len(vendor_code.strip()) <= 3
                or any(ord(c) < 32 for c in vendor_code)
                or type(limit) is not int or not 1 <= limit <= 100
                or type(after) is not int or not 0 <= after <= MAX_ID
                or type(include_closed) is not bool
                or order_id is not None and (type(order_id) is not int or not 1 <= order_id <= MAX_ID)
                or order_id is not None and after):
            raise PurchaseSourceError('INVALID_PURCHASE_SEARCH')
        vendor_code = vendor_code.strip()
        start, end = _search_date(date_from), _search_date(date_to)
        if start and end and start > end:
            raise PurchaseSourceError('INVALID_PURCHASE_SEARCH_DATE_RANGE')
        # Validate currency configuration before any business read.
        self._currency(None)
        clauses = ['p.Proveedor=%s', 'p.Pedido>0']
        params = [vendor_code]
        if order_id is not None:
            clauses.append('p.Pedido=%s'); params.append(order_id)
        else:
            if after:
                clauses.append('p.Pedido<%s'); params.append(after)
            if start:
                clauses.append('p.FechaPedido>=%s'); params.append(start)
            if end:
                if end == date.max:
                    raise PurchaseSourceError('INVALID_PURCHASE_SEARCH_DATE')
                clauses.append('p.FechaPedido<%s'); params.append(end + timedelta(days=1))
            if not include_closed:
                # Keep both selections observed in SGA: arithmetic pending and
                # the Saldada-based pending list. Disagreements remain visible.
                clauses.append('EXISTS (SELECT 1 FROM pedidosarticulos pa JOIN artsservs a '
                    'ON a.Articulo=pa.Articulo WHERE pa.Pedido=p.Pedido AND a.Trabajado<>0 '
                    'AND (pa.CantPedida-pa.CantRecibida>0 OR (pa.Saldada=0 AND pa.CantPedida<>0)))')
        params.append(limit + 1)
        headers = self.rows('SELECT p.Pedido,p.Proveedor,v.ProveedorNomb,v.Activo AS VendorActive,'
            'p.FechaPedido,p.FechaAprobacion,p.DiasEntrega,p.Comentario,p.Incoterms,'
            'p.Impreso,p.Procesado,p.Reorden,p.Retirar,p.Usuario,p.Estacion,'
            'p.FechaActualizacion,p.PGTS,vi.Moneda FROM pedidos p '
            'LEFT JOIN proveedores v ON v.Proveedor=p.Proveedor '
            'LEFT JOIN proveedoresimport vi ON vi.Proveedor=p.Proveedor WHERE '
            + ' AND '.join(clauses) + ' ORDER BY p.Pedido DESC LIMIT %s', tuple(params))
        has_more = len(headers) > limit
        headers = headers[:limit]
        ids = [_integer(row['Pedido'], minimum=1) for row in headers]
        if len(set(ids)) != len(ids) or ids != sorted(ids, reverse=True):
            raise PurchaseSourceError('SGA_PURCHASE_SOURCE_ORDER_IDENTITY_INVALID')
        if any(text(row['Proveedor']) != vendor_code for row in headers):
            raise PurchaseSourceError('SGA_PURCHASE_VENDOR_SCOPE_MISMATCH')
        if order_id is not None and any(n != order_id for n in ids):
            raise PurchaseSourceError('SGA_PURCHASE_ORDER_SCOPE_MISMATCH')
        groups = {order: [] for order in ids}
        if ids:
            raw_lines = self.rows('SELECT pa.Pedido,pa.PorMayor,pa.Articulo,pa.CantPedida,pa.CantRecibida,'
                'pa.Saldada,pa.UltCostoCotizado,pa.Consignacion,pa.FechaActualizacion AS LineUpdatedAt,'
                'pa.PGTS AS LinePGTS,a.ArticuloNomb,a.NombreCorto,a.NumeroUnidades,a.Unidad,a.UnidadDet,'
                # A missing catalog row must fail validation, not silently erase
                # a native detail from a supposedly complete order snapshot.
                'a.Trabajado FROM pedidosarticulos pa LEFT JOIN artsservs a ON a.Articulo=pa.Articulo '
                'WHERE pa.Pedido IN (' + ','.join(['%s'] * len(ids)) + ') '
                'ORDER BY pa.Pedido DESC,pa.PorMayor,pa.Articulo LIMIT 5001', tuple(ids))
            if len(raw_lines) > MAX_BATCH_LINES:
                raise PurchaseSourceError('SGA_PURCHASE_SEARCH_TOO_BROAD')
            seen = set()
            for row in raw_lines:
                line = self._line(row)
                key = tuple(line['line_key'])
                if key in seen or key[0] not in groups:
                    raise PurchaseSourceError('SGA_PURCHASE_SOURCE_LINE_IDENTITY_INVALID')
                seen.add(key); groups[key[0]].append(line)
        orders = [self._order(row, sorted(groups[row['Pedido']],
                  key=lambda line: (line['por_mayor'], line['article_id']))) for row in headers]
        return {'contract': CONTRACT, 'status': 'found' if orders else 'not_found',
            'source_instance': self.config.get('instance') or self.config.get('source_instance'),
            'scope': {'vendor_code': vendor_code, 'date_from': start.isoformat() if start else None,
                'date_to': end.isoformat() if end else None, 'order_id': order_id,
                'include_closed': include_closed},
            'orders': orders, 'cursor_after': ids[-1] if ids else after,
            # A terminal empty *later page* cannot certify that the initial
            # search found nothing. A caller aggregating all pages owns that proof.
            'has_more': has_more, 'search_complete': not has_more and not after}
