"""Company-scoped transport for standard Odoo JSON-2 operations."""

import os
import re
import stat
from urllib.parse import urlsplit

import requests


_MODEL = re.compile(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*')
_METHOD = re.compile(r'[a-z][a-z0-9_]*')
_DATABASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
_KEY = re.compile(r'[A-Za-z0-9_.-]{16,512}')


class OdooJson2Error(RuntimeError):
    """Diagnostic containing no server response, request details or secret."""

    def __init__(self, code, *, status=None, outcome_unknown=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.outcome_unknown = outcome_unknown


def _origin(value):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise OdooJson2Error('ODOO_JSON2_HTTPS_ORIGIN_REQUIRED')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
                 and not any(character in value for character in ('?', '#', '\\')))
        if parsed.port is not None and parsed.port == 0:
            valid = False
    except ValueError:
        raise OdooJson2Error('ODOO_JSON2_HTTPS_ORIGIN_REQUIRED') from None
    if not valid:
        raise OdooJson2Error('ODOO_JSON2_HTTPS_ORIGIN_REQUIRED')
    return value.rstrip('/')


def _api_key(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, encoding='utf-8') as key_file:
            metadata = os.fstat(key_file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o037:
                raise OdooJson2Error('ODOO_JSON2_KEY_FILE_UNPROTECTED')
            value = key_file.read(516).strip()
    except (OSError, TypeError, UnicodeError, ValueError):
        raise OdooJson2Error('ODOO_JSON2_KEY_FILE_UNREADABLE') from None
    if not _KEY.fullmatch(value) or value.startswith('sgw1_'):
        raise OdooJson2Error('ODOO_JSON2_API_KEY_REQUIRED')
    return value


class OdooJson2Client:
    """Send one request per call. Callers verify effects before retrying writes."""

    def __init__(self, config, *, session=None, timeout=(5, 60)):
        self._origin = _origin(config.get('url'))
        self._database = config.get('database')
        self.company_id = config.get('company_id')
        if not isinstance(self._database, str) or not _DATABASE.fullmatch(self._database):
            raise OdooJson2Error('ODOO_JSON2_DATABASE_REQUIRED')
        if type(self.company_id) is not int or self.company_id <= 0:
            raise OdooJson2Error('ODOO_JSON2_COMPANY_REQUIRED')
        self._key = _api_key(config.get('api_key_file'))
        self._session = requests.Session() if session is None else session
        self._session.trust_env = False
        self._timeout = timeout

    def call(self, model, method, *, context=None, **kwargs):
        if self._key is None:
            raise OdooJson2Error('ODOO_JSON2_CLIENT_CLOSED')
        if (not isinstance(model, str) or not _MODEL.fullmatch(model)
                or not isinstance(method, str) or not _METHOD.fullmatch(method)):
            raise OdooJson2Error('ODOO_JSON2_INVALID_PATH')
        if context is not None and not isinstance(context, dict):
            raise OdooJson2Error('ODOO_JSON2_INVALID_CONTEXT')
        scoped = dict(context or {})
        if ('allowed_company_ids' in scoped and scoped['allowed_company_ids'] != [self.company_id]
                or any(key in scoped and scoped[key] != self.company_id
                       for key in ('force_company', 'company_id', 'default_company_id'))):
            raise OdooJson2Error('ODOO_JSON2_CROSS_COMPANY_CONTEXT')
        scoped['allowed_company_ids'] = [self.company_id]
        try:
            response = self._session.post(
                f'{self._origin}/json/2/{model}/{method}',
                headers={'Authorization': f'Bearer {self._key}', 'X-Odoo-Database': self._database},
                json={**kwargs, 'context': scoped}, timeout=self._timeout, allow_redirects=False, verify=True,
            )
        except requests.RequestException:
            raise OdooJson2Error('ODOO_JSON2_TRANSPORT_FAILED', outcome_unknown=True) from None
        except (TypeError, ValueError):
            raise OdooJson2Error('ODOO_JSON2_INVALID_REQUEST') from None
        status = response.status_code
        if status != 200:
            raise OdooJson2Error(f'ODOO_JSON2_HTTP_{status}', status=status,
                                 outcome_unknown=status == 408 or status >= 500)
        try:
            return response.json()
        except ValueError:
            raise OdooJson2Error('ODOO_JSON2_INVALID_RESPONSE', status=200, outcome_unknown=True) from None

    def search_read(self, model, domain, fields, *, limit=None, order=None, offset=None):
        args = {'domain': domain, 'fields': fields, 'load': None}
        if limit is not None:
            args['limit'] = limit
        if order is not None:
            args['order'] = order
        if offset is not None:
            args['offset'] = offset
        return self._read_records(model, 'search_read', **args)

    def read(self, model, ids, fields):
        return self._read_records(model, 'read', ids=ids, fields=fields, load=None)

    def _read_records(self, model, method, *, fields, **args):
        if not isinstance(fields, (list, tuple)) or not fields or any(
            not isinstance(field, str) or not _METHOD.fullmatch(field) for field in fields
        ):
            raise OdooJson2Error('ODOO_JSON2_FIELDS_REQUIRED')
        result = self.call(model, method, fields=list(fields), **args)
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise OdooJson2Error('ODOO_JSON2_INVALID_RECORDS')
        return result

    def close(self):
        self._key = None
        self._session.close()
