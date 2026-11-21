"""Native provider balance maintained with the corresponding payable."""

from decimal import Decimal, ROUND_HALF_UP

from .purchase_effects_contract import MAX_MONEY, PurchaseEffectsError


def supplier_money(value):
    if type(value) not in (int, float, Decimal):
        raise PurchaseEffectsError('SGA_SUPPLIER_DEBT_INVALID')
    amount = Decimal(str(value))
    if not amount.is_finite() or abs(amount) > MAX_MONEY:
        raise PurchaseEffectsError('SGA_SUPPLIER_DEBT_INVALID')
    return amount.quantize(Decimal('.0001'), rounding=ROUND_HALF_UP)


def lock_supplier_debt(cursor, vendor_code):
    cursor.execute('SELECT MontoDeuda FROM proveedores WHERE Proveedor=%s FOR UPDATE', (vendor_code,))
    row = cursor.fetchone()
    if row is None:
        raise PurchaseEffectsError('SGA_SUPPLIER_DEBT_VENDOR_MISSING')
    return supplier_money(row['MontoDeuda'])


def set_locked_supplier_debt(cursor, vendor_code, expected):
    expected = supplier_money(expected)
    cursor.execute('UPDATE proveedores SET MontoDeuda=%s WHERE Proveedor=%s', (expected, vendor_code))
    cursor.execute('SELECT MontoDeuda FROM proveedores WHERE Proveedor=%s', (vendor_code,))
    row = cursor.fetchone()
    if row is None or supplier_money(row['MontoDeuda']) != expected:
        raise PurchaseEffectsError('SGA_SUPPLIER_DEBT_READBACK_MISMATCH')
    return expected
