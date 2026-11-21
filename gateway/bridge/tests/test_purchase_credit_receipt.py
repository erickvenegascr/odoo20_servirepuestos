from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock

from bridge import purchase_effects_contract as contract
from bridge.purchase_credit_receipt import PurchaseCreditReceiptSource
from bridge.tests.test_purchase_effects_contract import SOURCE, receive_request_v2, rehash
from bridge.tests.test_purchase_receipt_source import header, line


def credit_request():
    payload = receive_request_v2()
    receipt = payload['receipt']
    receipt['totals'].update(tax=26, total=226)
    receipt['fiscal']['payment_terms'].update(condition_code='02', credit_days=30,
                                             due_date='2026-12-18', due_date_source='xml')
    receipt['fiscal']['lines'][0].update(net_tax_amount=26, taxes=[{
        'code': '01', 'rate_code': '08', 'rate': 13, 'base_amount': 200,
        'amount': 26, 'net_amount': 26, 'exoneration_percentage': 0,
        'exoneration_amount': 0, 'included_in_cost': False, 'exoneration_xml': None,
    }])
    return rehash(payload)


class PurchaseCreditReceiptTests(TestCase):
    def setUp(self):
        self.payload = credit_request()
        receipt = self.payload['receipt']
        vendor, invoice = receipt['vendor_code'], receipt['invoice_number']
        receipt_date = datetime(2026, 11, 19, 8, 23)
        document_date = datetime(2026, 11, 18, 8, 0)
        self.receipts = [header(Proveedor=vendor, OrderVendor=vendor, Factura=invoice,
                                Pedido=receipt['order_id'], FechaRecibo=receipt_date)]
        self.lines = [line(Articulo=41, OrderId=receipt['order_id'], CantRecibida=2, CostoUnit=100)]
        self.headers = [dict(Proveedor=vendor, FacturaPrimaria=invoice, FechaDeuda=document_date)]
        self.details = [dict(Proveedor=vendor, FacturaPrimaria=invoice, Acreedor=vendor, Factura=invoice,
                             Fecha=document_date, Monto=226, ImptoVenta=0.13, MontoImptoVenta=26,
                             TipoCredito=30, TipoCambio=1, Generada=1, Actualizada=1)]
        self.tax = [dict(ImptoVenta=0.13, MontoImptoVenta=26)]

    def source(self):
        source = PurchaseCreditReceiptSource({'instance': SOURCE})
        engines = [{'TABLE_NAME': table, 'ENGINE': 'InnoDB'} for table in source.snapshot_tables]
        responses = [engines, self.receipts]
        if self.receipts:
            responses.append(self.lines)
        source.rows = Mock(side_effect=[*responses, self.headers, self.details, self.tax])
        return source

    def test_matches_exact_applied_lines_plus_tax_and_credit_without_using_untouched_balance(self):
        self.lines.append(line(Articulo=42, OrderId=55, CantRecibida=0, Recibido=0, Actualizado=0,
                               NoRecibido=1, OrderReceived=0))
        source = self.source()
        result = source.verify(self.payload)
        self.assertEqual(('matched', 101), (result['status'], result['receipt_id']))
        self.assertEqual(30, result['payable']['credit_days'])
        self.assertEqual(226, result['payable']['total'])
        self.assertEqual('2026-12-18', result['payable']['due_date'])
        self.assertEqual(self.payload['receipt']['invoice_number'], result['payable']['detail_key'][3])
        self.assertEqual(result['checksum'], contract.digest({key: value for key, value in result.items()
                                                             if key != 'checksum'}))
        for call in source.rows.call_args_list:
            sql, params = call.args
            self.assertTrue(sql.startswith('SELECT '))
            self.assertNotIn(result['scope']['invoice_number'], sql)
            self.assertIsInstance(params, tuple)
        # Matching a receipt and payable does not certify full native execution.
        self.assertNotIn('stock_confirmed', result)
        self.assertIn('BLOCKED_PURCHASE_CREDIT_NOT_CERTIFIED', contract.execution_blockers(self.payload))

    def test_missing_receipt_is_absence_only_when_payable_is_also_absent(self):
        self.receipts.clear()
        self.assertEqual('SGA_CREDIT_RECEIPT_PARTIAL_OR_MULTIPLE', self.source().verify(self.payload)['code'])
        self.headers.clear()
        self.details.clear()
        self.assertEqual('not_found', self.source().verify(self.payload)['status'])

    def test_double_receipt_or_extra_applied_line_is_conflict(self):
        self.receipts.append({**self.receipts[0], 'Recibo': 102})
        self.lines.append({**self.lines[0], 'Recibo': 102})
        self.assertEqual('SGA_CREDIT_RECEIPT_PARTIAL_OR_MULTIPLE', self.source().verify(self.payload)['code'])
        self.receipts.pop()
        self.lines[1].update(Recibo=101, Articulo=42)
        self.assertEqual('SGA_CREDIT_RECEIPT_DETAIL_MISMATCH', self.source().verify(self.payload)['code'])

    def test_detects_wrong_receipt_iva_and_wrong_credit_or_payable_identity(self):
        self.tax[0]['MontoImptoVenta'] = 25
        self.assertEqual('SGA_CREDIT_RECEIPT_TAX_MISMATCH', self.source().verify(self.payload)['code'])
        self.tax[0]['MontoImptoVenta'] = 26
        for key, wrong_value in (('TipoCredito', 0), ('FacturaPrimaria', 'other'), ('TipoCambio', 1.1),
                                 ('MontoImptoVenta', 25), ('Generada', 0), ('Monto', 225.99)):
            with self.subTest(key=key):
                good = self.details[0][key]
                self.details[0][key] = wrong_value
                self.assertEqual('SGA_CREDIT_RECEIPT_PAYABLE_MISMATCH', self.source().verify(self.payload)['code'])
                self.details[0][key] = good

    def test_detects_maturity_shift_if_sga_starts_credit_on_physical_receipt_date(self):
        self.details[0]['Fecha'] = self.receipts[0]['FechaRecibo']
        result = self.source().verify(self.payload)
        self.assertEqual('SGA_CREDIT_RECEIPT_DUE_DATE_MISMATCH', result['code'])
        self.assertEqual(('2026-12-18', '2026-12-19'),
                         (result['expected_due_date'], result['native_due_date']))

    def test_unsupported_documents_and_mismatched_source_do_not_query_native_tables(self):
        source = self.source()
        with self.assertRaisesRegex(contract.PurchaseEffectsError, 'SGA_CREDIT_RECEIPT_FISCAL_CASE_UNSUPPORTED'):
            source.verify(receive_request_v2())
        self.payload['source_instance'] = 'somewhere-else'
        with self.assertRaisesRegex(contract.PurchaseEffectsError, 'SGA_CREDIT_RECEIPT_SOURCE_MISMATCH'):
            source.verify(self.payload)
        source.rows.assert_not_called()
