from datetime import datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock

from bridge.purchase_source import _digest
from bridge.purchase_valuation_snapshot import PurchaseValuationSnapshot, compare_purchase_valuation, weighted_unit_cost
from bridge.tests.test_purchase_credit_receipt import credit_request
from bridge.tests.test_purchase_effects_contract import SOURCE


def native_line(**overrides):
    return {'CapturedAt': datetime(2026, 11, 19, 10, 2), 'OrderVendor': '001',
            'PorMayor': 0, 'Articulo': 41, 'Ordered': 5, 'OrderReceived': 0, 'Saldada': 0,
            'NumeroUnidades': 1, 'CantDisponible': 8, 'CantDisponibleDet': 0,
            'CostoUnit': 97.125, 'CostoUnitDet': 97.125, 'GlobalOrdered': 5,
            'GlobalOrderedDetail': 0, 'WarehouseArticle': 41, 'Cant': 8, 'CantDet': 0,
            'FechaUltIngreso': datetime(2026, 11, 15), 'SupplierArticle': 41,
            'SupplierOrdered': 5, 'SupplierOrderedDetail': 0, 'SupplierLastPurchase': datetime(2026, 11, 15),
            **overrides}


class PurchaseValuationSnapshotTests(TestCase):
    def capture(self, *lines):
        payload = credit_request()
        source = PurchaseValuationSnapshot({'instance': SOURCE})
        engines = [{'TABLE_NAME': table, 'ENGINE': 'InnoDB'} for table in source.snapshot_tables]
        source.rows = Mock(side_effect=[engines, [], [], [], list(lines)])
        return source.capture(payload), source

    def test_opening_captures_native_cost_and_untouched_order_lines(self):
        snapshot, source = self.capture(native_line(), native_line(Articulo=42, Ordered=8,
                                                                   WarehouseArticle=None, SupplierArticle=None))
        self.assertEqual('captured', snapshot['status'])
        self.assertEqual('not_found', snapshot['receipt_verification']['status'])
        self.assertEqual('2026-11-19T10:02:00', snapshot['captured_at_utc'])
        first, untouched = snapshot['lines']
        self.assertEqual(97.125, first['global']['unit_cost_bulk'])
        self.assertEqual(8, first['warehouse']['quantity_bulk'])
        self.assertEqual(5, first['supplier']['pending_bulk'])
        self.assertEqual(8, untouched['remaining'])
        self.assertIsNone(untouched['warehouse'])
        for call in source.rows.call_args_list:
            self.assertTrue(call.args[0].startswith('SELECT '))
        self.assertEqual((1, 55, 501), source.rows.call_args.args[1])

    def test_different_or_missing_order_line_cannot_be_captured(self):
        snapshot, _ = self.capture(native_line(OrderVendor='002'))
        self.assertEqual('SGA_VALUATION_ORDER_SCOPE_UNVERIFIED', snapshot['code'])
        snapshot, _ = self.capture(native_line(Articulo=42))
        self.assertEqual('SGA_VALUATION_ORDER_LINE_UNVERIFIED', snapshot['code'])

    def test_compares_stock_cost_pending_and_original_unreceived_lines(self):
        pending = native_line(Articulo=42, Ordered=32)
        before, _ = self.capture(native_line(), pending)
        applied = native_line(OrderReceived=2, Cant=10, CantDisponible=10, CostoUnit=97.7,
                              SupplierOrdered=3, GlobalOrdered=3, CapturedAt=datetime(2026, 11, 19, 10, 3))
        after, _ = self.capture(applied, pending)
        after.pop('checksum')
        after['receipt_verification']['status'] = 'matched'
        after['checksum'] = _digest(after)
        result = compare_purchase_valuation(credit_request(), before, after)
        self.assertEqual(('matched', 37, 35), (result['status'], result['remaining_before'], result['remaining_after']))
        self.assertEqual(97.7, result['lines'][0]['expected_unit_cost'])
        self.assertTrue(result['lines'][1]['matched'])

        after.pop('checksum')
        after['lines'][1].update(received=32, remaining=0, closed=True)
        after['checksum'] = _digest(after)
        result = compare_purchase_valuation(credit_request(), before, after)
        self.assertEqual('mismatch', result['status'])
        self.assertFalse(result['lines'][1]['matched'])

    def test_weighted_cost_keeps_four_decimals(self):
        self.assertEqual(Decimal('106.9667'), weighted_unit_cost(1, '104.9000', 2, '108'))
        self.assertEqual(Decimal('108.0000'), weighted_unit_cost(0, '104.9000', 2, '108'))
