"""Bounded, SELECT-only SGA purchase-order snapshots.

The source preserves native business flags. It neither creates a purchase order
nor equates printing/processing a document with receiving its merchandise.
"""
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .catalog_source import CatalogSource, text

CONTRACT = 1
MAX_ID = 2147483647
MAX_BATCH_LINES = 5000
DISCOVERY_SCOPE = frozenset({'vendor_code', 'vendor_tax_id', 'order_id', 'article_ids'})
DISCOVERY_LINE_FIELDS = ('article_id', 'article_code', 'name', 'remaining', 'remaining_base', 'unit')


def valid_discovery_query(scope, limit, after):
    if not isinstance(scope, dict) or set(scope) != DISCOVERY_SCOPE:
        return False
    vendor_code, tax_id = scope['vendor_code'], scope['vendor_tax_id']
    order_id, articles = scope['order_id'], scope['article_ids']
    return (
        (vendor_code is None or isinstance(vendor_code, str) and 1 <= len(vendor_code) <= 3
         and vendor_code == vendor_code.strip() and all(ord(char) >= 32 for char in vendor_code))
        and (tax_id is None or vendor_code is None and isinstance(tax_id, str)
             and re.fullmatch(r'[A-Z0-9]{1,40}', tax_id) is not None)
        and (order_id is None or type(order_id) is int and 1 <= order_id <= MAX_ID and after == 0)
        and isinstance(articles, list) and len(articles) <= 100
        and all(type(article) is int and 1 <= article <= MAX_ID for article in articles)
        and articles == sorted(set(articles))
        and type(limit) is int and 1 <= limit <= 100
        and type(after) is int and 0 <= after <= MAX_ID
    )


def _vendor_tax_id(value):
    return re.sub(r'[- ./]', '', text(value).upper())


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
        headers = self._headers(clauses, params, limit)
        has_more = len(headers) > limit
        headers = headers[:limit]
        ids = self._header_ids(headers, vendor_code, order_id)
        raw_lines = self._raw_lines(ids)
        if len(raw_lines) > MAX_BATCH_LINES:
            raise PurchaseSourceError('SGA_PURCHASE_SEARCH_TOO_BROAD')
        orders = self._orders(headers, ids, raw_lines)
        return {'contract': CONTRACT, 'status': 'found' if orders else 'not_found',
            'source_instance': self.config.get('instance') or self.config.get('source_instance'),
            'scope': {'vendor_code': vendor_code, 'date_from': start.isoformat() if start else None,
                'date_to': end.isoformat() if end else None, 'order_id': order_id,
                'include_closed': include_closed},
            'orders': orders, 'cursor_after': ids[-1] if ids else after,
            # A terminal empty *later page* cannot certify that the initial
            # search found nothing. A caller aggregating all pages owns that proof.
            'has_more': has_more, 'search_complete': not has_more and not after}

    def discover(self, vendor_code, vendor_tax_id, order_id, article_ids, *, limit, after=0):
        """List importable native orders using a read-only snapshot and keyset cursor."""
        scope = {'vendor_code': vendor_code, 'vendor_tax_id': vendor_tax_id,
                 'order_id': order_id, 'article_ids': article_ids}
        if not valid_discovery_query(scope, limit, after):
            raise PurchaseSourceError('INVALID_PURCHASE_DISCOVERY_REQUEST')
        self._currency(None)

        clauses, params = ['p.Pedido>0', 'v.Activo<>0'], []
        if vendor_code is not None:
            clauses.append('p.Proveedor=%s')
            params.append(vendor_code)
        if vendor_tax_id is not None:
            # Plain REPLACE works on the deployed MariaDB 5.5.
            clauses.append("REPLACE(REPLACE(REPLACE(REPLACE(UPPER(TRIM(v.Cedula)),"
                           "'-', ''), ' ', ''), '.', ''), '/', '')=%s")
            params.append(vendor_tax_id)
        if order_id is not None:
            clauses.append('p.Pedido=%s')
            params.append(order_id)
        if after:
            clauses.append('p.Pedido<%s')
            params.append(after)

        pending = (
            'EXISTS (SELECT 1 FROM pedidosarticulos pa JOIN artsservs a'
            ' ON a.Articulo=pa.Articulo WHERE pa.Pedido=p.Pedido AND a.Trabajado<>0'
            ' AND pa.Saldada=0 AND pa.CantPedida>pa.CantRecibida'
        )
        if article_ids:
            pending += ' AND pa.Articulo IN (' + ','.join(['%s'] * len(article_ids)) + ')'
            params.extend(article_ids)
        clauses.append(pending + ')')

        headers = self._headers(clauses, params, limit, include_tax_id=True)
        has_more = len(headers) > limit
        headers = headers[:limit]
        ids = self._header_ids(headers, vendor_code, order_id, after=after)
        if vendor_tax_id is not None and any(_vendor_tax_id(row['Cedula']) != vendor_tax_id
                                              for row in headers):
            raise PurchaseSourceError('SGA_PURCHASE_VENDOR_SCOPE_MISMATCH')
        raw_lines = self._raw_lines(ids)
        cursor = ids[-1] if ids else after
        if len(raw_lines) > MAX_BATCH_LINES:
            # The sentinel belongs to the first incomplete order. Leave that
            # order for the next page unless it exceeds the exact import limit.
            incomplete = _integer(raw_lines[-1]['Pedido'], minimum=1)
            if incomplete not in ids:
                raise PurchaseSourceError('SGA_PURCHASE_SOURCE_LINE_IDENTITY_INVALID')
            complete_ids = [native_id for native_id in ids if native_id > incomplete]
            if complete_ids:
                cursor = complete_ids[-1]
                has_more = True
            else:
                cursor = incomplete
                has_more = has_more or len(ids) > 1
            ids = complete_ids
            raw_lines = [row for row in raw_lines if row['Pedido'] > incomplete]
        rows = {row['Pedido']: row for row in headers if row['Pedido'] in ids}
        articles = set(article_ids)
        orders = []
        for order in self._orders(rows.values(), ids, raw_lines, skip_invalid=True):
            if (not order['eligible'] or order['currency_code'] != 'CRC'
                    or any(line['por_mayor'] and line['unit_factor'] != 1 for line in order['lines'])):
                continue
            pending = [line for line in order['lines']
                       if line['worked'] and not line['saldada'] and line['remaining'] > 0]
            lines = [
                {key: line[key] for key in DISCOVERY_LINE_FIELDS}
                for line in pending if not articles or line['article_id'] in articles
            ]
            if lines:
                orders.append({
                    'order_id': order['order_id'],
                    'vendor_code': order['vendor_code'],
                    'vendor_name': order['vendor_name'],
                    'vendor_tax_id': text(rows[order['order_id']]['Cedula']) or None,
                    'date_order': order['date_order'],
                    'currency_code': order['currency_code'],
                    'article_ids': sorted({line['article_id'] for line in order['lines']}),
                    'pending_article_ids': sorted({line['article_id'] for line in pending}),
                    'lines': lines,
                })
        return {'contract': CONTRACT, 'status': 'found' if orders else 'not_found',
                'source_instance': self.config.get('instance') or self.config.get('source_instance'),
                'scope': scope, 'orders': orders, 'cursor_after': cursor,
                'has_more': has_more, 'search_complete': not has_more and not after}

    def _headers(self, clauses, params, limit, *, include_tax_id=False):
        return self.rows(
            'SELECT p.Pedido,p.Proveedor,v.ProveedorNomb,v.Activo AS VendorActive,'
            'p.FechaPedido,p.FechaAprobacion,p.DiasEntrega,p.Comentario,p.Incoterms,'
            'p.Impreso,p.Procesado,p.Reorden,p.Retirar,p.Usuario,p.Estacion,'
            'p.FechaActualizacion,p.PGTS,vi.Moneda' + (',v.Cedula' if include_tax_id else '') +
            ' FROM pedidos p LEFT JOIN proveedores v ON v.Proveedor=p.Proveedor '
            'LEFT JOIN proveedoresimport vi ON vi.Proveedor=p.Proveedor WHERE '
            + ' AND '.join(clauses) + ' ORDER BY p.Pedido DESC LIMIT %s', (*params, limit + 1))

    def _header_ids(self, headers, vendor_code, order_id, *, after=0):
        ids = [_integer(row['Pedido'], minimum=1) for row in headers]
        if (len(set(ids)) != len(ids) or ids != sorted(ids, reverse=True)
                or after and any(n >= after for n in ids)):
            raise PurchaseSourceError('SGA_PURCHASE_SOURCE_ORDER_IDENTITY_INVALID')
        if vendor_code is not None and any(text(row['Proveedor']) != vendor_code for row in headers):
            raise PurchaseSourceError('SGA_PURCHASE_VENDOR_SCOPE_MISMATCH')
        if order_id is not None and any(n != order_id for n in ids):
            raise PurchaseSourceError('SGA_PURCHASE_ORDER_SCOPE_MISMATCH')
        return ids

    def _raw_lines(self, ids):
        if not ids:
            return []
        return self.rows(
            'SELECT pa.Pedido,pa.PorMayor,pa.Articulo,pa.CantPedida,pa.CantRecibida,'
            'pa.Saldada,pa.UltCostoCotizado,pa.Consignacion,pa.FechaActualizacion AS LineUpdatedAt,'
            'pa.PGTS AS LinePGTS,a.ArticuloNomb,a.NombreCorto,a.NumeroUnidades,a.Unidad,a.UnidadDet,'
            # A missing catalog row must not disappear from the native snapshot.
            'a.Trabajado FROM pedidosarticulos pa LEFT JOIN artsservs a ON a.Articulo=pa.Articulo '
            'WHERE pa.Pedido IN (' + ','.join(['%s'] * len(ids)) + ') '
            f'ORDER BY pa.Pedido DESC,pa.PorMayor,pa.Articulo LIMIT {MAX_BATCH_LINES + 1}', tuple(ids))

    def _orders(self, headers, ids, raw_lines, *, skip_invalid=False):
        groups = {order: [] for order in ids}
        invalid = set()
        seen = set()
        for row in raw_lines:
            order_id = _integer(row['Pedido'], minimum=1)
            if order_id not in groups:
                raise PurchaseSourceError('SGA_PURCHASE_SOURCE_LINE_IDENTITY_INVALID')
            try:
                line = self._line(row)
            except PurchaseSourceError:
                if not skip_invalid:
                    raise
                invalid.add(order_id)
                continue
            key = tuple(line['line_key'])
            if key in seen:
                raise PurchaseSourceError('SGA_PURCHASE_SOURCE_LINE_IDENTITY_INVALID')
            seen.add(key)
            groups[order_id].append(line)
        orders = []
        for row in headers:
            order_id = row['Pedido']
            if order_id in invalid:
                continue
            lines = sorted(groups[order_id], key=lambda line: (line['por_mayor'], line['article_id']))
            try:
                orders.append(self._order(row, lines))
            except PurchaseSourceError:
                if not skip_invalid:
                    raise
        return orders
