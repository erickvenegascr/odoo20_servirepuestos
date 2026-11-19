"""Pure contracts for requested purchase/price effects, never a financial writer.

The checker verifies identities, allocations, totals and native confirmations. It
cannot establish that a caller's booleans describe real SGA effects. Only inject
an executor independently certified against the native application, with durable
operation deduplication and invoice-level AP reconciliation. No SQL, recalc,
network, averaging formula or implicit inventory action exists in this module.
"""
import copy
import hashlib
import json
import math
import re
import uuid
from datetime import date
from decimal import Decimal


VERSION = 1
MAX_ID = 2147483647
MAX_SMALLINT = 32767
MAX_LINES = 500
# MariaDB schema: DOUBLE(19,4), INT quantity, SMALLINT pack factor/warehouse.
MAX_MONEY = Decimal('999999999999999.9999')
MONEY_TOLERANCE = Decimal('0.0001')
NATIVE_HALF_QUANTUM = Decimal('0.00005')
COMMON = {'contract', 'action', 'operation_id', 'application_id', 'source_instance'}
ACTIONS = {'purchase.receive', 'price.publish'}
RECEIPT_FIELDS = {
    'invoice_key', 'invoice_number', 'invoice_date', 'document_id', 'vendor_code',
    'warehouse', 'order_id', 'currency', 'order_checksum', 'totals', 'lines',
}
LINE_FIELDS = {
    'allocation_id', 'xml_line_id', 'order_line_key', 'article_id', 'por_mayor',
    'quantity', 'quantity_base', 'unit_factor', 'unit_cost', 'unit_cost_base',
}
PRICE_FIELDS = {
    'article_id', 'level', 'currency', 'unit_price_excluded_tax', 'vat_code',
    'vat_rate', 'document_id',
}
PAYABLE_FIELDS = {'invoice_key', 'primary_key', 'detail_keys', 'currency', 'amount_total'}
NATIVE_LINE_FIELDS = LINE_FIELDS - {'unit_factor', 'unit_cost'} | {
    'receipt_id', 'cost_confirmed', 'average_cost_base',
}
VAT_RATES = {'01': 0, '02': 1, '03': 2, '04': 4, '08': 13, '09': .5, '10': 0, '11': 0}


class PurchaseEffectsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NativeResultUnverified(PurchaseEffectsError):
    """A possible native commit is not proven; do not cache a final rejection."""


class NativeResultUnknown(RuntimeError):
    """Executor must reconcile its durable receipt with the original UUID."""


def digest(value):
    """Same canonical JSON digest as article_contract and purchase_source."""
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        raise PurchaseEffectsError('INVALID_PURCHASE_EFFECTS_JSON') from None
    return hashlib.sha256(raw).hexdigest()


def _fail(code):
    raise PurchaseEffectsError(code)


def _keys(value, required, code):
    if not isinstance(value, dict) or set(value) != required:
        _fail(code)


def _text(value, maximum, code):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum
            or any(ord(c) < 32 or ord(c) > 65535 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail(code)
    return value


def _identity(value):
    if not isinstance(value, str):
        _fail('INVALID_PURCHASE_EFFECT_IDENTITY')
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        _fail('INVALID_PURCHASE_EFFECT_IDENTITY')


def _integer(value, maximum=MAX_ID, minimum=1):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or type(value) is float and not math.isfinite(value) or int(value) != value):
        _fail('INVALID_PURCHASE_EFFECT_INTEGER')
    return int(value)


def _id(value, maximum=MAX_ID, minimum=1):
    if type(value) is not int:
        _fail('INVALID_PURCHASE_EFFECT_INTEGER')
    return _integer(value, maximum, minimum)


def _number(value, *, maximum=MAX_MONEY):
    if type(value) not in (int, float):
        _fail('INVALID_PURCHASE_EFFECT_AMOUNT')
    if type(value) is float and not math.isfinite(value):
        _fail('INVALID_PURCHASE_EFFECT_AMOUNT')
    if not 0 <= value <= maximum:
        _fail('INVALID_PURCHASE_EFFECT_AMOUNT')
    return Decimal(str(value))


def _equal_money(left, right):
    return abs(_number(left) - _number(right)) <= MONEY_TOLERANCE


def _currency(value):
    # This is the ISO name in the contract, NOT SGA's VARCHAR(2) Moneda key.
    # An independently verified mapping must be supplied by the executor.
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z]{3}', value):
        _fail('INVALID_PURCHASE_EFFECT_CURRENCY')
    return value


def _vendor(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9A-Za-z]{1,3}', value):
        _fail('INVALID_PURCHASE_EFFECT_VENDOR')
    return value


def _checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        _fail('INVALID_PURCHASE_EFFECT_CHECKSUM')
    return value


def _order_key(value, order_id=None):
    if not isinstance(value, list) or len(value) != 3 or type(value[1]) is not bool:
        _fail('INVALID_PURCHASE_ORDER_LINE_KEY')
    _integer(value[0]); _integer(value[2])
    if type(value[0]) is not int or type(value[2]) is not int:
        _fail('INVALID_PURCHASE_ORDER_LINE_KEY')
    if order_id is not None and value[0] != order_id:
        _fail('PURCHASE_RECEIPT_ORDER_IDENTITY_MISMATCH')
    return value


def receipt(value):
    _keys(value, RECEIPT_FIELDS, 'INVALID_PURCHASE_RECEIPT_FIELDS')
    _text(value['invoice_key'], 128, 'INVALID_PURCHASE_INVOICE_KEY')
    _text(value['invoice_number'], 20, 'PURCHASE_INVOICE_NUMBER_EXCEEDS_SGA_FORMAT')
    _text(value['invoice_date'], 10, 'INVALID_PURCHASE_INVOICE_DATE')
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value['invoice_date']):
            raise ValueError()
        date.fromisoformat(value['invoice_date'])
    except ValueError:
        _fail('INVALID_PURCHASE_INVOICE_DATE')
    _id(value['document_id']); _id(value['order_id'])
    _id(value['warehouse'], MAX_SMALLINT, 0)
    _vendor(value['vendor_code']); _currency(value['currency']); _checksum(value['order_checksum'])
    totals = value['totals']
    _keys(totals, {'untaxed', 'tax', 'total'}, 'INVALID_PURCHASE_INVOICE_TOTALS')
    amounts = {key: _number(amount) for key, amount in totals.items()}
    if abs(amounts['untaxed'] + amounts['tax'] - amounts['total']) > MONEY_TOLERANCE:
        _fail('PURCHASE_INVOICE_TOTALS_MISMATCH')
    lines = value['lines']
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        _fail('INVALID_PURCHASE_RECEIPT_LINES')
    seen, grouped = set(), {}
    allocated_untaxed, native_units = Decimal(0), 0
    for line in lines:
        _keys(line, LINE_FIELDS, 'INVALID_PURCHASE_RECEIPT_LINE_FIELDS')
        allocation = _text(line['allocation_id'], 128, 'INVALID_PURCHASE_ALLOCATION')
        if allocation in seen:
            _fail('DUPLICATE_PURCHASE_ALLOCATION')
        seen.add(allocation)
        _id(line['xml_line_id']); _id(line['article_id'])
        key = _order_key(line['order_line_key'], value['order_id'])
        if type(line['por_mayor']) is not bool or key[1] != line['por_mayor'] or key[2] != line['article_id']:
            _fail('PURCHASE_RECEIPT_LINE_IDENTITY_MISMATCH')
        factor = _id(line['unit_factor'], MAX_SMALLINT)
        quantity = _integer(line['quantity']); quantity_base = _integer(line['quantity_base'])
        expected_quantity = quantity * factor if line['por_mayor'] else quantity
        if quantity_base != expected_quantity:
            _fail('PURCHASE_RECEIPT_UNIT_MISMATCH')
        unit_cost = _number(line['unit_cost']); cost_base = _number(line['unit_cost_base'])
        expected_cost = unit_cost / factor if line['por_mayor'] else unit_cost
        if abs(cost_base - expected_cost) > MONEY_TOLERANCE:
            _fail('PURCHASE_RECEIPT_UNIT_COST_MISMATCH')
        allocated_untaxed += unit_cost * quantity
        native_units += quantity
        group_key = tuple(key)
        conditions = (factor, unit_cost, cost_base)
        if group_key in grouped:
            prior_conditions, total_quantity = grouped[group_key]
            if conditions != prior_conditions:
                _fail('PURCHASE_RECEIPT_NATIVE_KEY_CONFLICT')
            _integer(total_quantity + quantity)
            grouped[group_key] = (conditions, total_quantity + quantity)
        else:
            grouped[group_key] = (conditions, quantity)
    # Contract 1 covers ordinary merchandise with net costs allocated to lines.
    # It cannot represent unallocated charges/discounts or capitalized taxes/CIF.
    # This is only an arithmetic check, not an inferred VAT or native AP rule.
    # Each native unit cost and the header amount may round by half a 4-decimal
    # quantum; do not enlarge this bound using a commercial/currency tolerance.
    precision_bound = NATIVE_HALF_QUANTUM * (native_units + 1)
    if abs(allocated_untaxed - amounts['untaxed']) > precision_bound:
        _fail('PURCHASE_RECEIPT_UNTAXED_ALLOCATION_MISMATCH')
    # Retain exact JSON numbers: never silently change the frozen invoice/hash.
    return copy.deepcopy(value)


def price(value):
    _keys(value, PRICE_FIELDS, 'INVALID_PURCHASE_PRICE_FIELDS')
    _id(value['article_id']); _id(value['level'], MAX_SMALLINT)
    _id(value['document_id']); _currency(value['currency'])
    _number(value['unit_price_excluded_tax'])
    rate = _number(value['vat_rate'], maximum=Decimal('100'))
    if (not isinstance(value['vat_code'], str) or value['vat_code'] not in VAT_RATES
            or rate != Decimal(str(VAT_RATES[value['vat_code']]))):
        _fail('INVALID_PURCHASE_PRICE_VAT')
    return copy.deepcopy(value)


def request(value):
    if (not isinstance(value, dict) or type(value.get('contract')) is not int
            or value['contract'] != VERSION or not isinstance(value.get('action'), str)
            or value['action'] not in ACTIONS):
        _fail('INVALID_PURCHASE_EFFECT_REQUEST')
    body_name = 'receipt' if value['action'] == 'purchase.receive' else 'price'
    _keys(value, COMMON | {'request_checksum', body_name}, 'INVALID_PURCHASE_EFFECT_REQUEST')
    result = copy.deepcopy(value)
    for name in ('operation_id', 'application_id'):
        result[name] = _identity(value[name])
    _text(value['source_instance'], 80, 'INVALID_PURCHASE_EFFECT_SOURCE')
    _checksum(value['request_checksum'])
    result[body_name] = receipt(value[body_name]) if body_name == 'receipt' else price(value[body_name])
    if digest(result[body_name]) != value['request_checksum']:
        _fail('PURCHASE_EFFECT_REQUEST_CHECKSUM_MISMATCH')
    return result


def _payable(value, expected):
    _keys(value, PAYABLE_FIELDS, 'INVALID_PURCHASE_PAYABLE_CONFIRMATION')
    if value['invoice_key'] != expected['invoice_key']:
        _fail('PURCHASE_PAYABLE_INVOICE_IDENTITY_MISMATCH')
    primary = value['primary_key']
    if not isinstance(primary, list) or primary != [expected['vendor_code'], expected['invoice_number']]:
        _fail('PURCHASE_PAYABLE_PRIMARY_IDENTITY_MISMATCH')
    details = value['detail_keys']
    if not isinstance(details, list) or not 1 <= len(details) <= MAX_LINES:
        _fail('INVALID_PURCHASE_PAYABLE_DETAILS')
    keys = set()
    for key in details:
        if not isinstance(key, list) or len(key) != 4 or key[:2] != primary:
            _fail('PURCHASE_PAYABLE_DETAIL_IDENTITY_MISMATCH')
        _vendor(key[0]); _text(key[1], 20, 'INVALID_PURCHASE_PAYABLE_DETAILS')
        _vendor(key[2]); _text(key[3], 20, 'INVALID_PURCHASE_PAYABLE_DETAILS')
        if tuple(key) in keys:
            _fail('DUPLICATE_PURCHASE_PAYABLE_IDENTITY')
        keys.add(tuple(key))
    _currency(value['currency'])
    if value['currency'] != expected['currency'] or not _equal_money(value['amount_total'], expected['totals']['total']):
        _fail('PURCHASE_PAYABLE_TOTAL_OR_CURRENCY_MISMATCH')


def _receive_result(operation, result, invoice_binding):
    required = COMMON | {
        'status', 'request_checksum', 'receipt_ids', 'order_id', 'stock_confirmed',
        'valuation_confirmed', 'ap_confirmed', 'order_verified', 'payable', 'lines',
    }
    _keys(result, required, 'INVALID_PURCHASE_RECEIPT_CONFIRMATION')
    for flag in ('stock_confirmed', 'valuation_confirmed', 'ap_confirmed', 'order_verified'):
        if result[flag] is not True:
            _fail('PURCHASE_RECEIPT_NATIVE_EFFECT_UNCONFIRMED')
    expected = operation['receipt']
    if type(result['order_id']) is not int or result['order_id'] != expected['order_id']:
        _fail('PURCHASE_RECEIPT_ORDER_IDENTITY_MISMATCH')
    receipt_ids = result['receipt_ids']
    if not isinstance(receipt_ids, list) or not 1 <= len(receipt_ids) <= MAX_LINES:
        _fail('INVALID_NATIVE_PURCHASE_RECEIPT_IDENTITIES')
    for number in receipt_ids:
        if type(number) is not int:
            _fail('INVALID_NATIVE_PURCHASE_RECEIPT_IDENTITIES')
        _integer(number)
    if len(set(receipt_ids)) != len(receipt_ids):
        _fail('DUPLICATE_NATIVE_PURCHASE_RECEIPT_IDENTITY')
    _payable(result['payable'], expected)
    if invoice_binding is not None:
        _payable(invoice_binding, expected)
        # Another receipt for the same invoice must resolve the same AP identity.
        if (invoice_binding['primary_key'] != result['payable']['primary_key']
                or {tuple(k) for k in invoice_binding['detail_keys']} != {tuple(k) for k in result['payable']['detail_keys']}):
            _fail('PURCHASE_INVOICE_ALREADY_BOUND_TO_DIFFERENT_PAYABLE')
    lines = result['lines']
    if not isinstance(lines, list) or len(lines) != len(expected['lines']):
        _fail('PURCHASE_RECEIPT_ALLOCATION_COUNT_MISMATCH')
    expected_lines = {line['allocation_id']: line for line in expected['lines']}
    observed, used_receipts, native_rows = set(), set(), {}
    for line in lines:
        _keys(line, NATIVE_LINE_FIELDS, 'INVALID_PURCHASE_NATIVE_LINE_FIELDS')
        allocation = line['allocation_id']
        if not isinstance(allocation, str) or allocation not in expected_lines or allocation in observed:
            _fail('PURCHASE_RECEIPT_ALLOCATION_IDENTITY_MISMATCH')
        observed.add(allocation); wanted = expected_lines[allocation]
        _order_key(line['order_line_key'], expected['order_id'])
        _id(line['xml_line_id']); _id(line['article_id'])
        if type(line['por_mayor']) is not bool:
            _fail('PURCHASE_RECEIPT_CONFIRMED_LINE_MISMATCH')
        for field in ('xml_line_id', 'article_id', 'por_mayor', 'order_line_key', 'quantity', 'quantity_base'):
            if type(line[field]) is not type(wanted[field]) or line[field] != wanted[field]:
                # Quantity JSON int/float forms may differ, but booleans never qualify.
                if field not in ('quantity', 'quantity_base') or type(line[field]) not in (int, float) or line[field] != wanted[field]:
                    _fail('PURCHASE_RECEIPT_CONFIRMED_LINE_MISMATCH')
        _integer(line['quantity']); _integer(line['quantity_base'])
        if type(line['receipt_id']) is not int or line['receipt_id'] not in receipt_ids:
            _fail('PURCHASE_RECEIPT_NATIVE_LINE_IDENTITY_MISMATCH')
        if line['cost_confirmed'] is not True or not _equal_money(line['unit_cost_base'], wanted['unit_cost_base']):
            _fail('PURCHASE_RECEIPT_COST_UNCONFIRMED')
        _number(line['average_cost_base'])  # native result, never a local average.
        used_receipts.add(line['receipt_id'])
        native_key = (line['receipt_id'], line['por_mayor'], line['article_id'])
        conditions = (_number(line['unit_cost_base']), _number(line['average_cost_base']))
        if native_key in native_rows and native_rows[native_key] != conditions:
            _fail('PURCHASE_RECEIPT_NATIVE_KEY_CONFLICT')
        native_rows[native_key] = conditions
    if used_receipts != set(receipt_ids):
        _fail('PURCHASE_RECEIPT_UNUSED_NATIVE_IDENTITY')


def _price_result(operation, result):
    _keys(result, COMMON | {'status', 'request_checksum', 'price_confirmed', 'effective_price'},
          'INVALID_NATIVE_PRICE_CONFIRMATION')
    if result['price_confirmed'] is not True:
        _fail('NATIVE_PRICE_UNCONFIRMED')
    effective = result['effective_price']; expected = operation['price']
    _keys(effective, PRICE_FIELDS - {'document_id'}, 'INVALID_NATIVE_EFFECTIVE_PRICE')
    checked = price({**effective, 'document_id': expected['document_id']})
    for key in ('article_id', 'level', 'currency', 'vat_code', 'vat_rate'):
        if type(checked[key]) is bool or checked[key] != expected[key]:
            _fail('NATIVE_PRICE_IDENTITY_MISMATCH')
    if not _equal_money(checked['unit_price_excluded_tax'], expected['unit_price_excluded_tax']):
        _fail('NATIVE_EFFECTIVE_PRICE_MISMATCH')


def reconcile(payload, result, *, invoice_binding=None, previous_result=None):
    """Check native proof content; caller persists AP bindings and durable receipts.

    A failed confirmation may follow a native commit. Raise an unverified result
    instead of converting it to a final business rejection or inventing a retry.
    """
    operation = request(payload)
    try:
        if not isinstance(result, dict):
            _fail('INVALID_PURCHASE_EFFECT_RESULT')
        for key in COMMON | {'request_checksum'}:
            if key not in result or type(result[key]) is not type(operation[key]) or result[key] != operation[key]:
                _fail('PURCHASE_EFFECT_RESULT_IDENTITY_MISMATCH')
        if result.get('status') in ('blocked', 'rejected'):
            _keys(result, COMMON | {'status', 'request_checksum', 'code'}, 'INVALID_PURCHASE_EFFECT_RESULT')
            if not isinstance(result['code'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', result['code']):
                _fail('INVALID_PURCHASE_EFFECT_RESULT_CODE')
        elif result.get('status') == 'synchronized':
            if operation['action'] == 'purchase.receive':
                _receive_result(operation, result, invoice_binding)
            else:
                _price_result(operation, result)
        else:
            _fail('PURCHASE_EFFECT_RESULT_UNKNOWN')
        if previous_result is not None:
            if not isinstance(previous_result, dict) or digest(previous_result) != digest(result):
                _fail('PURCHASE_EFFECT_RESULT_REPLAY_CONFLICT')
        return copy.deepcopy(result)
    except PurchaseEffectsError as exc:
        raise NativeResultUnverified(exc.code) from None


def reconcile_receive(payload, result, *, invoice_binding=None, previous_result=None):
    if request(payload)['action'] != 'purchase.receive':
        _fail('PURCHASE_RECEIPT_ACTION_REQUIRED')
    return reconcile(payload, result, invoice_binding=invoice_binding, previous_result=previous_result)


def reconcile_price(payload, result, *, previous_result=None):
    if request(payload)['action'] != 'price.publish':
        _fail('PRICE_PUBLISH_ACTION_REQUIRED')
    return reconcile(payload, result, previous_result=previous_result)


class NativeEffectsBoundary:
    """Scoped injection point; disabled unless an independently certified callback is supplied.

    Evidence_reference and certified_actions are trusted deployment configuration,
    never fields accepted from a request. This gate does NOT certify an executor.
    The native implementation must atomically deduplicate operation_id, preserve
    AP identity across receipt partitions and verify actual costs/stock/price.
    It must not use a global recalc or claim success from an isolated UPDATE.
    """
    def __init__(self, application_id, source_instance, *, native_executor=None,
                 certified_actions=(), evidence_reference=None):
        self.application_id = _identity(application_id)
        self.source_instance = _text(source_instance, 80, 'INVALID_PURCHASE_EFFECT_SOURCE')
        self.native_executor = native_executor
        self.certified_actions = frozenset(certified_actions)
        if not self.certified_actions <= ACTIONS:
            _fail('INVALID_NATIVE_EFFECT_CERTIFICATION_SCOPE')
        self.evidence_reference = None
        if evidence_reference is not None:
            self.evidence_reference = _text(evidence_reference, 1024, 'INVALID_NATIVE_EFFECT_EVIDENCE_REFERENCE')
        if native_executor is not None and not callable(native_executor):
            _fail('INVALID_NATIVE_EFFECT_EXECUTOR')

    def apply(self, payload, *, invoice_binding=None, previous_result=None):
        operation = request(payload)
        if operation['application_id'] != self.application_id or operation['source_instance'] != self.source_instance:
            _fail('PURCHASE_EFFECT_SCOPE_MISMATCH')
        if previous_result is not None:
            return reconcile(operation, previous_result, invoice_binding=invoice_binding)
        if (self.native_executor is None or operation['action'] not in self.certified_actions
                or not self.evidence_reference):
            return {**{key: operation[key] for key in COMMON},
                    'request_checksum': operation['request_checksum'], 'status': 'blocked',
                    'code': 'BLOCKED_NATIVE_PURCHASE_NOT_CERTIFIED'}
        # Do not catch timeout/commit-unknown exceptions or persist them as rejection.
        result = self.native_executor(copy.deepcopy(operation))
        return reconcile(operation, result, invoice_binding=invoice_binding)


def execute(payload, *, application_id, source_instance, native_executor=None,
            certified_actions=(), evidence_reference=None, invoice_binding=None,
            previous_result=None):
    return NativeEffectsBoundary(application_id, source_instance,
        native_executor=native_executor, certified_actions=certified_actions,
        evidence_reference=evidence_reference).apply(payload,
            invoice_binding=invoice_binding, previous_result=previous_result)
