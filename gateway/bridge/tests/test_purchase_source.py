import copy
from datetime import date, datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import MagicMock, patch

from bridge.catalog_source import SourceReadOnlyError
from bridge.purchase_source import PurchaseSource, PurchaseSourceError


class PurchaseSourceTests(TestCase):
    def header(self, order=42, **values):
        return {'Pedido': order, 'Proveedor': '019', 'ProveedorNomb': 'Synthetic supplier',
            'VendorActive': b'\x01', 'FechaPedido': datetime(2026, 11, 1, 10),
            'FechaAprobacion': None, 'DiasEntrega': 3, 'Comentario': None, 'Incoterms': None,
            'Impreso': b'\x01', 'Procesado': b'\x00', 'Reorden': b'\x00', 'Retirar': b'\x01',
            'Usuario': 'LAB', 'Estacion': 1, 'FechaActualizacion': datetime(2026, 11, 2),
            'PGTS': datetime(2026, 11, 2), 'Moneda': None, **values}

    def line(self, order=42, **values):
        return {'Pedido': order, 'PorMayor': b'\x01', 'Articulo': 7, 'CantPedida': 10,
            'CantRecibida': 4, 'Saldada': b'\x00', 'UltCostoCotizado': 100.1234,
            'Consignacion': b'\x00', 'LineUpdatedAt': datetime(2026, 11, 2),
            'LinePGTS': datetime(2026, 11, 2), 'ArticuloNomb': 'SYNTHETIC ARTICLE',
            'NombreCorto': 'LAB-REF', 'NumeroUnidades': 12, 'Unidad': 'Caja',
            'UnidadDet': 'Unidad', 'Trabajado': 255, **values}

    def source(self, *, headers=None, lines=None, **config):
        source = PurchaseSource({'instance': 'purchase-lab', 'national_currency_code': 'C',
                                 'currency_map': {'C': 'CRC', '$': 'USD'}, **config})
        source.rows = MagicMock(side_effect=[headers if headers is not None else [self.header()],
                                            lines if lines is not None else [self.line()]])
        return source

    def test_partial_order_preserves_native_and_base_units(self):
        source = self.source()
        result = source.search('019', date_from='2026-10-01', date_to='2026-11-18')
        order = result['orders'][0]; line = order['lines'][0]
        self.assertEqual(('found', False, True), (result['status'], result['has_more'], result['search_complete']))
        self.assertEqual(('019', 'CRC', 'national_configuration'),
                         (order['vendor_code'], order['currency_code'], order['currency_source']))
        self.assertEqual([42, True, 7], line['line_key'])
        self.assertEqual((10, 4, 6, 120, 48, 72),
            tuple(line[n] for n in ('ordered', 'received', 'remaining', 'ordered_base', 'received_base', 'remaining_base')))
        self.assertTrue(order['eligible']); self.assertTrue(order['withdraw'])
        self.assertEqual('000007', line['article_code'])
        sql, params = source.rows.call_args_list[0].args
        self.assertIn('p.Proveedor=%s', sql)
        self.assertEqual(('019', date(2026, 10, 1), date(2026, 11, 19), 21), params)
        self.assertIn('pa.Saldada=0', sql)
        for call in source.rows.call_args_list:
            self.assertTrue(call.args[0].startswith('SELECT ')); self.assertNotIn(';', call.args[0])
            self.assertNotIn('FOR UPDATE', call.args[0])
        self.assertIn('LEFT JOIN artsservs', source.rows.call_args_list[1].args[0])

    def test_retail_quantities_are_not_multiplied_by_pack_factor(self):
        source = self.source(lines=[self.line(PorMayor=b'\x00', CantPedida=7, CantRecibida=2)])
        line = source.search('019')['orders'][0]['lines'][0]
        self.assertEqual([42, False, 7], line['line_key'])
        self.assertEqual((7, 2, 5), (line['ordered_base'], line['received_base'], line['remaining_base']))

    def test_currency_is_never_guessed_from_odoo_or_native_label(self):
        source = self.source(headers=[self.header(Moneda='X')])
        order = source.search('019')['orders'][0]
        self.assertIsNone(order['currency_code']); self.assertEqual('X', order['currency_native'])
        self.assertEqual('unresolved', order['currency_status']); self.assertFalse(order['eligible'])
        source = self.source(national_currency_code=None, currency_map={})
        order = source.search('019')['orders'][0]
        self.assertIsNone(order['currency_code']); self.assertEqual('unresolved', order['currency_source'])
        source = self.source(headers=[self.header(Moneda='$')])
        order = source.search('019')['orders'][0]
        self.assertEqual(('USD', 'supplier_import'), (order['currency_code'], order['currency_source']))

    def test_explicit_identity_returns_closed_order_outside_date_window(self):
        source = self.source(lines=[self.line(CantRecibida=10, Saldada=b'\x01')])
        result = source.search('019', order_id=42, date_from='2026-11-10')
        self.assertEqual('found', result['status']); self.assertFalse(result['orders'][0]['eligible'])
        self.assertIn('no_pending_quantity', result['orders'][0]['issues'])
        sql, params = source.rows.call_args_list[0].args
        self.assertIn('p.Pedido=%s', sql); self.assertNotIn('EXISTS', sql)
        self.assertNotIn('p.FechaPedido>=', sql); self.assertEqual(('019', 42, 21), params)

    def test_closed_remaining_and_open_zero_balances_are_not_hidden(self):
        for row, issue in [(self.line(Saldada=b'\x01'), 'closed_with_remaining'),
                           (self.line(CantRecibida=10), 'open_without_remaining'),
                           (self.line(CantRecibida=11), 'over_received')]:
            with self.subTest(issue=issue):
                order = self.source(lines=[row]).search('019')['orders'][0]
                self.assertIn(issue, order['lines'][0]['issues']); self.assertFalse(order['eligible'])

    def test_zero_factor_is_preserved_for_review_not_divided_or_imported(self):
        order = self.source(lines=[self.line(NumeroUnidades=0)]).search('019')['orders'][0]
        self.assertIsNone(order['lines'][0]['ordered_base'])
        self.assertIn('invalid_unit_factor', order['lines'][0]['issues']); self.assertFalse(order['eligible'])
        with self.assertRaises(PurchaseSourceError):
            self.source(lines=[self.line(NumeroUnidades=None, Trabajado=None)]).search('019')

    def test_descending_page_cursor_does_not_claim_complete_search(self):
        source = self.source(headers=[self.header(42), self.header(41), self.header(40)],
                             lines=[self.line(42), self.line(41)])
        result = source.search('019', after=50, limit=2)
        self.assertEqual([42, 41], [o['order_id'] for o in result['orders']])
        self.assertEqual(41, result['cursor_after']); self.assertTrue(result['has_more'])
        self.assertFalse(result['search_complete'])
        self.assertIn('p.Pedido<%s', source.rows.call_args_list[0].args[0])
        self.assertEqual(('019', 50, 3), source.rows.call_args_list[0].args[1])
        self.assertEqual((42, 41), source.rows.call_args_list[1].args[1])

    def test_successful_empty_read_is_scoped_not_found_and_failures_propagate(self):
        source = self.source(headers=[])
        result = source.search('019')
        self.assertEqual(('not_found', True), (result['status'], result['search_complete']))
        self.assertEqual([], result['orders']); self.assertEqual(1, source.rows.call_count)
        source = self.source(); source.rows.side_effect = TimeoutError('synthetic connection loss')
        with self.assertRaises(TimeoutError): source.search('019')
        source = self.source(headers=[])
        self.assertFalse(source.search('019', after=41)['search_complete'])

    def test_bad_scope_or_types_never_execute_business_query(self):
        for values in [{'vendor_code': ''}, {'vendor_code': 'LONG'}, {'vendor_code': '019', 'limit': True},
                       {'vendor_code': '019', 'order_id': True}, {'vendor_code': '019', 'after': -1},
                       {'vendor_code': '019', 'order_id': 42, 'after': 41},
                       {'vendor_code': '019', 'date_from': '2026-11-20', 'date_to': '2026-11-01'}]:
            source = self.source()
            with self.subTest(values=values), self.assertRaises(PurchaseSourceError): source.search(**values)
            source.rows.assert_not_called()

    def test_mismatched_or_duplicate_native_identities_fail_closed(self):
        for headers, lines in [([self.header(Proveedor='020')], []),
                               ([self.header(), self.header()], []),
                               ([self.header()], [self.line(), self.line()]),
                               ([self.header()], [self.line(43)])]:
            with self.subTest(headers=headers, lines=lines), self.assertRaises(PurchaseSourceError):
                self.source(headers=headers, lines=lines).search('019')

    def test_invalid_native_cost_is_not_cast_into_a_quote(self):
        for value in [None, True, float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(PurchaseSourceError):
                self.source(lines=[self.line(UltCostoCotizado=value)]).search('019')
        self.assertEqual(100.1235, self.source(lines=[self.line(UltCostoCotizado=Decimal('100.12345'))])
                         .search('019')['orders'][0]['lines'][0]['quoted_cost'])

    def test_snapshot_hash_detects_child_change_and_is_order_independent(self):
        lines = [self.line(Articulo=8), self.line(Articulo=7)]
        a = self.source(lines=lines).search('019')['orders'][0]
        b = self.source(lines=list(reversed(lines))).search('019')['orders'][0]
        self.assertEqual(a['checksum'], b['checksum'])
        changed = copy.deepcopy(lines); changed[0]['CantRecibida'] += 1
        c = self.source(lines=changed).search('019')['orders'][0]
        self.assertNotEqual(a['checksum'], c['checksum'])
        self.assertEqual(64, len(a['checksum']))

    def discover(self, source, **filters):
        query = {'vendor_code': None, 'vendor_tax_id': None, 'order_id': None,
                 'article_ids': [], 'limit': 20}
        return source.discover(**{**query, **filters})

    def test_discovery_combines_vendor_and_articles_and_previews_only_pending_matches(self):
        source = self.source(
            headers=[self.header(Cedula='3-101-555555')],
            lines=[self.line(NumeroUnidades=1), self.line(Articulo=8, NumeroUnidades=1, CantRecibida=10, Saldada=b'\x01'),
                   self.line(Articulo=9, NumeroUnidades=1)],
        )
        result = self.discover(source, vendor_code='019', article_ids=[7, 8])
        self.assertEqual('found', result['status'])
        self.assertEqual([{
            'order_id': 42, 'vendor_code': '019', 'vendor_name': 'Synthetic supplier',
            'vendor_tax_id': '3-101-555555', 'date_order': '2026-11-01T10:00:00',
            'currency_code': 'CRC', 'article_ids': [7, 8, 9], 'pending_article_ids': [7, 9], 'lines': [{
                'article_id': 7, 'article_code': '000007', 'name': 'SYNTHETIC ARTICLE',
                'remaining': 6, 'remaining_base': 6, 'unit': 'Caja',
            }],
        }], result['orders'])
        sql, params = source.rows.call_args_list[0].args
        self.assertIn('v.Activo<>0', sql)
        self.assertIn('pa.Saldada=0 AND pa.CantPedida>pa.CantRecibida', sql)
        self.assertIn('pa.Articulo IN (%s,%s)', sql)
        self.assertEqual(('019', 7, 8, 21), params)

    def test_discovery_can_browse_all_suppliers_or_resolve_an_exact_order_by_tax_number(self):
        source = self.source(
            headers=[self.header(Cedula=None), self.header(41, Proveedor='020', Cedula='010')],
            lines=[self.line(NumeroUnidades=1), self.line(41, NumeroUnidades=1)],
        )
        result = self.discover(source)
        self.assertEqual(['019', '020'], [order['vendor_code'] for order in result['orders']])
        self.assertEqual((21,), source.rows.call_args_list[0].args[1])

        source = self.source(headers=[self.header(Cedula='3-101-555555 ')],
                             lines=[self.line(NumeroUnidades=1)])
        result = self.discover(source, vendor_tax_id='3101555555', order_id=42)
        self.assertEqual(42, result['orders'][0]['order_id'])
        sql, params = source.rows.call_args_list[0].args
        self.assertIn('REPLACE', sql)
        self.assertIn('p.Pedido=%s', sql)
        self.assertEqual(('3101555555', 42, 21), params)
        wrong_vendor = self.source(headers=[self.header(Cedula='099')])
        with self.assertRaisesRegex(PurchaseSourceError, 'SGA_PURCHASE_VENDOR_SCOPE_MISMATCH'):
            self.discover(wrong_vendor, vendor_tax_id='3101555555')

    def test_discovery_skips_ineligible_orders_and_preserves_a_later_page(self):
        source = self.source(
            headers=[self.header(42, Cedula=None), self.header(41, Cedula=None),
                     self.header(40, Cedula=None)],
            lines=[self.line(42, Saldada=b'\x01'), self.line(41, Trabajado=None)],
        )
        result = self.discover(source, limit=2)
        self.assertEqual(('not_found', [], 41, True, False),
                         (result['status'], result['orders'], result['cursor_after'],
                          result['has_more'], result['search_complete']))

    def test_discovery_omits_currencies_and_pack_factors_import_cannot_handle(self):
        source = self.source(
            headers=[self.header(42, Cedula=None, Moneda='$'),
                     self.header(41, Cedula=None), self.header(40, Cedula=None)],
            lines=[self.line(42, NumeroUnidades=1),
                   self.line(41, CantRecibida=10, Saldada=b'\x01'), self.line(41, Articulo=9, NumeroUnidades=1),
                   self.line(40, PorMayor=b'\x00')],
        )
        result = self.discover(source, article_ids=[7, 9])
        self.assertEqual([40], [order['order_id'] for order in result['orders']])

    @patch('bridge.purchase_source.MAX_BATCH_LINES', 1)
    def test_discovery_paginates_before_an_incomplete_order_snapshot(self):
        source = self.source(headers=[self.header(42, Cedula=None), self.header(41, Cedula=None)],
                             lines=[self.line(42, NumeroUnidades=1), self.line(41, NumeroUnidades=1)])
        result = self.discover(source, limit=2)
        self.assertEqual(([42], 42, True), ([row['order_id'] for row in result['orders']],
                                          result['cursor_after'], result['has_more']))
        source = self.source(headers=[self.header(41, Cedula=None)], lines=[self.line(41, NumeroUnidades=1)])
        result = self.discover(source, after=42)
        self.assertEqual(([41], 41, False), ([row['order_id'] for row in result['orders']],
                                           result['cursor_after'], result['has_more']))
        self.assertEqual((42, 21), source.rows.call_args_list[0].args[1])

    @patch('bridge.catalog_source.pymysql.connect')
    def test_inherited_connection_guard_rejects_source_with_write_grants(self, connect):
        connection = connect.return_value; cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'grant': 'GRANT SELECT, UPDATE ON `sgadata`.* TO `lab`@`localhost`'}]
        source = PurchaseSource({'host': 'localhost', 'user': 'lab', 'password': 'synthetic'})
        with self.assertRaises(SourceReadOnlyError):
            with source:
                self.fail('write account was accepted')
        connection.close.assert_called_once()
        self.assertFalse(any(call.args[0].startswith('SELECT p.') for call in cursor.execute.call_args_list))
