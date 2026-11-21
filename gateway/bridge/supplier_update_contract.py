"""A confirmed edit to one already existing supplier relation."""

import re
import uuid

from .article_contract import digest
from .catalog_source import text

ACTION = 'supplier.update'
COMMON = frozenset({'contract', 'action', 'application_id', 'operation_id', 'source_instance'})
RELATION_FIELDS = frozenset({'article_id', 'vendor_code', 'product_code', 'priority', 'disabled', 'vendor_active'})
CHANGE_FIELDS = frozenset({'product_code', 'priority'})


class SupplierUpdateError(ValueError):
    def __init__(self, code, status='rejected'):
        self.code = code
        self.status = status
        super().__init__(code)


class SupplierResultUnverified(RuntimeError):
    """Leave the original operation queued if committed native evidence is uncertain."""


def canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _product_code(value, *, requested=False):
    if not isinstance(value, str) or len(value) > 20 or value != value.strip():
        return False
    if not value:
        return not requested
    if not value.isprintable():
        return False
    if requested:
        # The existing supplier snapshot uses a Latin-1 client and text()
        # normalization. A new code must be representable through that reader.
        try:
            value.encode('latin1')
        except UnicodeEncodeError:
            return False
        return text(value) == value
    return True


def _priority(value):
    return type(value) is int and 0 <= value <= 32767


def valid_relation(value):
    return (isinstance(value, dict) and set(value) == RELATION_FIELDS
            and type(value['article_id']) is int and 1 <= value['article_id'] <= 2147483647
            and isinstance(value['vendor_code'], str) and re.fullmatch(r'[0-9A-Za-z]{1,3}', value['vendor_code'])
            and _product_code(value['product_code']) and _priority(value['priority'])
            and type(value['disabled']) is bool and type(value['vendor_active']) is bool)


def validate_update_request(payload):
    if (not isinstance(payload, dict) or set(payload) != COMMON | {'relation', 'changes', 'request_checksum'}
            or type(payload['contract']) is not int or payload['contract'] != 1 or payload['action'] != ACTION
            or not canonical_uuid(payload['application_id']) or not canonical_uuid(payload['operation_id'])
            or not isinstance(payload['source_instance'], str) or not 1 <= len(payload['source_instance']) <= 80
            or any(not char.isprintable() for char in payload['source_instance'])):
        raise SupplierUpdateError('INVALID_SUPPLIER_UPDATE_REQUEST')
    relation, changes = payload['relation'], payload['changes']
    if not valid_relation(relation):
        raise SupplierUpdateError('INVALID_SUPPLIER_UPDATE_RELATION')
    if (not isinstance(changes, dict) or not changes or not set(changes) <= CHANGE_FIELDS
            or 'product_code' in changes and not _product_code(changes['product_code'], requested=True)
            or 'priority' in changes and not _priority(changes['priority'])
            or all(relation[field] == value for field, value in changes.items())):
        raise SupplierUpdateError('INVALID_SUPPLIER_UPDATE_CHANGES')
    if digest({'relation': relation, 'changes': changes}) != payload['request_checksum']:
        raise SupplierUpdateError('INVALID_SUPPLIER_UPDATE_CHECKSUM')
    return {**payload, 'relation': dict(relation), 'changes': dict(changes)}


def update_result(request, error=None, *, relation=None):
    result = {key: request.get(key) for key in COMMON | {'request_checksum'}}
    if error is not None:
        return {**result, 'status': error.status, 'code': error.code}
    return {**result, 'status': 'synchronized', 'relation': relation}


def verify_native_result(result, operation):
    expected_relation = {**operation['relation'], **operation['changes']}
    if (not isinstance(result, dict) or type(result.get('contract')) is not int
            or not valid_relation(result.get('relation'))
            or result != update_result(operation, relation=expected_relation)):
        raise SupplierResultUnverified('SGA_SUPPLIER_UPDATE_NATIVE_RESULT_MISMATCH')
    return result
