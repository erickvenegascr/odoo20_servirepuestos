import json
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, ConcurrencyError, ValidationError
from odoo.sql_db import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from werkzeug.exceptions import RequestEntityTooLarge
from odoo.addons.sga_gateway_connector.controllers.api import GatewayApi

_logger = logging.getLogger(__name__)
MAX_BODY = 512 * 1024


class SupplierApi(GatewayApi):
    def _suppliers(self, action):
        app = self._application()
        if not app:
            return self._json({'error': 'unauthorized'}, 401)
        if not app.catalog_enabled:
            return self._json({'error': 'scope_disabled'}, 403)
        try:
            if request.httprequest.mimetype != 'application/json':
                raise ValueError('content_type')
            request.httprequest.max_content_length = MAX_BODY
            raw = request.httprequest.get_data()
            if len(raw) > MAX_BODY:
                raise RequestEntityTooLarge()
            def bad_number(value):
                raise ValueError('nonfinite_number')
            body = json.loads(raw, parse_constant=bad_number)
            if not isinstance(body, dict):
                raise ValueError('object_required')
            with request.env.cr.savepoint():
                model = request.env['sga.supplier.import'].sudo().with_context(
                    allowed_company_ids=[app.company_id.id]).with_company(app.company_id)
                if action == 'info':
                    if body:
                        raise ValueError('unknown_fields')
                    value = {'suppliers_contract': 1, 'company_id': app.company_id.id,
                             'database': request.env.cr.dbname, 'application_id': app.application_uuid,
                             'source_instance': app.source_instance, 'sga_writes_enabled': False,
                             'authentication': 'application_token', 'missing_supported': True}
                elif action == 'missing':
                    if set(body) != {'instance', 'relations', 'dry_run'} or body['instance'] != app.source_instance:
                        return self._json({'error': 'source_scope_mismatch'}, 403)
                    value = model._flag_missing(app.source_instance, body['relations'], body['dry_run'])
                else:
                    if set(body) != {'instance', 'vendors', 'relations', 'dry_run'} or body['instance'] != app.source_instance:
                        return self._json({'error': 'source_scope_mismatch'}, 403)
                    value = model._apply(app.source_instance, body['vendors'], body['relations'], body['dry_run'])
                app._touch()
                return self._json(value)
        except (*PG_CONCURRENCY_EXCEPTIONS_TO_RETRY, ConcurrencyError):
            raise
        except RequestEntityTooLarge:
            return self._json({'error': 'body_too_large'}, 413)
        except (ValueError, UnicodeError):
            return self._json({'error': 'invalid_json'}, 400)
        except (AccessError, ValidationError):
            return self._json({'error': 'supplier_conflict_or_invalid_values'}, 409)
        except Exception:
            _logger.error('SGA supplier relations request rolled back (%s)', action)
            return self._json({'error': 'request_failed'}, 500)

    @http.route('/sga_gateway/api/v1/catalog/suppliers/info', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def suppliers_info(self):
        return self._suppliers('info')

    @http.route('/sga_gateway/api/v1/catalog/suppliers/batch', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def suppliers_batch(self):
        return self._suppliers('batch')

    @http.route('/sga_gateway/api/v1/catalog/suppliers/missing', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def suppliers_missing(self):
        return self._suppliers('missing')
