"""Optional purchase bridge over the existing authenticated reverse channel.

Search uses the independently restricted SELECT account. Financial actions are
explicitly unavailable until a native executor has been independently certified;
loading this bridge does not grant business-table write privileges.
"""
import json
import os
import uuid
from pathlib import Path

from pymysql.err import MySQLError

from .catalog_source import SourceReadOnlyError, configuration
from .purchase_source import PurchaseSource, PurchaseSourceError, valid_discovery_query
from .purchase_receipt_source import PurchaseReceiptSource, valid_scope as valid_receipt_scope
from .purchase_replenishment import PurchaseReplenishmentSource, ReplenishmentSourceError
from .purchase_effects_contract import (
    COMMON, NativeEffectsBoundary, NativeResultUnverified, PurchaseEffectsError,
)
from .purchase_order_contract import validate_order_request, validate_order_result
from .purchase_order_sql import PurchaseOrderSql


ACTIONS = frozenset({'purchase.search', 'purchase.discover', 'purchase.receipt.lookup', 'purchase.upsert',
                     'purchase.receive', 'purchase.reverse', 'purchase.replenish', 'price.publish'})
MAX_RESULT_BYTES = 240000  # Odoo exchange/ack caps the whole request at 262144.


def _bounded_result(result, common):
    value = {**result, **common}
    key = 'candidates' if common['action'] == 'purchase.replenish' else 'orders'
    while len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > MAX_RESULT_BYTES:
        rows = value.get(key)
        if not isinstance(rows, list) or len(rows) <= 1:
            return {**common, 'status': 'blocked', 'code': 'SGA_PURCHASE_RESULT_TOO_LARGE'}
        # Keep a complete prefix from the same source snapshot. A partial page
        # must never acquire the meaning of an exhaustive absence check.
        value[key] = rows[:-1]
        last = value[key][-1]
        value['cursor_after'] = (last['order_id'] if key == 'orders' else
                                {'ranking': last['ranking_native'], 'article_id': last['article_id']})
        value['has_more'] = True
        value['search_complete'] = False
    return value


class PurchaseBridge:
    def __init__(self, application_id, source_config, *, source_factory=PurchaseSource,
                 replenishment_factory=PurchaseReplenishmentSource, receipt_factory=PurchaseReceiptSource,
                 effects=None, order_writer=None, receipt_writer=None):
        self.application_id = str(uuid.UUID(application_id))
        self.source_instance = source_config.get('instance') or source_config.get('source_instance')
        if (not isinstance(self.source_instance, str) or not self.source_instance
                or len(self.source_instance) > 80):
            raise ValueError('Purchase adapter requires a pinned source identity')
        self.source_config = dict(source_config)
        self.source_factory = source_factory
        self.replenishment_factory = replenishment_factory
        self.receipt_factory = receipt_factory
        self.order_writer = order_writer
        if order_writer is not None and (order_writer.application_id != self.application_id
                                         or order_writer.source_instance != self.source_instance):
            raise ValueError('Purchase order executor scope mismatch')
        self.receipt_writer = receipt_writer
        if receipt_writer is not None and (receipt_writer.application_id != self.application_id
                                           or receipt_writer.source_instance != self.source_instance):
            raise ValueError('Purchase receipt executor scope mismatch')
        self.effects = effects or NativeEffectsBoundary(self.application_id, self.source_instance)
        if (self.effects.application_id != self.application_id
                or self.effects.source_instance != self.source_instance):
            raise ValueError('Purchase effect executor scope mismatch')

    def _common(self, payload):
        if (not isinstance(payload, dict) or type(payload.get('contract')) is not int
                or payload['contract'] not in (1, 2)
                or not isinstance(payload.get('action'), str) or payload['action'] not in ACTIONS
                or payload['contract'] == 2 and payload['action'] not in ('purchase.receive', 'price.publish')
                or payload.get('application_id') != self.application_id
                or payload.get('source_instance') != self.source_instance):
            raise ValueError('Purchase operation/source scope mismatch')
        operation = payload.get('operation_id')
        if not isinstance(operation, str) or str(uuid.UUID(operation)) != operation:
            raise ValueError('Invalid purchase operation identity')
        return {key: payload[key] for key in COMMON}

    def apply(self, payload):
        common = self._common(payload)
        action = payload['action']
        if action == 'purchase.discover':
            return self._discover(payload, common)
        if action == 'purchase.receipt.lookup':
            return self._lookup_receipt(payload, common)
        if action == 'purchase.replenish':
            if (set(payload) - (COMMON | {'scope', 'limit', 'after'})
                    or not COMMON | {'scope', 'limit'} <= set(payload)
                    or not isinstance(payload['scope'], dict)
                    or set(payload['scope']) != {'vendor_code', 'strategy', 'abc', 'retained'}):
                return {**common, 'status': 'rejected', 'code': 'INVALID_REPLENISHMENT_SEARCH_REQUEST'}
            try:
                with self.replenishment_factory(self.source_config) as source:
                    result = source.suggest(**payload['scope'], limit=payload['limit'],
                                            after=payload.get('after', 0))
            except ReplenishmentSourceError as exc:
                return {**common, 'status': 'rejected', 'code': exc.code}
            if result.get('source_instance') != self.source_instance:
                raise ValueError('Purchase source identity changed')
            if result.get('status') == 'blocked':
                result['code'] = 'BLOCKED_NATIVE_REPLENISHMENT_NOT_CERTIFIED'
            return _bounded_result(result, common)
        if action == 'purchase.search':
            if (set(payload) - (COMMON | {'scope', 'limit', 'after'})
                    or not COMMON | {'scope', 'limit'} <= set(payload)
                    or not isinstance(payload['scope'], dict)
                    or set(payload['scope']) != {'vendor_code', 'date_from', 'date_to',
                                                  'order_id', 'include_closed'}):
                return {**common, 'status': 'rejected', 'code': 'INVALID_PURCHASE_SEARCH_REQUEST'}
            try:
                # Source is opened only after transport and scope validation.
                # A failed connection/read is never interpreted as absence.
                with self.source_factory(self.source_config) as source:
                    result = source.search(**payload['scope'], limit=payload['limit'],
                                           after=payload.get('after', 0))
            except PurchaseSourceError as exc:
                return {**common, 'status': 'rejected', 'code': exc.code}
            if result.get('source_instance') != self.source_instance:
                raise ValueError('Purchase source identity changed')
            return _bounded_result(result, common)
        if action in ('purchase.receive', 'price.publish'):
            try:
                if action == 'purchase.receive' and self.receipt_writer is not None:
                    return self.receipt_writer.apply(payload)
                return self.effects.apply(payload)
            except NativeResultUnverified:
                # Could follow a native commit. Retain the original operation
                # for native reconciliation; never turn this into a rejection.
                raise
            except PurchaseEffectsError as exc:
                return {**common, 'request_checksum': payload.get('request_checksum'),
                        'status': 'rejected', 'code': exc.code}
        if action == 'purchase.upsert' and isinstance(payload.get('order'), dict) and 'order_uuid' in payload['order']:
            try:
                request = validate_order_request(payload)
            except PurchaseEffectsError as exc:
                return {**common, 'request_checksum': payload.get('request_checksum'),
                        'status': 'rejected', 'code': exc.code}
            if self.order_writer is not None:
                result = validate_order_result(self.order_writer.apply(request), payload)
                if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > MAX_RESULT_BYTES:
                    raise NativeResultUnverified('SGA_PURCHASE_ORDER_RESULT_TOO_LARGE')
                return result
        # Counter allocation, PO state changes and reversals have no certified
        # native executor yet. A stub must never report a successful SGA effect.
        return {**common, 'status': 'blocked', 'code': 'BLOCKED_NATIVE_PURCHASE_NOT_CERTIFIED'}

    def _discover(self, payload, common):
        scope, limit, after = payload.get('scope'), payload.get('limit'), payload.get('after', 0)
        if (set(payload) - (COMMON | {'scope', 'limit', 'after'})
                or not valid_discovery_query(scope, limit, after)):
            return {**common, 'status': 'rejected', 'code': 'INVALID_PURCHASE_DISCOVERY_REQUEST'}
        try:
            with self.source_factory(self.source_config) as source:
                result = source.discover(**scope, limit=limit, after=after)
        except PurchaseSourceError as exc:
            return {**common, 'scope': scope, 'status': 'rejected', 'code': exc.code}
        except SourceReadOnlyError as exc:
            return {**common, 'scope': scope, 'status': 'unavailable', 'code': exc.code}
        except (MySQLError, OSError):
            return {**common, 'scope': scope, 'status': 'unavailable', 'code': 'SGA_PURCHASE_SOURCE_UNAVAILABLE'}
        if result.get('source_instance') != self.source_instance or result.get('scope') != scope:
            raise ValueError('Purchase discovery source or query scope changed')
        return _bounded_result(result, common)

    def _lookup_receipt(self, payload, common):
        scope = payload.get('scope')
        if set(payload) != COMMON | {'scope'} or not valid_receipt_scope(scope):
            return {**common, 'status': 'rejected', 'code': 'INVALID_PURCHASE_RECEIPT_LOOKUP_REQUEST'}
        try:
            with self.receipt_factory(self.source_config) as source:
                result = source.lookup(**scope)
        except (PurchaseSourceError, SourceReadOnlyError) as exc:
            return {**common, 'scope': scope, 'status': 'unavailable', 'code': exc.code}
        except (MySQLError, OSError):
            return {**common, 'scope': scope, 'status': 'unavailable', 'code': 'SGA_RECEIPT_SOURCE_UNAVAILABLE'}
        if result.get('source_instance') != self.source_instance or result.get('scope') != scope:
            raise ValueError('Purchase receipt source or query scope changed')
        response = {**result, **common}
        if len(json.dumps(response, ensure_ascii=True, allow_nan=False).encode()) > MAX_RESULT_BYTES:
            return {**common, 'scope': scope, 'status': 'conflict', 'code': 'SGA_RECEIPT_LOOKUP_TOO_BROAD'}
        return response


def configured_purchase_bridge(client):
    if not os.environ.get('SGA_PURCHASE_BRIDGE_FILE'):
        return None
    config = configuration('SGA_PURCHASE_BRIDGE')
    if config.get('enabled') is not True:
        return None
    application = client.config.get('application_id')
    if not application or config.get('application_id') != application:
        raise ValueError('Purchase bridge requires the pinned exchange application')
    source_path = config.get('source_file') or os.environ.get('SGA_CATALOG_SOURCE_FILE')
    if not isinstance(source_path, str) or not Path(source_path).is_absolute():
        raise ValueError('Purchase bridge requires an absolute SELECT source configuration file')
    source = json.loads(Path(source_path).read_text())
    if (source.get('instance') or source.get('source_instance')) != config.get('source_instance'):
        raise ValueError('Purchase bridge source identity mismatch')
    for key in ('currency_map', 'national_currency_code', 'replenishment_worked',
                'replenishment_retained', 'replenishment_query_verified'):
        if key in config:
            source[key] = config[key]
    order_writer = None
    writer_path = config.get('order_writer_file')
    if writer_path:
        if not isinstance(writer_path, str) or not Path(writer_path).is_absolute():
            raise ValueError('Purchase order writer requires an absolute configuration path')
        writer_config = json.loads(Path(writer_path).read_text())
        if writer_config.get('enabled') is True:
            order_writer = PurchaseOrderSql(writer_config, source, application)
    receipt_writer = None
    receipt_path = config.get('receipt_writer_file')
    if receipt_path:
        if not isinstance(receipt_path, str) or not Path(receipt_path).is_absolute():
            raise ValueError('Purchase receipt writer requires an absolute configuration path')
        receipt_config = json.loads(Path(receipt_path).read_text())
        if receipt_config.get('enabled') is True:
            from .purchase_receipt_sql import PurchaseReceiptSql

            receipt_writer = PurchaseReceiptSql(receipt_config, source, application)
    return PurchaseBridge(application, source, order_writer=order_writer, receipt_writer=receipt_writer)
