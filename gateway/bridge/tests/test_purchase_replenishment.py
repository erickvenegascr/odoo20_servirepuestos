from unittest import TestCase
from unittest.mock import MagicMock, patch

from bridge.catalog_source import SourceReadOnlyError
from bridge.purchase_replenishment import PurchaseReplenishmentSource, ReplenishmentSourceError


class PurchaseReplenishmentTests(TestCase):
    def article(self, article=7, **values):
        return {'Articulo': article, 'ArticuloNomb': 'SYNTHETIC ARTICLE', 'NumeroUnidades': 12,
            'Unidad': 'Caja', 'UnidadDet': 'Unidad', 'CantDisponible': 1, 'CantDisponibleDet': 6,
            'CantMin': 2, 'CantMax': 5, 'Clasificacion': 'A', 'Ranking': 3,
            'Trabajado': 255, 'Retenido': b'\x00', 'NoPedir': b'\x00', 'Servicio': b'\x00',
            'Consignacion': b'\x00', 'MantieneMinMax': b'\x01', **values}

    def supplier(self, article=7, vendor='019', **values):
        return {'Articulo': article, 'Proveedor': vendor, 'Prioridad': 1, 'Deshabilitado': b'\x00',
            'ProveedorNomb': 'Synthetic supplier', 'Activo': b'\x01', **values}

    def source(self, articles=None, suppliers=None, **config):
        source = PurchaseReplenishmentSource({'instance': 'replenishment-lab',
            'replenishment_worked': 255, 'replenishment_retained': False,
            'replenishment_query_verified': True, **config})
        source.rows = MagicMock(side_effect=[articles if articles is not None else [self.article()],
                                             suppliers if suppliers is not None else [self.supplier()]])
        return source

    def test_below_minimum_is_raw_normalized_stock_and_never_an_order_quantity(self):
        source = self.source()
        result = source.suggest('019', abc='a')
        self.assertEqual(('found', True, False),
                         (result['status'], result['search_complete'], result['has_more']))
        candidate = result['candidates'][0]
        self.assertEqual('000007', candidate['article_code'])
        self.assertEqual((1, 6, 18, 1.5, 2, 5, 0.5), tuple(candidate[name] for name in (
            'available_major', 'available_detail', 'available_base', 'available_major_normalized',
            'minimum_major', 'maximum_major', 'shortfall_to_minimum_major')))
        self.assertEqual(('A', 3), (candidate['abc_native'], candidate['ranking_native']))
        self.assertTrue(candidate['eligible']); self.assertIsNone(candidate['suggested_major'])
        self.assertEqual('019', candidate['supplier']['vendor_code'])
        sql, params = source.rows.call_args_list[0].args
        self.assertIn('a.Trabajado=%s', sql); self.assertIn('a.Retenido=%s', sql)
        self.assertIn('/NULLIF(a.NumeroUnidades,0))<a.CantMin', sql)
        self.assertNotIn('Facturable', sql); self.assertNotIn('CantPedida', sql)
        self.assertNotIn('<=a.CantMin', sql)
        self.assertEqual((255, 0, 'A', '019', 81), params)
        for call in source.rows.call_args_list:
            self.assertTrue(call.args[0].startswith('SELECT ')); self.assertNotIn(';', call.args[0])
            self.assertNotIn('FOR UPDATE', call.args[0])

    def test_equal_minimum_cannot_be_returned_as_below_minimum(self):
        with self.assertRaises(ReplenishmentSourceError):
            self.source(articles=[self.article(CantDisponible=2, CantDisponibleDet=0)]).suggest()

    def test_missing_native_selection_context_blocks_without_query(self):
        source = self.source(replenishment_worked=None, replenishment_retained=None)
        result = source.suggest()
        self.assertEqual('blocked', result['status']); self.assertFalse(result['search_complete'])
        self.assertEqual(['SGA_NATIVE_WORKED_SELECTION_REQUIRED', 'SGA_NATIVE_RETAINED_SELECTION_REQUIRED'],
                         result['blockers'])
        source.rows.assert_not_called()
        source = self.source(articles=[self.article(Retenido=b'\x01')], replenishment_retained=None)
        candidate = source.suggest(retained=True)['candidates'][0]
        self.assertIn('article_retained', candidate['issues']); self.assertFalse(candidate['eligible'])

    def test_auto_strategy_is_blocked_without_running_or_inventing_native_functions(self):
        source = self.source()
        result = source.suggest(strategy='minmax_auto')
        self.assertEqual('blocked', result['status']); self.assertEqual([], result['candidates'])
        self.assertIn('SGA_NATIVE_FACTURABLE_UNVERIFIED', result['blockers'])
        self.assertIn('SGA_NATIVE_BACKORDER_UNVERIFIED', result['blockers'])
        source.rows.assert_not_called()

    def test_unverified_live_query_keeps_candidate_visible_for_review(self):
        source = self.source(replenishment_query_verified=False)
        result = source.suggest()
        self.assertEqual('found', result['status'])
        self.assertFalse(result['evidence']['native_query_version_verified'])
        self.assertIn('native_query_version_unverified', result['candidates'][0]['issues'])
        self.assertFalse(result['candidates'][0]['eligible'])

    def test_no_order_service_flags_guard_eligibility_without_hiding_native_list(self):
        candidate = self.source(articles=[self.article(NoPedir=b'\x01', Servicio=b'\x01')])\
            .suggest()['candidates'][0]
        self.assertIn('article_no_order', candidate['issues'])
        self.assertIn('article_service', candidate['issues']); self.assertFalse(candidate['eligible'])

    def test_priority_tie_is_not_arbitrarily_resolved(self):
        candidate = self.source(suppliers=[self.supplier(), self.supplier(vendor='020')])\
            .suggest('019')['candidates'][0]
        self.assertIsNone(candidate['supplier']); self.assertFalse(candidate['eligible'])
        self.assertIn('supplier_priority_ambiguous', candidate['issues'])
        self.assertEqual(2, len(candidate['supplier_candidates']))

    def test_disabled_or_inactive_winner_is_not_replaced_with_next_priority(self):
        for values, issue in [({'Deshabilitado': b'\x01'}, 'supplier_relation_disabled'),
                              ({'Activo': b'\x00'}, 'supplier_inactive'),
                              ({'ProveedorNomb': None, 'Activo': None}, 'supplier_missing')]:
            with self.subTest(issue=issue):
                candidate = self.source(suppliers=[self.supplier(**values),
                    self.supplier(vendor='020', Prioridad=2)]).suggest()['candidates'][0]
                self.assertEqual('019', candidate['supplier']['vendor_code'])
                self.assertIn(issue, candidate['issues']); self.assertFalse(candidate['eligible'])
        candidate = self.source(suppliers=[]).suggest()['candidates'][0]
        self.assertIn('supplier_missing', candidate['issues'])

    def test_stable_rank_cursor_preserves_null_and_ties(self):
        source = self.source(articles=[self.article(7, Ranking=None), self.article(8, Ranking=1),
                                      self.article(9, Ranking=1)],
                             suppliers=[self.supplier(7), self.supplier(8)])
        first = source.suggest(limit=2)
        self.assertEqual({'ranking': 1, 'article_id': 8}, first['cursor_after'])
        self.assertTrue(first['has_more']); self.assertFalse(first['search_complete'])
        source = self.source(articles=[self.article(9, Ranking=1)], suppliers=[self.supplier(9)])
        second = source.suggest(after=first['cursor_after'])
        self.assertEqual(9, second['candidates'][0]['article_id'])
        self.assertFalse(second['search_complete'])
        self.assertEqual((255, 0, 1, 1, 8, 81), source.rows.call_args_list[0].args[1])
        source = self.source(articles=[], suppliers=[])
        result = source.suggest(after={'ranking': None, 'article_id': 7})
        self.assertFalse(result['search_complete'])
        self.assertIn('a.Ranking IS NOT NULL', source.rows.call_args.args[0])

    def test_empty_result_is_scoped_and_source_timeout_is_not_absence(self):
        source = self.source(articles=[])
        result = source.suggest('019')
        self.assertEqual(('not_found', True), (result['status'], result['search_complete']))
        self.assertEqual(1, source.rows.call_count)
        source = self.source(); source.rows.side_effect = TimeoutError('synthetic loss')
        with self.assertRaises(TimeoutError): source.suggest()

    def test_malformed_scope_or_config_never_executes_business_query(self):
        for values in [{'strategy': 'other'}, {'vendor_code': 'LONG'}, {'limit': True},
                       {'abc': 'AB'}, {'retained': 1}, {'after': False}, {'after': 7},
                       {'after': {'ranking': 1, 'article_id': True}}]:
            source = self.source()
            with self.subTest(values=values), self.assertRaises(ReplenishmentSourceError):
                source.suggest(**values)
            source.rows.assert_not_called()
        for config in [{'replenishment_worked': True}, {'replenishment_retained': 1},
                       {'replenishment_query_verified': 'true'}]:
            source = self.source(**config)
            with self.subTest(config=config), self.assertRaises(ReplenishmentSourceError): source.suggest()
            source.rows.assert_not_called()

    def test_mismatched_scope_duplicate_identity_or_incomplete_native_row_fails_closed(self):
        for articles, suppliers in [([self.article(Trabajado=1)], []),
                ([self.article(Retenido=b'\x01')], []), ([self.article(), self.article()], []),
                ([self.article(NumeroUnidades=0)], []), ([self.article(NumeroUnidades=None)], []),
                ([self.article()], [self.supplier(), self.supplier()]),
                ([self.article()], [self.supplier(8)])]:
            with self.subTest(articles=articles, suppliers=suppliers), self.assertRaises(ReplenishmentSourceError):
                self.source(articles=articles, suppliers=suppliers).suggest()

    @patch('bridge.catalog_source.pymysql.connect')
    def test_inherited_account_guard_rejects_write_grants_before_business_reads(self, connect):
        connection = connect.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'grant': 'GRANT SELECT, UPDATE ON `sgadata`.* TO `lab`@`localhost`'}]
        source = PurchaseReplenishmentSource({'host': 'localhost', 'user': 'lab', 'password': 'synthetic'})
        with self.assertRaises(SourceReadOnlyError):
            with source:
                self.fail('write account was accepted')
        connection.close.assert_called_once()
        self.assertFalse(any(call.args[0].startswith('SELECT a.') for call in cursor.execute.call_args_list))
