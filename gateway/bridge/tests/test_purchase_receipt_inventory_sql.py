from datetime import datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock

from bridge.purchase_effects_contract import PurchaseEffectsError
from bridge.purchase_receipt_inventory_sql import apply_receipt_inventory, lock_receipt_inventory


ORDER = 56632
ACTOR = {'audit_user': 'SGAGW', 'audit_station': 28, 'applied_at': datetime(2026, 11, 21, 10, 4, 30)}


def allocation(article, quantity, cost, *, bulk=True):
    return {'order_line_key': [ORDER, bulk, article], 'article_id': article, 'por_mayor': bulk,
            'quantity': quantity, 'quantity_base': quantity, 'unit_factor': 1, 'unit_cost': cost,
            'unit_cost_base': cost}


def stock(article, ordered, quantity, cost):
    return {'OrderVendor': '318', 'PorMayor': b'\x01', 'Articulo': article,
            'Ordered': ordered, 'OrderReceived': 0, 'Saldada': b'\x00',
            'OrderConsignment': b'\x00', 'CatalogConsignment': b'\x00',
            'CatalogArticle': article, 'WarehouseArticle': article, 'SupplierArticle': article,
            'NumeroUnidades': 1, 'Trabajado': 255, 'Servicio': b'\x00', 'Combo': b'\x00',
            'PorLote': b'\x00', 'WarehouseDisabled': b'\x00', 'SupplierDisabled': b'\x00',
            'CantDisponible': quantity, 'CantDisponibleDet': 0, 'Cant': quantity, 'CantDet': 0,
            'GlobalOrdered': ordered + 6, 'GlobalOrderedDetail': 8,
            'SupplierOrdered': ordered + 5, 'SupplierOrderedDetail': 4,
            'CostoUnit': Decimal(cost), 'CostoUnitDet': Decimal(cost)}


def applied(row, quantity, cost):
    result = {
        **row, 'OrderReceived': quantity, 'Saldada': bytes([quantity == row['Ordered']]),
        'CantDisponible': row['CantDisponible'] + quantity, 'Cant': row['Cant'] + quantity,
        'GlobalOrdered': row['GlobalOrdered'] - quantity, 'SupplierOrdered': row['SupplierOrdered'] - quantity,
        'CostoUnit': Decimal(cost), 'CostoUnitDet': Decimal(cost),
    }
    for prefix in ('Order', 'Catalog', 'Warehouse', 'Supplier'):
        result.update({prefix + 'User': ACTOR['audit_user'], prefix + 'Station': ACTOR['audit_station'],
                       prefix + 'Updated': ACTOR['applied_at']})
    return result


class PurchaseReceiptInventoryTest(TestCase):
    def test_56632_applies_33_units_and_keeps_untouched_32_pending(self):
        first = stock(498, 15, 3, '1370.0001')
        second = stock(3674, 6, 1, '2065.0947')
        third = stock(9373, 6, 0, '3609')
        fourth = stock(10653, 6, 1, '1539')
        untouched = [stock(5407, 20, 36, '3850'), stock(14285, 12, 41, '3478')]
        receipt = {'order_id': ORDER, 'vendor_code': '318', 'warehouse': 1,
                   'lines': [allocation(498, 15, 1370), allocation(3674, 6, 2065),
                             allocation(9373, 6, 3609), allocation(10653, 6, 1539)]}
        after = [applied(first, 15, '1370'), applied(second, 6, '2065.0135'),
                 applied(third, 6, '3609'), applied(fourth, 6, '1539'), *untouched]
        cursor = Mock(rowcount=1)
        cursor.fetchall.side_effect = [[first, second, third, fourth, *untouched], after]
        plan = lock_receipt_inventory(cursor, receipt)
        costs = apply_receipt_inventory(cursor, receipt, plan, **ACTOR)
        self.assertEqual(costs[(ORDER, True, 3674)], Decimal('2065.0135'))
        writes = [call.args for call in cursor.execute.call_args_list if call.args[0].startswith('UPDATE')]
        self.assertEqual(len(writes), 16)
        order_writes = [args for sql, args in writes if sql.startswith('UPDATE pedidosarticulos')]
        self.assertEqual({args[-1]: args[0] for args in order_writes}, {498: 15, 3674: 6, 9373: 6, 10653: 6})
        self.assertTrue(all(args[1] == 1 for args in order_writes))

    def test_native_rounding_and_partial_receive_leave_order_line_open(self):
        before = stock(3674, 8, 1, '2065.6630')
        receipt = {'order_id': ORDER, 'vendor_code': '318', 'warehouse': 1, 'lines': [allocation(3674, 6, 2065)]}
        cursor = Mock(rowcount=1)
        cursor.fetchall.side_effect = [[before], [applied(before, 6, '2065.0947')]]
        plan = lock_receipt_inventory(cursor, receipt)
        costs = apply_receipt_inventory(cursor, receipt, plan, **ACTOR)
        self.assertEqual(costs[(ORDER, True, 3674)], Decimal('2065.0947'))

    def test_inconsistent_pending_fails_before_writes(self):
        before = stock(3674, 6, 1, '2065.6630')
        before['SupplierOrdered'] = 5
        receipt = {'order_id': ORDER, 'vendor_code': '318', 'warehouse': 1, 'lines': [allocation(3674, 6, 2065)]}
        cursor = Mock()
        cursor.fetchall.return_value = [before]
        with self.assertRaisesRegex(PurchaseEffectsError, 'SGA_RECEIPT_INVENTORY_PENDING_MISMATCH'):
            lock_receipt_inventory(cursor, receipt)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_unexpected_untouched_write_fails_readback(self):
        before = stock(3674, 6, 1, '2065.6630')
        untouched = stock(14285, 12, 41, '3478')
        receipt = {'order_id': ORDER, 'vendor_code': '318', 'warehouse': 1, 'lines': [allocation(3674, 6, 2065)]}
        cursor = Mock(rowcount=1)
        cursor.fetchall.side_effect = [
            [before, untouched], [applied(before, 6, '2065.0947'), dict(untouched, OrderReceived=1)],
        ]
        plan = lock_receipt_inventory(cursor, receipt)
        with self.assertRaisesRegex(PurchaseEffectsError, 'SGA_RECEIPT_INVENTORY_READBACK_MISMATCH'):
            apply_receipt_inventory(cursor, receipt, plan, **ACTOR)
