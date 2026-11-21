"""Contract for creating a standalone Odoo purchase order in SGA."""

import copy
import re
from decimal import Decimal

from .purchase_effects_contract import (
    COMMON, MAX_LINES, MAX_SMALLINT, NativeResultUnverified, PurchaseEffectsError,
    _id, _iso_date, _keys, _number, _text, _vendor, digest,
)


ORDER_FIELDS = {'order_uuid', 'odoo_order_id', 'odoo_order_ref', 'vendor_code', 'currency',
                'date_order', 'delivery_days', 'lines'}
FISCALBOT_FIELDS = {'profile', 'fiscalbot_document_id', 'invoice_key', 'invoice_number'}
FISCALBOT_BULK_PROFILE = 'fiscalbot_crc_iva13_credit_factor_one_bulk'
LINE_FIELDS = {'article_id', 'por_mayor', 'unit_factor', 'quantity', 'unit_cost', 'source_line_id'}
QUANTUM = Decimal('0.0001')


def validate_order_request(payload):
    _keys(payload, COMMON | {'order', 'request_checksum'}, 'INVALID_PURCHASE_ORDER_REQUEST')
    version = payload['contract']
    if type(version) is not int or version not in (1, 2) or payload['action'] != 'purchase.upsert':
        raise PurchaseEffectsError('INVALID_PURCHASE_ORDER_CONTRACT')
    bulk = version == 2
    order = payload['order']
    _keys(order, ORDER_FIELDS | (FISCALBOT_FIELDS if bulk else set()), 'INVALID_PURCHASE_ORDER_FIELDS')
    if (order['order_uuid'] != payload['operation_id'] or order['currency'] != 'CRC'
            or digest(order) != payload['request_checksum']):
        raise PurchaseEffectsError('INVALID_PURCHASE_ORDER_IDENTITY_OR_CURRENCY')
    _id(order['odoo_order_id'])
    _vendor(order['vendor_code'])
    _text(order['odoo_order_ref'], 80, 'INVALID_ODOO_ORDER_REFERENCE')
    _iso_date(order['date_order'])
    _id(order['delivery_days'], maximum=MAX_SMALLINT, minimum=0)
    if bulk:
        _id(order['fiscalbot_document_id'])
        if (order['profile'] != FISCALBOT_BULK_PROFILE or not isinstance(order['invoice_key'], str)
                or not re.fullmatch(r'[0-9]{50}', order['invoice_key'])):
            raise PurchaseEffectsError('INVALID_FISCALBOT_PURCHASE_ORDER_PROFILE')
        _text(order['invoice_number'], 20, 'INVALID_FISCALBOT_PURCHASE_ORDER_INVOICE_NUMBER')
    lines = order['lines']
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise PurchaseEffectsError('INVALID_PURCHASE_ORDER_LINE_COUNT')
    articles = []
    source_lines = set()
    for line in lines:
        _keys(line, LINE_FIELDS, 'INVALID_PURCHASE_ORDER_LINE')
        articles.append(_id(line['article_id']))
        source_lines.add(_id(line['source_line_id']))
        _id(line['quantity'])
        cost = _number(line['unit_cost'])
        if (line['por_mayor'] is not bulk or _id(line['unit_factor']) != 1 or cost != cost.quantize(QUANTUM)
                or bulk and cost <= 0):
            raise PurchaseEffectsError('INVALID_PURCHASE_ORDER_UNIT_OR_COST')
    if articles != sorted(set(articles)) or len(source_lines) != len(lines):
        raise PurchaseEffectsError('DUPLICATE_OR_UNSORTED_PURCHASE_ORDER_LINES')
    return copy.deepcopy(payload)


def validate_order_result(result, request):
    """A response without exact readback must leave the same operation pending."""
    try:
        for key in COMMON | {'request_checksum'}:
            if result.get(key) != request[key]:
                raise PurchaseEffectsError('WRONG_PURCHASE_ORDER_RESULT_SCOPE')
        if result.get('status') in ('blocked', 'rejected'):
            _keys(result, COMMON | {'request_checksum', 'status', 'code'}, 'INVALID_PURCHASE_ORDER_REJECTION')
            _text(result['code'], 128, 'INVALID_PURCHASE_ORDER_REJECTION_CODE')
            return result
        _keys(result, COMMON | {'request_checksum', 'status', 'order_uuid', 'order_confirmed', 'order'},
              'INVALID_PURCHASE_ORDER_RESULT')
        expected = request['order']
        source = result['order']
        if (result['status'] != 'synchronized' or result['order_uuid'] != expected['order_uuid']
                or result['order_confirmed'] is not True or not isinstance(source, dict)):
            raise PurchaseEffectsError('MISSING_NATIVE_PURCHASE_ORDER_PROOF')
        native_id = _id(source['order_id'])
        if (source.get('vendor_code') != expected['vendor_code'] or source.get('currency_code') != 'CRC'
                or source.get('currency_status') != 'resolved' or source.get('eligible') is not True
                or source.get('checksum') != digest({key: value for key, value in source.items() if key != 'checksum'})
                or source.get('date_approval') or source['date_order'][:10] != expected['date_order']
                or source.get('delivery_days') != expected['delivery_days']
                or any(source.get(key) is not False for key in ('printed', 'processed', 'reorder', 'withdraw'))):
            raise PurchaseEffectsError('SGA_PURCHASE_ORDER_HEADER_MISMATCH')
        lines = source['lines']
        if not isinstance(lines, list) or len(lines) != len(expected['lines']):
            raise PurchaseEffectsError('SGA_PURCHASE_ORDER_LINES_MISMATCH')
        for actual, wanted in zip(lines, expected['lines']):
            article = wanted['article_id']
            bulk = wanted['por_mayor']
            factor = _id(actual['unit_factor'], maximum=MAX_SMALLINT)
            if (actual['line_key'] != [native_id, bulk, article] or actual['article_id'] != article
                    or actual['por_mayor'] is not bulk or bulk and factor != 1
                    or _id(actual['ordered_base']) != wanted['quantity'] or actual['received_base'] != 0
                    or actual['saldada'] is not False or actual['consignment'] is not False
                    or _number(actual['quoted_cost']) != _number(wanted['unit_cost'])):
                raise PurchaseEffectsError('SGA_PURCHASE_ORDER_LINE_MISMATCH')
    except (AttributeError, KeyError, TypeError, PurchaseEffectsError) as exc:
        raise NativeResultUnverified(getattr(exc, 'code', 'INVALID_NATIVE_PURCHASE_ORDER_PROOF')) from None
    return result
