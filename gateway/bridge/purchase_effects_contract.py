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
from datetime import date, datetime
from decimal import Decimal
from xml.etree import ElementTree


VERSION = 2
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
FISCAL_FIELDS = {'xml_sha256', 'issued_at', 'physical_receipt_date', 'source_currency',
    'source_exchange_rate', 'exchange_rate', 'payment_terms', 'vat_refund',
    'other_charges', 'lines'}
FISCAL_LINE_FIELDS = {'xml_line_id', 'xml_sequence', 'cabys', 'inventory_effect',
    'quantity', 'quantity_base', 'conversion_factor', 'conversion_mode', 'unit_price',
    'gross_amount', 'discounts', 'net_amount', 'taxes', 'assumed_tax_amount',
    'net_tax_amount', 'promotion', 'allocation_ids'}
TAX_FIELDS = {'code', 'rate_code', 'rate', 'base_amount', 'amount', 'net_amount',
    'exoneration_percentage', 'exoneration_amount', 'included_in_cost', 'exoneration_xml'}
TERMS_FIELDS = {'condition_code', 'credit_days', 'due_date', 'due_date_source'}
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


def receipt(value, *, version=1):
    _keys(value, RECEIPT_FIELDS | ({'fiscal'} if version == 2 else set()), 'INVALID_PURCHASE_RECEIPT_FIELDS')
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
    if version == 1 and abs(amounts['untaxed'] + amounts['tax'] - amounts['total']) > MONEY_TOLERANCE:
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
            if version == 1 and conditions != prior_conditions:
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
    if version == 1 and abs(allocated_untaxed - amounts['untaxed']) > precision_bound:
        _fail('PURCHASE_RECEIPT_UNTAXED_ALLOCATION_MISMATCH')
    if version == 2:
        fiscal(value)
    # Retain exact JSON numbers: never silently change the frozen invoice/hash.
    return copy.deepcopy(value)


def _crc(currency, exchange_rate):
    _currency(currency)
    if currency != 'CRC' or _number(exchange_rate) != 1:
        _fail('PURCHASE_EFFECT_CRC_IDENTITY_RATE_REQUIRED')


def _iso_date(value):
    _text(value, 10, 'INVALID_PURCHASE_FISCAL_DATE')
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError:
        _fail('INVALID_PURCHASE_FISCAL_DATE')


def _source_text(value, maximum, *, empty=False):
    # XML source text is audit material, not a native VARCHAR to normalize.
    if (not isinstance(value, str) or not empty and not value or len(value) > maximum
            or any((ord(c) < 32 and c not in '\t\r\n') or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail('INVALID_PURCHASE_FISCAL_SOURCE_TEXT')
    return value


def _fragment(value, root):
    _source_text(value, 65536)
    # No external resolution, DTD or entity declarations in audit fragments.
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', value, re.I):
        _fail('UNSAFE_PURCHASE_FISCAL_XML_FRAGMENT')
    try:
        element = ElementTree.fromstring(value)
    except (ElementTree.ParseError, ValueError):
        _fail('INVALID_PURCHASE_FISCAL_XML_FRAGMENT')
    if element.tag.rsplit('}', 1)[-1] != root:
        _fail('INVALID_PURCHASE_FISCAL_XML_FRAGMENT_ROOT')


def _fiscal_code(value, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{2}', value):
        _fail('INVALID_PURCHASE_FISCAL_CODE')


def _list(value, maximum=MAX_LINES):
    if not isinstance(value, list) or len(value) > maximum:
        _fail('INVALID_PURCHASE_FISCAL_COLLECTION')
    return value


def _money_matches(actual, expected, code):
    if abs(actual - expected) > MONEY_TOLERANCE:
        _fail(code)


def _terms(value, invoice_date):
    _keys(value, TERMS_FIELDS, 'INVALID_PURCHASE_PAYMENT_TERMS')
    _fiscal_code(value['condition_code'])
    _id(value['credit_days'], MAX_SMALLINT, 0)
    due = _iso_date(value['due_date'])
    if (value['due_date_source'] not in ('xml', 'odoo_credit_days', 'cash_issue_date')
            or due < invoice_date):
        _fail('INVALID_PURCHASE_PAYMENT_DUE_DATE')
    # Cash is the only prospective execution case. Credit terms are transported
    # as a proposal, without claiming that SGA computes due dates this way.
    if value['condition_code'] == '01':
        if value['credit_days'] != 0 or due != invoice_date:
            _fail('PURCHASE_CASH_TERMS_MISMATCH')
    elif value['due_date_source'] == 'cash_issue_date':
        _fail('INVALID_PURCHASE_PAYMENT_DUE_DATE_SOURCE')


def fiscal(receipt_value):
    """Validate complete XML amounts and explicit allocations; never calculate IVA.

    Legal fragments and the original decoded XML digest preserve information the
    older ORM tax projection omits. They are transported, not interpreted as a
    certified SGA tax/cost/credit algorithm. Special cases remain execution-blocked.
    """
    value = receipt_value['fiscal']
    _keys(value, FISCAL_FIELDS, 'INVALID_PURCHASE_FISCAL_FIELDS')
    if not isinstance(receipt_value['invoice_key'], str) or not re.fullmatch(r'[0-9]{50}', receipt_value['invoice_key']):
        _fail('PURCHASE_FISCAL_KEY_REQUIRED')
    _checksum(value['xml_sha256'])
    _crc(receipt_value['currency'], value['exchange_rate'])
    if value['source_currency'] != 'CRC':
        _fail('PURCHASE_EFFECT_CRC_SOURCE_REQUIRED')
    if value['source_exchange_rate'] is not None and _number(value['source_exchange_rate']) != 1:
        _fail('PURCHASE_EFFECT_CRC_SOURCE_RATE_MISMATCH')
    issued = _text(value['issued_at'], 64, 'INVALID_PURCHASE_FISCAL_ISSUED_AT')
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', issued):
            raise ValueError()
        issued_at = datetime.fromisoformat(issued.replace('Z', '+00:00'))
        if issued_at.utcoffset() is None or issued_at.date() != _iso_date(receipt_value['invoice_date']):
            raise ValueError()
    except ValueError:
        _fail('INVALID_PURCHASE_FISCAL_ISSUED_AT')
    _iso_date(value['physical_receipt_date'])
    _terms(value['payment_terms'], issued_at.date())
    refund = _number(value['vat_refund'])
    charge_total, charge_ids = Decimal(0), set()
    for charge in _list(value['other_charges']):
        _keys(charge, {'xml_index', 'type', 'detail', 'percentage', 'amount', 'source_xml'},
              'INVALID_PURCHASE_FISCAL_OTHER_CHARGE')
        _id(charge['xml_index'])
        if charge['xml_index'] in charge_ids:
            _fail('DUPLICATE_PURCHASE_FISCAL_OTHER_CHARGE')
        charge_ids.add(charge['xml_index'])
        _fiscal_code(charge['type'])
        _source_text(charge['detail'], 4096, empty=True)
        if charge['percentage'] is not None:
            _number(charge['percentage'])
        charge_total += _number(charge['amount'])
        _fragment(charge['source_xml'], 'OtrosCargos')
    sources = _list(value['lines'])
    if not sources:
        _fail('PURCHASE_FISCAL_LINES_REQUIRED')
    allocations = {line['allocation_id']: line for line in receipt_value['lines']}
    used_allocations, source_ids, sequences, promotions = set(), set(), set(), []
    net_total, tax_total = Decimal(0), Decimal(0)
    for source in sources:
        _keys(source, FISCAL_LINE_FIELDS, 'INVALID_PURCHASE_FISCAL_LINE_FIELDS')
        _id(source['xml_line_id']); _id(source['xml_sequence'])
        source_id = source['xml_line_id']
        if source_id in source_ids or source['xml_sequence'] in sequences:
            _fail('DUPLICATE_PURCHASE_FISCAL_LINE')
        source_ids.add(source_id); sequences.add(source['xml_sequence'])
        if not isinstance(source['cabys'], str) or not re.fullmatch(r'[0-9]{13}', source['cabys']):
            _fail('INVALID_PURCHASE_FISCAL_CABYS')
        quantity = _number(source['quantity'])
        if not quantity:
            _fail('INVALID_PURCHASE_FISCAL_QUANTITY')
        gross = _number(source['gross_amount']); net = _number(source['net_amount'])
        unit_price = _number(source['unit_price'])
        _money_matches(quantity * unit_price, gross, 'PURCHASE_FISCAL_GROSS_MISMATCH')
        discounts = Decimal(0)
        for discount in _list(source['discounts']):
            _keys(discount, {'code', 'amount', 'nature', 'source_xml'}, 'INVALID_PURCHASE_FISCAL_DISCOUNT')
            _fiscal_code(discount['code'], optional=True)
            _source_text(discount['nature'], 4096, empty=True)
            discounts += _number(discount['amount'])
            _fragment(discount['source_xml'], 'Descuento')
        if discounts > gross + MONEY_TOLERANCE:
            _fail('PURCHASE_FISCAL_DISCOUNT_EXCEEDS_GROSS')
        _money_matches(gross - discounts, net, 'PURCHASE_FISCAL_NET_MISMATCH')
        line_tax = Decimal(0)
        for tax in _list(source['taxes']):
            _keys(tax, TAX_FIELDS, 'INVALID_PURCHASE_FISCAL_TAX')
            _fiscal_code(tax['code']); _fiscal_code(tax['rate_code'], optional=True)
            for field in ('rate', 'base_amount'):
                _number(tax[field])
            amount = _number(tax['amount']); exempt = _number(tax['exoneration_amount'])
            if tax['exoneration_percentage'] is not None:
                _number(tax['exoneration_percentage'], maximum=Decimal(100))
            if type(tax['included_in_cost']) is not bool or exempt > amount:
                _fail('INVALID_PURCHASE_FISCAL_TAX')
            _money_matches(amount - exempt, _number(tax['net_amount']), 'PURCHASE_FISCAL_TAX_NET_MISMATCH')
            if tax['exoneration_xml'] is not None:
                _fragment(tax['exoneration_xml'], 'Exoneracion')
            elif exempt or tax['exoneration_percentage']:
                _fail('PURCHASE_FISCAL_EXONERATION_SOURCE_REQUIRED')
            line_tax += _number(tax['net_amount'])
        assumed = _number(source['assumed_tax_amount'])
        if assumed > line_tax + MONEY_TOLERANCE:
            _fail('PURCHASE_FISCAL_ASSUMED_TAX_EXCEEDS_TAX')
        _money_matches(line_tax - assumed, _number(source['net_tax_amount']), 'PURCHASE_FISCAL_LINE_TAX_MISMATCH')
        promotion = source['promotion']
        _keys(promotion, {'type', 'parent_xml_line_id'}, 'INVALID_PURCHASE_FISCAL_PROMOTION')
        if promotion['type'] is not None:
            _text(promotion['type'], 64, 'INVALID_PURCHASE_FISCAL_PROMOTION')
        if promotion['parent_xml_line_id'] is not None:
            _id(promotion['parent_xml_line_id'])
            if promotion['type'] is None or promotion['parent_xml_line_id'] == source_id:
                _fail('INVALID_PURCHASE_FISCAL_PROMOTION')
            promotions.append((source_id, promotion['parent_xml_line_id']))
        ids = _list(source['allocation_ids'])
        if source['inventory_effect'] == 'none':
            if ids or any(source[field] is not None for field in ('quantity_base', 'conversion_factor', 'conversion_mode')):
                _fail('PURCHASE_NON_STOCK_FISCAL_ALLOCATION_FORBIDDEN')
        elif source['inventory_effect'] == 'receive':
            base_quantity = _integer(source['quantity_base'])
            factor = _number(source['conversion_factor'])
            if not factor or source['conversion_mode'] not in ('multiply', 'divide'):
                _fail('INVALID_PURCHASE_FISCAL_CONVERSION')
            converted = quantity * factor if source['conversion_mode'] == 'multiply' else quantity / factor
            if abs(converted - base_quantity) > Decimal('0.00000001'):
                _fail('PURCHASE_FISCAL_CONVERSION_MISMATCH')
            if not ids:
                _fail('PURCHASE_FISCAL_ALLOCATION_REQUIRED')
            allocated_quantity, allocated_net, native_quantity = 0, Decimal(0), 0
            for allocation_id in ids:
                _text(allocation_id, 128, 'INVALID_PURCHASE_ALLOCATION')
                if allocation_id in used_allocations or allocation_id not in allocations:
                    _fail('PURCHASE_FISCAL_ALLOCATION_IDENTITY_MISMATCH')
                allocation = allocations[allocation_id]
                if allocation['xml_line_id'] != source_id:
                    _fail('PURCHASE_FISCAL_ALLOCATION_IDENTITY_MISMATCH')
                used_allocations.add(allocation_id)
                allocated_quantity += _integer(allocation['quantity_base'])
                allocated_net += _number(allocation['unit_cost']) * _integer(allocation['quantity'])
                native_quantity += _integer(allocation['quantity'])
            if allocated_quantity != base_quantity:
                _fail('PURCHASE_FISCAL_ALLOCATION_QUANTITY_MISMATCH')
            if abs(allocated_net - net) > NATIVE_HALF_QUANTUM * (native_quantity + 1):
                _fail('PURCHASE_RECEIPT_UNTAXED_ALLOCATION_MISMATCH')
        else:
            _fail('INVALID_PURCHASE_FISCAL_INVENTORY_EFFECT')
        net_total += net; tax_total += _number(source['net_tax_amount'])
    if used_allocations != set(allocations):
        _fail('PURCHASE_FISCAL_UNCOVERED_STOCK_ALLOCATION')
    parents = dict(promotions)
    for source_id, parent in promotions:
        if parent not in source_ids or parent in parents:
            _fail('PURCHASE_FISCAL_PROMOTION_PARENT_INVALID')
    totals = receipt_value['totals']
    _money_matches(net_total, _number(totals['untaxed']), 'PURCHASE_FISCAL_UNTAXED_TOTAL_MISMATCH')
    _money_matches(tax_total, _number(totals['tax']), 'PURCHASE_FISCAL_TAX_TOTAL_MISMATCH')
    if refund > tax_total + MONEY_TOLERANCE:
        _fail('PURCHASE_FISCAL_REFUND_EXCEEDS_TAX')
    _money_matches(net_total + tax_total + charge_total - refund, _number(totals['total']),
                   'PURCHASE_FISCAL_DOCUMENT_TOTAL_MISMATCH')
    return copy.deepcopy(value)


def execution_blockers(payload):
    """Pure capability policy, separate from validity of transported source data."""
    operation = request(payload)
    if operation['contract'] == 1:
        return ['BLOCKED_PURCHASE_FISCAL_V2_REQUIRED']
    if operation['action'] == 'price.publish':
        return []
    value = operation['receipt']['fiscal']; blockers = []
    if value['other_charges'] or value['vat_refund']:
        blockers.append('BLOCKED_PURCHASE_CHARGES_REFUND_NOT_CERTIFIED')
    if value['payment_terms']['condition_code'] != '01':
        blockers.append('BLOCKED_PURCHASE_CREDIT_NOT_CERTIFIED')
    for source in value['lines']:
        if source['inventory_effect'] == 'none':
            blockers.append('BLOCKED_PURCHASE_NON_STOCK_FISCAL_NOT_CERTIFIED')
        if source['taxes'] or source['assumed_tax_amount'] or source['net_tax_amount']:
            blockers.append('BLOCKED_PURCHASE_TAX_NOT_CERTIFIED')
        if source['discounts']:
            blockers.append('BLOCKED_PURCHASE_DISCOUNT_NOT_CERTIFIED')
        if source['promotion']['type'] is not None:
            blockers.append('BLOCKED_PURCHASE_PROMOTION_NOT_CERTIFIED')
    native_keys = {}
    for allocation in operation['receipt']['lines']:
        key = tuple(allocation['order_line_key'])
        costs = (allocation['unit_factor'], allocation['unit_cost'], allocation['unit_cost_base'])
        if key in native_keys and native_keys[key] != costs:
            blockers.append('BLOCKED_PURCHASE_NATIVE_SPLIT_NOT_CERTIFIED')
        native_keys[key] = costs
    return sorted(set(blockers))


def price(value, *, version=1):
    _keys(value, PRICE_FIELDS | ({'exchange_rate'} if version == 2 else set()), 'INVALID_PURCHASE_PRICE_FIELDS')
    _id(value['article_id']); _id(value['level'], MAX_SMALLINT)
    _id(value['document_id']); _currency(value['currency'])
    _number(value['unit_price_excluded_tax'])
    rate = _number(value['vat_rate'], maximum=Decimal('100'))
    if (not isinstance(value['vat_code'], str) or value['vat_code'] not in VAT_RATES
            or rate != Decimal(str(VAT_RATES[value['vat_code']]))):
        _fail('INVALID_PURCHASE_PRICE_VAT')
    if version == 2:
        _crc(value['currency'], value['exchange_rate'])
    return copy.deepcopy(value)


def request(value):
    if (not isinstance(value, dict) or type(value.get('contract')) is not int
            or value['contract'] not in (1, VERSION) or not isinstance(value.get('action'), str)
            or value['action'] not in ACTIONS):
        _fail('INVALID_PURCHASE_EFFECT_REQUEST')
    body_name = 'receipt' if value['action'] == 'purchase.receive' else 'price'
    _keys(value, COMMON | {'request_checksum', body_name}, 'INVALID_PURCHASE_EFFECT_REQUEST')
    result = copy.deepcopy(value)
    for name in ('operation_id', 'application_id'):
        result[name] = _identity(value[name])
    _text(value['source_instance'], 80, 'INVALID_PURCHASE_EFFECT_SOURCE')
    _checksum(value['request_checksum'])
    result[body_name] = (receipt(value[body_name], version=value['contract']) if body_name == 'receipt'
                         else price(value[body_name], version=value['contract']))
    if digest(result[body_name]) != value['request_checksum']:
        _fail('PURCHASE_EFFECT_REQUEST_CHECKSUM_MISMATCH')
    return result


def _payable(value, expected, version=1):
    _keys(value, PAYABLE_FIELDS | ({'payment_terms', 'exchange_rate'} if version == 2 else set()),
          'INVALID_PURCHASE_PAYABLE_CONFIRMATION')
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
    if version == 2:
        _crc(value['currency'], value['exchange_rate'])
        _terms(value['payment_terms'], _iso_date(expected['invoice_date']))
        if value['payment_terms'] != expected['fiscal']['payment_terms']:
            _fail('PURCHASE_PAYABLE_PAYMENT_TERMS_MISMATCH')


def _receive_result(operation, result, invoice_binding):
    required = COMMON | {
        'status', 'request_checksum', 'receipt_ids', 'order_id', 'stock_confirmed',
        'valuation_confirmed', 'ap_confirmed', 'order_verified', 'payable', 'lines',
    }
    if operation['contract'] == 2:
        required |= {'fiscal_checksum', 'fiscal_confirmed', 'physical_receipt_date'}
    _keys(result, required, 'INVALID_PURCHASE_RECEIPT_CONFIRMATION')
    for flag in ('stock_confirmed', 'valuation_confirmed', 'ap_confirmed', 'order_verified'):
        if result[flag] is not True:
            _fail('PURCHASE_RECEIPT_NATIVE_EFFECT_UNCONFIRMED')
    expected = operation['receipt']
    if operation['contract'] == 2:
        if (result['fiscal_confirmed'] is not True or result['fiscal_checksum'] != digest(expected['fiscal'])
                or result['physical_receipt_date'] != expected['fiscal']['physical_receipt_date']):
            _fail('PURCHASE_RECEIPT_FISCAL_EFFECT_UNCONFIRMED')
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
    _payable(result['payable'], expected, operation['contract'])
    if invoice_binding is not None:
        _payable(invoice_binding, expected, operation['contract'])
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
    fields = PRICE_FIELDS | ({'exchange_rate'} if operation['contract'] == 2 else set())
    _keys(effective, fields - {'document_id'}, 'INVALID_NATIVE_EFFECTIVE_PRICE')
    checked = price({**effective, 'document_id': expected['document_id']}, version=operation['contract'])
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
        blockers = execution_blockers(operation)
        if blockers:
            return {**{key: operation[key] for key in COMMON},
                    'request_checksum': operation['request_checksum'], 'status': 'blocked',
                    'code': blockers[0]}
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
