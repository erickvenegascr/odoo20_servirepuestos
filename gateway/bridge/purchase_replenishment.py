"""Read-only, reviewable SGA replenishment selection; no order creation.

The below-minimum predicate comes from the recovered q703 list. It is not the
q556/q557 automatic-order algorithm: available stock is not Facturable, and the
list neither calculates order quantities nor recalculates ABC/minimums.
"""
from decimal import Decimal

from .catalog_source import CatalogSource, text

CONTRACT = 1
MAX_ID = 2147483647


class ReplenishmentSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _integer(value, minimum=-MAX_ID-1, maximum=MAX_ID, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReplenishmentSourceError('SGA_REPLENISHMENT_INTEGER_INVALID')
    return value


def _bit(value):
    if isinstance(value, bytes) and len(value) == 1:
        value = int.from_bytes(value, 'big')
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ReplenishmentSourceError('SGA_REPLENISHMENT_BIT_INVALID')


def _vendor(value, nullable=False):
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 3
            or any(ord(c) < 32 for c in value)):
        raise ReplenishmentSourceError('INVALID_REPLENISHMENT_VENDOR')
    return value.strip()


def _cursor(value):
    if value is None or type(value) is int and value == 0:
        return None
    if type(value) is not dict or set(value) != {'ranking', 'article_id'}:
        raise ReplenishmentSourceError('INVALID_REPLENISHMENT_CURSOR')
    return {'ranking': _integer(value['ranking'], -32768, 32767, nullable=True),
            'article_id': _integer(value['article_id'], 1)}


def _sort_key(value):
    # MySQL ascending order puts NULL before non-NULL ranks.
    return (value['ranking'] is not None, value['ranking'] or 0, value['article_id'])


class PurchaseReplenishmentSource(CatalogSource):
    """A bounded SELECT-only list requiring explicit native selection context.

    ``replenishment_worked`` supplies the verified PGVariabTrae("trabajados")
    selection. ``retained`` or ``replenishment_retained`` supplies the boolean
    selection returned by PGBool(PGVariabTrae("retenidos")). Missing context
    blocks rather than inventing a 255/False default.

    ``replenishment_query_verified`` may be True only after checking the live
    q703 SQL against the recovered predicate. False leaves candidates reviewable.
    """

    def _supplier(self, rows, vendor_code):
        if not rows:
            return None, ['supplier_missing']
        first_priority = min(row['priority'] for row in rows)
        preferred = [row for row in rows if row['priority'] == first_priority]
        if len(preferred) != 1:
            return None, ['supplier_priority_ambiguous']
        supplier = preferred[0]
        issues = []
        if supplier['disabled']:
            issues.append('supplier_relation_disabled')
        if not supplier['exists']:
            issues.append('supplier_missing')
        elif not supplier['active']:
            issues.append('supplier_inactive')
        if vendor_code and supplier['vendor_code'] != vendor_code:
            issues.append('supplier_scope_mismatch')
        # Never choose the next priority to bypass a disabled/inactive winner.
        return supplier, issues

    def _candidate(self, row, suppliers, scope, query_verified):
        article = _integer(row['Articulo'], 1)
        factor = _integer(row['NumeroUnidades'], -32768, 32767)
        major = _integer(row['CantDisponible'])
        detail = _integer(row['CantDisponibleDet'], -32768, 32767)
        minimum = _integer(row['CantMin'])
        maximum = _integer(row['CantMax'])
        worked = _integer(row['Trabajado'], -32768, 32767)
        retained = _bit(row['Retenido'])
        ranking = _integer(row['Ranking'], -32768, 32767, nullable=True)
        abc = text(row['Clasificacion']) or None
        if abc is not None and len(abc) != 1:
            raise ReplenishmentSourceError('SGA_REPLENISHMENT_ABC_INVALID')
        if worked != scope['worked_native'] or retained != scope['retained'] or (
                scope['abc'] is not None and abc != scope['abc']):
            raise ReplenishmentSourceError('SGA_REPLENISHMENT_SCOPE_MISMATCH')
        # Zero factors produce NULL in the SQL predicate and cannot be candidates.
        # A contradictory mock/native result is an incomplete source, not absence.
        if factor == 0:
            raise ReplenishmentSourceError('SGA_REPLENISHMENT_FACTOR_INVALID')
        normalized = Decimal(major) + Decimal(detail) / Decimal(factor)
        if normalized >= minimum:
            raise ReplenishmentSourceError('SGA_REPLENISHMENT_PREDICATE_MISMATCH')
        no_order = _bit(row['NoPedir'])
        service = _bit(row['Servicio'])
        issues = []
        if factor < 0:
            issues.append('invalid_unit_factor')
        if no_order:
            issues.append('article_no_order')
        if service:
            issues.append('article_service')
        if not worked:
            issues.append('article_not_worked')
        if retained:
            issues.append('article_retained')
        if maximum < minimum:
            issues.append('maximum_below_minimum')
        if not query_verified:
            issues.append('native_query_version_unverified')
        supplier, supplier_issues = self._supplier(suppliers, scope['vendor_code'])
        issues.extend(supplier_issues)
        return {'article_id': article, 'article_code': str(article).zfill(6),
            'name': text(row['ArticuloNomb']), 'abc_native': abc, 'ranking_native': ranking,
            'unit_factor': factor, 'unit': text(row['Unidad']), 'unit_detail': text(row['UnidadDet']),
            'available_major': major, 'available_detail': detail,
            'available_base': major * factor + detail if factor > 0 else None,
            'available_major_normalized': float(normalized) if factor > 0 else None,
            'minimum_major': minimum, 'maximum_major': maximum, 'below_minimum': True,
            'shortfall_to_minimum_major': float(Decimal(minimum) - normalized) if factor > 0 else None,
            'suggested_major': None,
            'flags': {'worked': worked, 'retained': retained, 'no_order': no_order,
                      'service': service, 'consignment': _bit(row['Consignacion']),
                      'maintain_minmax': _bit(row['MantieneMinMax'])},
            'supplier': supplier, 'supplier_candidates': suppliers,
            'eligible': not issues, 'issues': issues}

    def suggest(self, vendor_code=None, *, strategy='bajo_minimo', abc=None,
                retained=None, after=0, limit=80):
        vendor_code = _vendor(vendor_code, nullable=True)
        if (strategy not in ('bajo_minimo', 'minmax_auto') or type(limit) is not int
                or not 1 <= limit <= 100 or retained is not None and type(retained) is not bool
                or abc is not None and (not isinstance(abc, str) or len(abc) != 1
                                       or not abc.isascii() or not abc.isalpha())):
            raise ReplenishmentSourceError('INVALID_REPLENISHMENT_SEARCH')
        if abc is not None:
            abc = abc.upper()
        cursor = _cursor(after)
        worked = self.config.get('replenishment_worked')
        selected_retained = retained if retained is not None else self.config.get('replenishment_retained')
        query_verified = self.config.get('replenishment_query_verified', False)
        if type(query_verified) is not bool:
            raise ReplenishmentSourceError('INVALID_REPLENISHMENT_QUERY_VERIFICATION')
        if worked is not None:
            worked = _integer(worked, -32768, 32767)
        if selected_retained is not None and type(selected_retained) is not bool:
            raise ReplenishmentSourceError('INVALID_REPLENISHMENT_RETAINED_SELECTION')
        scope = {'vendor_code': vendor_code, 'strategy': strategy, 'abc': abc,
                 'retained': selected_retained, 'worked_native': worked}
        result = {'contract': CONTRACT, 'status': 'blocked', 'strategy': strategy,
            'source_instance': self.config.get('instance') or self.config.get('source_instance'),
            'scope': scope, 'candidates': [], 'cursor_after': cursor,
            'has_more': False, 'search_complete': False, 'blockers': [],
            'evidence': {'selection_query': 'SGAProg /queries/703 Ver Lista ArtsServs Debajo Minimo',
                'native_query_version_verified': query_verified,
                'predicate': 'CantDisponible + CantDisponibleDet / NumeroUnidades < CantMin',
                'ordering': ['Ranking ASC (NULL first)', 'Articulo ASC (stable tie-breaker)'],
                'availability_basis': 'raw_available_normalized_to_major',
                'includes_ordered_or_committed_or_backorder': False,
                'abc_basis': 'stored Clasificacion and Ranking; no ABC recalculation',
                'quantity_basis': 'selection only; no order quantity inferred',
                'supplier_basis': 'q557 minimum positive priority, unique winner only; q556 active/enabled guards',
                'differences_from_automatic': ['q703 uses < minimum; q556 uses <= on a different existence',
                    'q556/q557 require Facturable, pending orders, BackOrder and Int',
                    'NoPedir/Servicio/supplier exclusions guard eligibility without hiding q703 candidates']}}
        if strategy == 'minmax_auto':
            result['blockers'] = ['SGA_NATIVE_FACTURABLE_UNVERIFIED', 'SGA_NATIVE_BACKORDER_UNVERIFIED']
            return result
        if worked is None:
            result['blockers'].append('SGA_NATIVE_WORKED_SELECTION_REQUIRED')
        if selected_retained is None:
            result['blockers'].append('SGA_NATIVE_RETAINED_SELECTION_REQUIRED')
        if result['blockers']:
            return result
        clauses = ['a.Articulo>0', 'a.Trabajado=%s', 'a.Retenido=%s',
            '(a.CantDisponible+a.CantDisponibleDet/NULLIF(a.NumeroUnidades,0))<a.CantMin']
        params = [worked, int(selected_retained)]
        if abc is not None:
            clauses.append('a.Clasificacion=%s'); params.append(abc)
        if cursor:
            if cursor['ranking'] is None:
                clauses.append('(a.Ranking IS NOT NULL OR (a.Ranking IS NULL AND a.Articulo>%s))')
                params.append(cursor['article_id'])
            else:
                clauses.append('(a.Ranking>%s OR (a.Ranking=%s AND a.Articulo>%s))')
                params.extend([cursor['ranking'], cursor['ranking'], cursor['article_id']])
        if vendor_code:
            clauses.append('EXISTS (SELECT 1 FROM articulosproveedor ap WHERE ap.Articulo=a.Articulo '
                'AND ap.Proveedor=%s AND ap.Prioridad>0 AND ap.Prioridad=(SELECT MIN(ap2.Prioridad) '
                'FROM articulosproveedor ap2 WHERE ap2.Articulo=a.Articulo AND ap2.Prioridad>0))')
            params.append(vendor_code)
        params.append(limit + 1)
        rows = self.rows('SELECT a.Articulo,a.ArticuloNomb,a.NumeroUnidades,a.Unidad,a.UnidadDet,'
            'a.CantDisponible,a.CantDisponibleDet,a.CantMin,a.CantMax,a.Clasificacion,a.Ranking,'
            'a.Trabajado,a.Retenido,a.NoPedir,a.Servicio,a.Consignacion,a.MantieneMinMax '
            'FROM artsservs a WHERE ' + ' AND '.join(clauses) + ' ORDER BY a.Ranking,a.Articulo LIMIT %s', tuple(params))
        has_more = len(rows) > limit
        rows = rows[:limit]
        identities = [{'ranking': _integer(row['Ranking'], -32768, 32767, nullable=True),
                       'article_id': _integer(row['Articulo'], 1)} for row in rows]
        keys = [_sort_key(identity) for identity in identities]
        if (keys != sorted(keys) or len({v['article_id'] for v in identities}) != len(identities)
                or cursor and any(key <= _sort_key(cursor) for key in keys)):
            raise ReplenishmentSourceError('SGA_REPLENISHMENT_IDENTITY_INVALID')
        ids = [identity['article_id'] for identity in identities]
        suppliers = {article: [] for article in ids}
        if ids:
            supplier_rows = self.rows('SELECT ap.Articulo,ap.Proveedor,ap.Prioridad,ap.Deshabilitado,'
                'p.ProveedorNomb,p.Activo FROM articulosproveedor ap '
                'LEFT JOIN proveedores p ON p.Proveedor=ap.Proveedor WHERE ap.Articulo IN ('
                + ','.join(['%s'] * len(ids)) + ') AND ap.Prioridad>0 '
                'ORDER BY ap.Articulo,ap.Prioridad,ap.Proveedor', tuple(ids))
            seen = set()
            for row in supplier_rows:
                article = _integer(row['Articulo'], 1)
                vendor = _vendor(text(row['Proveedor']))
                if article not in suppliers or (article, vendor) in seen:
                    raise ReplenishmentSourceError('SGA_REPLENISHMENT_SUPPLIER_IDENTITY_INVALID')
                seen.add((article, vendor))
                exists = row['ProveedorNomb'] is not None
                suppliers[article].append({'vendor_code': vendor, 'name': text(row['ProveedorNomb']),
                    'priority': _integer(row['Prioridad'], 1, 32767),
                    'disabled': _bit(row['Deshabilitado']), 'exists': exists,
                    'active': _bit(row['Activo']) if exists else None})
        candidates = [self._candidate(row, suppliers[row['Articulo']], scope, query_verified) for row in rows]
        result.update(status='found' if candidates else 'not_found', candidates=candidates,
            cursor_after=identities[-1] if identities else cursor, has_more=has_more,
            search_complete=not has_more and cursor is None)
        return result
