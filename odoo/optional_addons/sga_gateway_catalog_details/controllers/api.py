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


class CatalogDetailsApi(GatewayApi):
    def _details(self, action):
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
                model = request.env['sga.catalog.details.import'].sudo().with_context(
                    allowed_company_ids=[app.company_id.id]).with_company(app.company_id)
                if action == 'info':
                    if body:
                        raise ValueError('unknown_fields')
                    value = {'details_contract': 1, 'company_id': app.company_id.id,
                             'database': request.env.cr.dbname, 'application_id': app.application_uuid,
                             'source_instance': app.source_instance, 'sga_writes_enabled': False,
                             'authentication': 'application_token'}
                else:
                    if set(body) != {'instance', 'masters', 'products', 'dry_run'} or body['instance'] != app.source_instance:
                        return self._json({'error': 'source_scope_mismatch'}, 403)
                    value = model._apply_details_batch(app.source_instance, body['masters'], body['products'], body['dry_run'])
                app._touch()
                return self._json(value)
        except (*PG_CONCURRENCY_EXCEPTIONS_TO_RETRY, ConcurrencyError):
            raise
        except RequestEntityTooLarge:
            return self._json({'error': 'body_too_large'}, 413)
        except (ValueError, UnicodeError):
            return self._json({'error': 'invalid_json'}, 400)
        except (AccessError, ValidationError):
            return self._json({'error': 'catalog_details_conflict_or_invalid_values'}, 409)
        except Exception:
            _logger.error('SGA catalog details request rolled back (%s)', action)
            return self._json({'error': 'request_failed'}, 500)

    @http.route('/sga_gateway/api/v1/catalog/details/info', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def details_info(self):
        return self._details('info')

    @http.route('/sga_gateway/api/v1/catalog/details/batch', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def details_batch(self):
        return self._details('batch')
