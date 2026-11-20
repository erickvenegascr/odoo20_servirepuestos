"""Effect contracts prove correspondence, not SGA's unobserved accounting rules."""
import copy
import unittest
from unittest.mock import Mock

from bridge import purchase_effects_contract as contract


APP = 'b3c9b7d9-5655-452d-bf0f-39d81661eff5'
OPERATION = '393634c1-1420-4fad-87c3-818046499dc7'
SOURCE = 'servirepuestos-test'


def receive_request():
    body = {
        'invoice_key': '50618112600310112345600100001010000000001199999999',
        'invoice_number': '00100001010000000001',
        'invoice_date': '2026-11-18', 'document_id': 91,
        'vendor_code': '001', 'warehouse': 1, 'order_id': 55,
        'currency': 'CRC', 'order_checksum': 'a' * 64,
        'totals': {'untaxed': 200, 'tax': 26, 'total': 226},
        'lines': [{
            'allocation_id': 'invoice-91/xml-4/order-55-detail-41',
            'xml_line_id': 4, 'order_line_key': [55, False, 41],
            'article_id': 41, 'por_mayor': False, 'quantity': 2,
            'quantity_base': 2, 'unit_factor': 1,
            'unit_cost': 100, 'unit_cost_base': 100,
        }],
    }
    return envelope('purchase.receive', 'receipt', body)


def envelope(action, name, body):
    return {
        'contract': 1, 'action': action, 'operation_id': OPERATION,
        'application_id': APP, 'source_instance': SOURCE,
        'request_checksum': contract.digest(body), name: body,
    }


def price_request():
    return envelope('price.publish', 'price', {
        'article_id': 41, 'level': 1, 'currency': 'CRC',
        'unit_price_excluded_tax': 150.1234, 'vat_code': '08', 'vat_rate': 13,
        'document_id': 91,
    })


def receive_request_v2():
    request = receive_request()
    request['contract'] = 2
    receipt = request['receipt']
    receipt['invoice_key'] = '5' * 50
    receipt['totals'] = {'untaxed': 200, 'tax': 0, 'total': 200}
    receipt['fiscal'] = {
        'xml_sha256': 'c' * 64, 'issued_at': '2026-11-18T08:00:00-06:00',
        'physical_receipt_date': '2026-11-19', 'source_currency': 'CRC',
        'source_exchange_rate': None, 'exchange_rate': 1,
        'payment_terms': {'condition_code': '01', 'credit_days': 0,
            'due_date': '2026-11-18', 'due_date_source': 'cash_issue_date'},
        'vat_refund': 0, 'other_charges': [],
        'lines': [{
            'xml_line_id': 4, 'xml_sequence': 1, 'cabys': '4740399009900',
            'inventory_effect': 'receive', 'quantity': 2, 'quantity_base': 2,
            'conversion_factor': 1, 'conversion_mode': 'multiply',
            'unit_price': 100, 'gross_amount': 200, 'discounts': [], 'net_amount': 200,
            'taxes': [], 'assumed_tax_amount': 0, 'net_tax_amount': 0,
            'promotion': {'type': None, 'parent_xml_line_id': None},
            'allocation_ids': [receipt['lines'][0]['allocation_id']],
        }],
    }
    return rehash(request)


def price_request_v2():
    request = price_request()
    request['contract'] = 2
    request['price']['exchange_rate'] = 1
    return rehash(request)


def common(request):
    return {key: copy.deepcopy(request[key]) for key in contract.COMMON | {'request_checksum'}}


def native_receive(request):
    receipt = request['receipt']
    result = {
        **common(request), 'status': 'synchronized', 'receipt_ids': [101],
        'order_id': receipt['order_id'], 'stock_confirmed': True,
        'valuation_confirmed': True, 'ap_confirmed': True, 'order_verified': True,
        'payable': {
            'invoice_key': receipt['invoice_key'],
            'primary_key': [receipt['vendor_code'], receipt['invoice_number']],
            'detail_keys': [[receipt['vendor_code'], receipt['invoice_number'],
                             receipt['vendor_code'], receipt['invoice_number']]],
            'currency': receipt['currency'], 'amount_total': receipt['totals']['total'],
        },
        'lines': [],
    }
    for wanted in receipt['lines']:
        result['lines'].append({
            **{key: copy.deepcopy(value) for key, value in wanted.items()
               if key not in {'unit_factor', 'unit_cost'}},
            'receipt_id': 101, 'cost_confirmed': True,
            # Intentionally unlike incoming cost: this is a native result.
            'average_cost_base': 73.125,
        })
    if request['contract'] == 2:
        result.update(fiscal_confirmed=True, fiscal_checksum=contract.digest(receipt['fiscal']),
                      physical_receipt_date=receipt['fiscal']['physical_receipt_date'])
        result['payable'].update(payment_terms=copy.deepcopy(receipt['fiscal']['payment_terms']), exchange_rate=1)
    return result


def native_price(request):
    return {**common(request), 'status': 'synchronized', 'price_confirmed': True,
            'effective_price': {key: value for key, value in request['price'].items()
                                if key != 'document_id'}}


def rehash(request):
    body = 'receipt' if request['action'] == 'purchase.receive' else 'price'
    request['request_checksum'] = contract.digest(request[body])
    return request


class PurchaseEffectsContractTests(unittest.TestCase):
    def assert_bad_request(self, request):
        with self.assertRaises(contract.PurchaseEffectsError) as raised:
            contract.request(request)
        self.assertNotIsInstance(raised.exception, contract.NativeResultUnverified)
        return raised.exception.code

    def assert_unverified(self, request, result, **kwargs):
        with self.assertRaises(contract.NativeResultUnverified) as raised:
            contract.reconcile(request, result, **kwargs)
        return raised.exception.code

    def test_receipt_partial_allocation_and_exact_json_are_preserved(self):
        request = receive_request()
        request['receipt']['lines'][0]['quantity'] = 2.0
        rehash(request)
        frozen = copy.deepcopy(request)
        result = contract.request(request)
        self.assertEqual(result, frozen)
        self.assertIs(type(result['receipt']['lines'][0]['quantity']), float)
        result['receipt']['lines'][0]['quantity'] = 3
        self.assertEqual(request, frozen)
        ack = native_receive(request)
        ack['lines'][0]['quantity'] = 2
        self.assertEqual(contract.reconcile_receive(request, ack), ack)

    def test_no_average_is_computed_from_incoming_cost(self):
        request = receive_request()
        ack = native_receive(request)
        result = contract.reconcile(request, ack)
        self.assertEqual(result['lines'][0]['average_cost_base'], 73.125)
        self.assertEqual(request['receipt']['lines'][0]['unit_cost_base'], 100)

    def test_pack_quantity_and_cost_must_use_same_base(self):
        request = receive_request()
        line = request['receipt']['lines'][0]
        line.update(por_mayor=True, order_line_key=[55, True, 41], unit_factor=6,
                    quantity=2, quantity_base=12, unit_cost=600, unit_cost_base=100)
        request['receipt']['totals'] = {'untaxed': 1200, 'tax': 156, 'total': 1356}
        rehash(request)
        self.assertEqual(contract.request(request), request)
        line['quantity_base'] = 2
        self.assertEqual(self.assert_bad_request(rehash(request)), 'PURCHASE_RECEIPT_UNIT_MISMATCH')
        line['quantity_base'] = 12
        line['unit_cost_base'] = 600
        self.assertEqual(self.assert_bad_request(rehash(request)), 'PURCHASE_RECEIPT_UNIT_COST_MISMATCH')

    def test_detail_quantity_is_already_base_even_if_master_has_pack(self):
        request = receive_request()
        request['receipt']['lines'][0]['unit_factor'] = 6
        self.assertEqual(contract.request(rehash(request)), request)

    def test_native_key_may_consolidate_only_compatible_allocations(self):
        request = receive_request()
        duplicate = copy.deepcopy(request['receipt']['lines'][0])
        duplicate.update(allocation_id='second-xml-allocation', xml_line_id=5)
        request['receipt']['lines'].append(duplicate)
        request['receipt']['totals'] = {'untaxed': 400, 'tax': 52, 'total': 452}
        rehash(request)
        self.assertEqual(contract.reconcile(request, native_receive(request))['status'], 'synchronized')
        duplicate['unit_cost'] = duplicate['unit_cost_base'] = 99
        self.assertEqual(self.assert_bad_request(rehash(request)), 'PURCHASE_RECEIPT_NATIVE_KEY_CONFLICT')

    def test_duplicate_allocation_or_conflicting_compound_identity_is_rejected(self):
        for field, value in [('allocation_id', None), ('order_line_key', [56, False, 41]),
                             ('article_id', 42), ('por_mayor', True)]:
            with self.subTest(field=field):
                request = receive_request()
                if field == 'allocation_id':
                    request['receipt']['lines'].append(copy.deepcopy(request['receipt']['lines'][0]))
                else:
                    request['receipt']['lines'][0][field] = value
                self.assert_bad_request(rehash(request))

    def test_receive_requires_resolved_order(self):
        request = receive_request()
        request['receipt']['order_id'] = None
        self.assert_bad_request(rehash(request))

    def test_bools_fractional_quantities_nan_and_overflow_do_not_reach_executor(self):
        for field, value in [('quantity', True), ('quantity', 1.5), ('quantity', 0),
                             ('quantity', 2147483648), ('quantity_base', float('inf')),
                             ('unit_factor', 32768), ('unit_cost', float('nan')),
                             ('unit_cost_base', -1), ('article_id', 41.0),
                             ('xml_line_id', True)]:
            with self.subTest(field=field, value=value):
                request = receive_request()
                request['receipt']['lines'][0][field] = value
                if isinstance(value, float) and not value < float('inf'):
                    self.assert_bad_request(request)
                else:
                    self.assert_bad_request(rehash(request))

    def test_total_overflow_and_native_consolidated_quantity_overflow(self):
        request = receive_request()
        request['receipt']['totals'] = {'untaxed': 1e16, 'tax': 0, 'total': 1e16}
        self.assert_bad_request(rehash(request))
        request = receive_request()
        line = request['receipt']['lines'][0]
        line['quantity'] = line['quantity_base'] = 2147483647
        duplicate = {**line, 'allocation_id': 'other', 'quantity': 1, 'quantity_base': 1}
        request['receipt']['lines'].append(duplicate)
        self.assert_bad_request(rehash(request))

    def test_invoice_number_is_never_truncated_or_whitespace_normalized(self):
        for value in ('0' * 21, ' 123', '123 ', '12\n3'):
            with self.subTest(value=value):
                request = receive_request()
                request['receipt']['invoice_number'] = value
                self.assert_bad_request(rehash(request))
        request = receive_request()
        self.assertEqual(contract.request(request)['receipt']['invoice_number'], '00100001010000000001')

    def test_invoice_totals_currency_dates_and_hash_are_explicit(self):
        for field, value in [('currency', 'CR'), ('currency', None), ('invoice_date', '2026-02-30'),
                             ('warehouse', True), ('order_checksum', 'x' * 64),
                             ('totals', {'untaxed': 200, 'tax': 26, 'total': 225})]:
            with self.subTest(field=field):
                request = receive_request()
                request['receipt'][field] = value
                self.assert_bad_request(rehash(request))
        request = receive_request()
        request['receipt']['lines'][0]['quantity'] = 3
        self.assert_bad_request(request)

    def test_invalid_container_inputs_raise_contract_errors(self):
        for bad in (None, [], {}, {'contract': 1, 'action': []}):
            self.assert_bad_request(bad)
        request = price_request()
        request['price']['vat_code'] = []
        self.assert_bad_request(rehash(request))

    def test_request_echo_is_not_native_confirmation(self):
        request = receive_request()
        self.assert_unverified(request, request)
        self.assert_unverified(price_request(), price_request())

    def test_receive_requires_all_native_effect_flags_as_true_booleans(self):
        request = receive_request()
        for field in ('stock_confirmed', 'valuation_confirmed', 'ap_confirmed', 'order_verified'):
            for value in (False, 1, None):
                with self.subTest(field=field, value=value):
                    ack = native_receive(request)
                    ack[field] = value
                    self.assert_unverified(request, ack)
            ack = native_receive(request)
            del ack[field]
            self.assert_unverified(request, ack)

    def test_ack_cannot_change_scope_request_checksum_or_operation(self):
        request = receive_request()
        for field, value in [('operation_id', APP), ('application_id', OPERATION),
                             ('source_instance', 'other'), ('request_checksum', 'b' * 64),
                             ('action', 'price.publish'), ('contract', True)]:
            with self.subTest(field=field):
                ack = native_receive(request)
                ack[field] = value
                self.assert_unverified(request, ack)

    def test_ack_requires_unique_used_receipt_ids(self):
        request = receive_request()
        for value in ([101, 101], [101, 102], [True], [], [0]):
            with self.subTest(value=value):
                ack = native_receive(request)
                ack['receipt_ids'] = value
                self.assert_unverified(request, ack)

    def test_ack_requires_every_exact_allocation_and_native_cost(self):
        request = receive_request()
        for field, value in [('quantity', 3), ('quantity_base', 3), ('article_id', 42),
                             ('order_line_key', [55, 0, 41]), ('order_line_key', [55, False, 42]),
                             ('xml_line_id', 4.0), ('por_mayor', 0), ('receipt_id', 102),
                             ('cost_confirmed', 1), ('unit_cost_base', 101),
                             ('average_cost_base', float('nan'))]:
            with self.subTest(field=field, value=value):
                ack = native_receive(request)
                ack['lines'][0][field] = value
                self.assert_unverified(request, ack)
        ack = native_receive(request)
        ack['lines'] = []
        self.assert_unverified(request, ack)

    def test_financial_flag_without_actual_identity_or_total_is_insufficient(self):
        request = receive_request()
        for field, value in [('primary_key', ['002', '123']), ('detail_keys', []),
                             ('invoice_key', 'different-fiscal-key'), ('amount_total', 225),
                             ('currency', 'USD')]:
            with self.subTest(field=field):
                ack = native_receive(request)
                ack['payable'][field] = value
                self.assert_unverified(request, ack)
        ack = native_receive(request)
        ack['payable']['detail_keys'] *= 2
        self.assert_unverified(request, ack)

    def test_split_physical_receipts_keep_one_invoice_payable_identity(self):
        first = receive_request()
        binding = native_receive(first)['payable']
        second = copy.deepcopy(first)
        second['operation_id'] = '9a9f5c4a-98a1-4054-a90b-a492fc71c6f8'
        second['receipt']['lines'][0]['allocation_id'] = 'remaining-physical-receipt'
        rehash(second)
        ack = native_receive(second)
        ack['receipt_ids'] = [102]
        ack['lines'][0]['receipt_id'] = 102
        self.assertEqual(contract.reconcile(second, ack, invoice_binding=binding), ack)
        ack['payable']['detail_keys'][0][2:] = ['002', 'other-invoice']
        self.assertEqual(self.assert_unverified(second, ack, invoice_binding=binding),
                         'PURCHASE_INVOICE_ALREADY_BOUND_TO_DIFFERENT_PAYABLE')

    def test_replay_must_be_identical_even_if_another_ack_is_individually_plausible(self):
        request = receive_request()
        original = native_receive(request)
        self.assertEqual(contract.reconcile(request, original, previous_result=original), original)
        altered = copy.deepcopy(original)
        altered['lines'][0]['average_cost_base'] = 80
        self.assertEqual(self.assert_unverified(request, altered, previous_result=original),
                         'PURCHASE_EFFECT_RESULT_REPLAY_CONFLICT')
        changed = copy.deepcopy(request)
        changed['receipt']['lines'][0]['unit_cost'] = changed['receipt']['lines'][0]['unit_cost_base'] = 90
        changed['receipt']['totals'] = {'untaxed': 180, 'tax': 26, 'total': 206}
        rehash(changed)
        self.assert_unverified(changed, original)

    def test_untaxed_amount_must_be_covered_by_actual_allocations(self):
        for net, tax in ((199, 26), (201, 26), (226, 0)):
            with self.subTest(net=net, tax=tax):
                request = receive_request()
                request['receipt']['totals'] = {'untaxed': net, 'tax': tax, 'total': net + tax}
                self.assertEqual(self.assert_bad_request(rehash(request)),
                                 'PURCHASE_RECEIPT_UNTAXED_ALLOCATION_MISMATCH')
        # Losing a positive-cost XML allocation or adding an unallocated charge
        # cannot be disguised by a self-consistent header total.
        request = receive_request()
        request['receipt']['lines'][0]['quantity'] = 1
        request['receipt']['lines'][0]['quantity_base'] = 1
        self.assert_bad_request(rehash(request))

    def test_untaxed_precision_bound_is_only_native_four_decimal_rounding(self):
        request = receive_request()
        line = request['receipt']['lines'][0]
        line['quantity'] = line['quantity_base'] = 100
        line['unit_cost'] = line['unit_cost_base'] = 0.3333
        request['receipt']['totals'] = {'untaxed': 33.3349, 'tax': 0, 'total': 33.3349}
        self.assertEqual(contract.request(rehash(request)), request)
        # The bound is 100 units x0.00005 plus0.00005 for the header.
        # It must not become the currency's0.01 or an assumed discount tolerance.
        request['receipt']['totals'] = {'untaxed': 33.336, 'tax': 0, 'total': 33.336}
        self.assert_bad_request(rehash(request))

    def test_price_net_amount_vat_and_identity_are_confirmed(self):
        request = price_request()
        ack = native_price(request)
        self.assertEqual(contract.reconcile_price(request, ack), ack)
        for field, value in [('unit_price_excluded_tax', 169.639442), ('currency', 'USD'),
                             ('level', 2), ('article_id', 42), ('vat_rate', 15)]:
            with self.subTest(field=field):
                altered = native_price(request)
                altered['effective_price'][field] = value
                self.assert_unverified(request, altered)
        ack['price_confirmed'] = 1
        self.assert_unverified(request, ack)

    def test_no_default_fifteen_percent_vat_and_no_nonfinite_price(self):
        request = price_request()
        request['price']['vat_rate'] = 15
        self.assert_bad_request(rehash(request))
        request = price_request()
        request['price']['unit_price_excluded_tax'] = float('inf')
        self.assert_bad_request(request)
        request = price_request()
        del request['price']['currency']
        self.assert_bad_request(rehash(request))

    def test_default_boundary_blocks_without_invoking_executor(self):
        executor = Mock(side_effect=AssertionError('must not execute'))
        for config in ({}, {'native_executor': executor},
                       {'native_executor': executor, 'certified_actions': {'purchase.receive'}}):
            with self.subTest(config=config):
                result = contract.execute(receive_request_v2(), application_id=APP,
                                          source_instance=SOURCE, **config)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['code'], 'BLOCKED_NATIVE_PURCHASE_NOT_CERTIFIED')
                self.assertNotIn('stock_confirmed', result)
        executor.assert_not_called()

    def test_boundary_uses_explicit_trusted_scope_and_action_certification(self):
        executor = Mock(side_effect=native_receive)
        boundary = contract.NativeEffectsBoundary(APP, SOURCE, native_executor=executor,
            certified_actions={'purchase.receive'}, evidence_reference='laboratory/certified-fixture')
        self.assertEqual(boundary.apply(receive_request_v2())['status'], 'synchronized')
        self.assertEqual(boundary.apply(price_request_v2())['status'], 'blocked')
        wrong = receive_request_v2()
        wrong['source_instance'] = 'untrusted'
        with self.assertRaises(contract.PurchaseEffectsError):
            boundary.apply(wrong)
        self.assertEqual(executor.call_count, 1)

    def test_durable_replay_does_not_invoke_executor_and_unknown_commit_is_not_rejected(self):
        request = receive_request_v2()
        original = native_receive(request)
        executor = Mock(side_effect=contract.NativeResultUnknown('native result requires reconciliation'))
        boundary = contract.NativeEffectsBoundary(APP, SOURCE, native_executor=executor,
            certified_actions={'purchase.receive'}, evidence_reference='laboratory/certified-fixture')
        self.assertEqual(boundary.apply(request, previous_result=original), original)
        executor.assert_not_called()
        with self.assertRaises(contract.NativeResultUnknown):
            boundary.apply(request)
        self.assertEqual(executor.call_count, 1)

    def test_structured_rejection_is_not_a_claim_of_native_success(self):
        request = receive_request()
        result = {**common(request), 'status': 'rejected', 'code': 'ORDER_NO_LONGER_OPEN'}
        self.assertEqual(contract.reconcile(request, result), result)
        result['stock_confirmed'] = True
        self.assert_unverified(request, result)

    def test_v1_is_historical_only_even_with_a_certified_callback(self):
        executor = Mock(side_effect=native_receive)
        boundary = contract.NativeEffectsBoundary(APP, SOURCE, native_executor=executor,
            certified_actions=contract.ACTIONS, evidence_reference='laboratory/certified-fixture')
        request = receive_request()
        self.assertEqual(boundary.apply(request)['code'], 'BLOCKED_PURCHASE_FISCAL_V2_REQUIRED')
        self.assertEqual(boundary.apply(price_request())['code'], 'BLOCKED_PURCHASE_FISCAL_V2_REQUIRED')
        historical = native_receive(request)
        self.assertEqual(boundary.apply(request, previous_result=historical), historical)
        executor.assert_not_called()

    def test_v2_ordinary_crc_source_and_separate_physical_date_are_preserved(self):
        request = receive_request_v2()
        self.assertEqual(contract.request(request), request)
        self.assertEqual(contract.execution_blockers(request), [])
        self.assertIsNone(contract.request(request)['receipt']['fiscal']['source_exchange_rate'])
        result = native_receive(request)
        self.assertEqual(contract.reconcile(request, result), result)
        request['receipt']['fiscal']['source_exchange_rate'] = 1.0
        self.assertEqual(contract.request(rehash(request)), request)

    def test_v2_requires_crc_identity_rate_without_reinterpreting_foreign_values(self):
        for field, value in [('source_currency', 'USD'), ('source_exchange_rate', 530),
                             ('exchange_rate', 530), ('exchange_rate', True)]:
            with self.subTest(field=field):
                request = receive_request_v2()
                request['receipt']['fiscal'][field] = value
                self.assert_bad_request(rehash(request))
        request = receive_request_v2()
        request['receipt']['currency'] = 'USD'
        self.assert_bad_request(rehash(request))
        request = price_request_v2()
        self.assertEqual(contract.reconcile(request, native_price(request))['status'], 'synchronized')
        request['price']['currency'] = 'USD'
        self.assert_bad_request(rehash(request))

    def test_v2_fiscal_identity_timestamp_and_complete_stock_coverage_are_required(self):
        for field, value in [('xml_sha256', 'not-a-digest'), ('issued_at', '2026-11-18T08:00:00'),
                             ('issued_at', '2026-11-17T08:00:00-06:00'), ('physical_receipt_date', None)]:
            with self.subTest(field=field):
                request = receive_request_v2()
                request['receipt']['fiscal'][field] = value
                self.assert_bad_request(rehash(request))
        for field, value in [('allocation_ids', []), ('allocation_ids', ['missing']),
                             ('quantity_base', 3), ('conversion_factor', 2),
                             ('xml_line_id', 5), ('cabys', '')]:
            with self.subTest(field=field):
                request = receive_request_v2()
                request['receipt']['fiscal']['lines'][0][field] = value
                self.assert_bad_request(rehash(request))

    def test_v2_tax_is_declared_not_inferred_from_rate(self):
        request = receive_request_v2()
        source = request['receipt']['fiscal']['lines'][0]
        # A deliberately non-rate-derived amount must be transported faithfully.
        source['taxes'] = [{'code': '01', 'rate_code': '08', 'rate': 13, 'base_amount': 200,
            'amount': 25.1234, 'net_amount': 25.1234, 'exoneration_percentage': 0,
            'exoneration_amount': 0, 'included_in_cost': False, 'exoneration_xml': None}]
        source['net_tax_amount'] = 25.1234
        request['receipt']['totals'].update(tax=25.1234, total=225.1234)
        rehash(request)
        self.assertEqual(contract.request(request), request)
        self.assertIn('BLOCKED_PURCHASE_TAX_NOT_CERTIFIED', contract.execution_blockers(request))
        self.assertEqual(contract.reconcile(request, native_receive(request))['status'], 'synchronized')
        source['taxes'][0]['amount'] = 26
        self.assert_bad_request(rehash(request))

    def test_v2_exoneration_legal_fragment_and_assumed_tax_are_not_lost_or_reallocated(self):
        request = receive_request_v2()
        source = request['receipt']['fiscal']['lines'][0]
        fragment = '<Exoneracion><NumeroDocumento>EX-001</NumeroDocumento><Articulo>9</Articulo></Exoneracion>'
        source['taxes'] = [{'code': '01', 'rate_code': '08', 'rate': 13, 'base_amount': 200,
            'amount': 26, 'net_amount': 16, 'exoneration_percentage': None,
            'exoneration_amount': 10, 'included_in_cost': False, 'exoneration_xml': fragment}]
        source['assumed_tax_amount'] = 6
        source['net_tax_amount'] = 10
        request['receipt']['totals'].update(tax=10, total=210)
        rehash(request)
        checked = contract.request(request)
        self.assertEqual(checked['receipt']['fiscal']['lines'][0]['taxes'][0]['exoneration_xml'], fragment)
        self.assertIsNone(checked['receipt']['fiscal']['lines'][0]['taxes'][0]['exoneration_percentage'])
        self.assertNotIn('assumed_tax_amount', source['taxes'][0])
        source['taxes'][0]['exoneration_xml'] = None
        self.assert_bad_request(rehash(request))

    def test_v2_services_and_other_charges_keep_financial_amount_without_stock(self):
        request = receive_request_v2()
        fiscal = request['receipt']['fiscal']
        service = copy.deepcopy(fiscal['lines'][0])
        service.update(xml_line_id=5, xml_sequence=2, inventory_effect='none', quantity=1,
            quantity_base=None, conversion_factor=None, conversion_mode=None,
            unit_price=10, gross_amount=10, net_amount=10, allocation_ids=[])
        fiscal['lines'].append(service)
        fiscal['other_charges'] = [{'xml_index': 1, 'type': '06', 'detail': 'Servicio tercero',
            'percentage': None, 'amount': 5,
            'source_xml': '<OtrosCargos><TipoDocumentoOC>06</TipoDocumentoOC><NombreTercero>Proveedor tercero</NombreTercero><MontoCargo>5</MontoCargo></OtrosCargos>'}]
        request['receipt']['totals'] = {'untaxed': 210, 'tax': 0, 'total': 215}
        rehash(request)
        self.assertEqual(contract.request(request), request)
        self.assertEqual(len(request['receipt']['lines']), 1)
        self.assertIn('BLOCKED_PURCHASE_NON_STOCK_FISCAL_NOT_CERTIFIED', contract.execution_blockers(request))
        self.assertIn('BLOCKED_PURCHASE_CHARGES_REFUND_NOT_CERTIFIED', contract.execution_blockers(request))
        service['quantity_base'] = 0
        self.assert_bad_request(rehash(request))

    def test_v2_discount_and_zero_cost_promotion_preserve_original_fiscal_lines(self):
        request = receive_request_v2()
        fiscal = request['receipt']['fiscal']; source = fiscal['lines'][0]
        source['unit_price'] = 125; source['gross_amount'] = 250
        source['discounts'] = [{'code': '07', 'amount': 50, 'nature': 'Descuento comercial',
            'source_xml': '<Descuento><MontoDescuento>50</MontoDescuento><CodigoDescuento>07</CodigoDescuento></Descuento>'}]
        gift = copy.deepcopy(source)
        gift.update(xml_line_id=5, xml_sequence=2, quantity=1, quantity_base=1,
                    unit_price=0, gross_amount=0, discounts=[], net_amount=0,
                    promotion={'type': 'regalia', 'parent_xml_line_id': 4}, allocation_ids=['gift'])
        fiscal['lines'].append(gift)
        allocation = copy.deepcopy(request['receipt']['lines'][0])
        allocation.update(allocation_id='gift', xml_line_id=5, quantity=1, quantity_base=1,
                          unit_cost=0, unit_cost_base=0)
        request['receipt']['lines'].append(allocation)
        rehash(request)
        self.assertEqual(contract.request(request), request)
        blockers = contract.execution_blockers(request)
        self.assertIn('BLOCKED_PURCHASE_PROMOTION_NOT_CERTIFIED', blockers)
        self.assertIn('BLOCKED_PURCHASE_DISCOUNT_NOT_CERTIFIED', blockers)
        self.assertIn('BLOCKED_PURCHASE_NATIVE_SPLIT_NOT_CERTIFIED', blockers)
        gift['promotion']['parent_xml_line_id'] = 999
        self.assert_bad_request(rehash(request))

    def test_v2_credit_is_a_labelled_proposal_not_a_native_due_date_rule(self):
        request = receive_request_v2()
        terms = request['receipt']['fiscal']['payment_terms']
        terms.update(condition_code='02', credit_days=30, due_date='2026-12-18', due_date_source='odoo_credit_days')
        rehash(request)
        self.assertEqual(contract.request(request), request)
        self.assertIn('BLOCKED_PURCHASE_CREDIT_NOT_CERTIFIED', contract.execution_blockers(request))
        terms['due_date_source'] = 'cash_issue_date'
        self.assert_bad_request(rehash(request))

    def test_v2_special_cases_never_reach_a_generic_certified_callback(self):
        request = receive_request_v2()
        request['receipt']['fiscal']['payment_terms'].update(condition_code='02', credit_days=30,
            due_date='2026-12-18', due_date_source='odoo_credit_days')
        rehash(request)
        executor = Mock(side_effect=AssertionError('special fiscal case is not certified'))
        boundary = contract.NativeEffectsBoundary(APP, SOURCE, native_executor=executor,
            certified_actions=contract.ACTIONS, evidence_reference='laboratory/general-only')
        self.assertEqual(boundary.apply(request)['code'], 'BLOCKED_PURCHASE_CREDIT_NOT_CERTIFIED')
        executor.assert_not_called()

    def test_v2_fiscal_ack_requires_original_digest_physical_date_and_payable_terms(self):
        request = receive_request_v2()
        for field, value in [('fiscal_confirmed', 1), ('fiscal_checksum', 'd' * 64),
                             ('physical_receipt_date', '2026-11-18')]:
            with self.subTest(field=field):
                ack = native_receive(request); ack[field] = value
                self.assert_unverified(request, ack)
        ack = native_receive(request)
        ack['payable']['payment_terms']['due_date_source'] = 'xml'
        self.assert_unverified(request, ack)
        ack = native_receive(request)
        ack['payable']['exchange_rate'] = 530
        self.assert_unverified(request, ack)

    def test_v2_audit_fragments_cannot_expand_entities_or_hide_another_root(self):
        for fragment in ('<!DOCTYPE x [<!ENTITY y "value">]><Descuento>&y;</Descuento>',
                         '<OtrosCargos/>', '<Descuento>', '<Descuento>' + 'x' * 65536 + '</Descuento>'):
            with self.subTest(fragment=fragment[:50]):
                request = receive_request_v2()
                source = request['receipt']['fiscal']['lines'][0]
                source['discounts'] = [{'code': None, 'amount': 0, 'nature': '', 'source_xml': fragment}]
                self.assert_bad_request(rehash(request))


if __name__ == '__main__':
    unittest.main()
