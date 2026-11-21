"""Compare a regular IVA credit purchase with its native receipt and payable."""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .purchase_effects_contract import PurchaseEffectsError, request
from .purchase_receipt_source import RECEIPT_TABLES, SCOPE_FIELDS, PurchaseReceiptSource
from .purchase_source import _bit, _digest, _iso, _money


class PurchaseCreditReceiptSource(PurchaseReceiptSource):
    snapshot_tables = RECEIPT_TABLES | {'cuentaspagar', 'cuentaspagardet'}

    def verify(self, payload):
        operation = request(payload)
        source_instance = self.config.get('instance') or self.config.get('source_instance')
        if operation['action'] != 'purchase.receive' or operation['contract'] != 2:
            raise PurchaseEffectsError('SGA_CREDIT_RECEIPT_REQUIRES_V2')
        if not source_instance or operation['source_instance'] != source_instance:
            raise PurchaseEffectsError('SGA_CREDIT_RECEIPT_SOURCE_MISMATCH')
        receipt = operation['receipt']
        fiscal = receipt['fiscal']
        if not _regular_credit(fiscal):
            raise PurchaseEffectsError('SGA_CREDIT_RECEIPT_FISCAL_CASE_UNSUPPORTED')
        expected = {tuple(row['order_line_key']): row for row in receipt['lines']}
        if len(expected) != len(receipt['lines']):
            raise PurchaseEffectsError('SGA_CREDIT_RECEIPT_SPLIT_LINES_UNSUPPORTED')

        scope = {key: receipt[key] for key in SCOPE_FIELDS}
        lookup = self.lookup(**scope)
        result = {'scope': scope, 'source_instance': source_instance,
                  'operation_id': operation['operation_id'], 'application_id': operation['application_id'],
                  'request_checksum': operation['request_checksum']}
        if lookup['status'] not in ('not_found', 'found'):
            return {**result, 'status': lookup['status'], 'code': lookup['code']}

        invoice = receipt['invoice_number']
        vendor = receipt['vendor_code']
        headers = self.rows(
            'SELECT Proveedor,FacturaPrimaria,FechaDeuda FROM cuentaspagar '
            'WHERE Proveedor=%s AND FacturaPrimaria=%s LIMIT 2', (vendor, invoice))
        details = self.rows(
            'SELECT Proveedor,FacturaPrimaria,Acreedor,Factura,Fecha,Monto,MontoImptoVenta,'
            'ImptoVenta,TipoCredito,TipoCambio,Generada,Actualizada FROM cuentaspagardet '
            'WHERE (Proveedor=%s AND FacturaPrimaria=%s) OR (Acreedor=%s AND Factura=%s) LIMIT 2',
            (vendor, invoice, vendor, invoice))
        receipts = lookup['receipts']
        if not receipts and not headers and not details:
            return {**result, 'status': 'not_found'}
        if len(receipts) != 1 or not receipts[0]['complete'] or len(headers) != 1 or len(details) != 1:
            return {**result, 'status': 'conflict', 'code': 'SGA_CREDIT_RECEIPT_PARTIAL_OR_MULTIPLE'}

        native = receipts[0]
        if (native['receipt_date'][:10] != fiscal['physical_receipt_date']
                or not _lines_match(expected, native['lines'])):
            return {**result, 'status': 'conflict', 'code': 'SGA_CREDIT_RECEIPT_DETAIL_MISMATCH'}
        receipt_tax = self.rows(
            'SELECT ImptoVenta,MontoImptoVenta FROM recibos WHERE Recibo=%s', (native['receipt_id'],))
        if len(receipt_tax) != 1 or not _vat_matches(receipt_tax[0], receipt['totals']['tax']):
            return {**result, 'status': 'conflict', 'code': 'SGA_CREDIT_RECEIPT_TAX_MISMATCH'}

        header, detail = headers[0], details[0]
        primary_key = [header['Proveedor'], header['FacturaPrimaria']]
        detail_key = [detail['Proveedor'], detail['FacturaPrimaria'], detail['Acreedor'], detail['Factura']]
        if (primary_key != [vendor, invoice] or detail_key != [vendor, invoice, vendor, invoice]
                or not _bit(detail['Generada']) or not _bit(detail['Actualizada'])
                or detail['TipoCredito'] != fiscal['payment_terms']['credit_days']
                or _money(detail['TipoCambio']) != 1
                or not _vat_matches(detail, receipt['totals']['tax'])
                or _cents(detail['Monto']) != _cents(receipt['totals']['total'])):
            return {**result, 'status': 'conflict', 'code': 'SGA_CREDIT_RECEIPT_PAYABLE_MISMATCH'}

        document_date = _iso(detail['Fecha'])
        if document_date is None:
            return {**result, 'status': 'conflict', 'code': 'SGA_CREDIT_RECEIPT_PAYABLE_DATE_MISSING'}
        # SGA's Pagos Gestion query adds TipoCredito days to CuentasPagarDet.Fecha.
        due_date = date.fromisoformat(document_date[:10]) + timedelta(days=detail['TipoCredito'])
        if due_date.isoformat() != fiscal['payment_terms']['due_date']:
            return {**result, 'status': 'conflict', 'code': 'SGA_CREDIT_RECEIPT_DUE_DATE_MISMATCH',
                    'expected_due_date': fiscal['payment_terms']['due_date'], 'native_due_date': due_date.isoformat()}
        result.update(status='matched', receipt_id=native['receipt_id'], receipt_checksum=native['checksum'],
                      payable={'primary_key': primary_key, 'detail_key': detail_key,
                               'debt_date': _iso(header['FechaDeuda']), 'document_date': document_date,
                               'due_date': due_date.isoformat(),
                               'credit_days': detail['TipoCredito'], 'tax': _money(detail['MontoImptoVenta']),
                               'total': _money(detail['Monto'])})
        result['checksum'] = _digest(result)
        return result


def _regular_credit(fiscal):
    terms = fiscal['payment_terms']
    if (terms['condition_code'] != '02' or not terms['credit_days']
            or fiscal['other_charges'] or fiscal['vat_refund']):
        return False
    for line in fiscal['lines']:
        taxes = line['taxes']
        if (line['inventory_effect'] != 'receive' or line['discounts'] or line['promotion']['type'] is not None
                or line['assumed_tax_amount'] or len(taxes) != 1):
            return False
        tax = taxes[0]
        if (tax['code'], tax['rate_code'], tax['rate']) != ('01', '08', 13) or any(
                tax[key] for key in ('exoneration_amount', 'exoneration_percentage', 'exoneration_xml',
                                     'included_in_cost')):
            return False
    return True


def _lines_match(expected, actual):
    touched = set()
    for line in actual:
        if not line['applied']:
            if line['quantity'] or line['received'] or line['updated']:
                return False
            continue
        key = tuple(line['order_line_key'])
        wanted = expected.get(key)
        if (wanted is None or line['quantity'] != wanted['quantity']
                or line['quantity_base'] != wanted['quantity_base']
                or line['por_mayor'] and line['unit_factor'] != wanted['unit_factor']
                or line['unit_cost_net'] != _money(wanted['unit_cost'])):
            return False
        touched.add(key)
    return touched == expected.keys()


def _cents(value):
    return Decimal(str(_money(value))).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def _vat_matches(row, tax):
    return _money(row['ImptoVenta']) == 0.13 and _cents(row['MontoImptoVenta']) == _cents(tax)
