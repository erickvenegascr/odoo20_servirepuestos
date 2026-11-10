import json
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, ConcurrencyError, ValidationError
from odoo.sql_db import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from werkzeug.exceptions import RequestEntityTooLarge
from odoo.addons.sga_gateway_connector.controllers.api import GatewayApi

_logger = logging.getLogger(__name__)
MAX_BODY = 10 * 1024 * 1024


class GatewayMediaApi(GatewayApi):
    def _media(self, action):
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
            body = json.loads(raw)
            keys = {'instance', 'items'} if action == 'check' else {'instance', 'item', 'content'}
            if not isinstance(body, dict) or set(body) != keys or body.get('instance') != app.source_instance:
                return self._json({'error': 'source_scope_mismatch'}, 403)
            with request.env.cr.savepoint():
                model = request.env['sga.media.import'].sudo().with_context(
                    allowed_company_ids=[app.company_id.id]).with_company(app.company_id)
                value = (model._check_images(app.source_instance, body['items']) if action == 'check'
                         else model._apply_image(app.source_instance, body['item'], body['content']))
                app._touch()
                return self._json(value)
        except (*PG_CONCURRENCY_EXCEPTIONS_TO_RETRY, ConcurrencyError):
            raise
        except RequestEntityTooLarge:
            return self._json({'error': 'body_too_large'}, 413)
        except (ValueError, UnicodeError):
            return self._json({'error': 'invalid_json'}, 400)
        except (AccessError, ValidationError):
            return self._json({'error': 'media_conflict_or_invalid_image'}, 409)
        except Exception:
            _logger.error('SGA media request rolled back (%s)', action)
            return self._json({'error': 'request_failed'}, 500)

    @http.route('/sga_gateway/api/v1/media/check', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def media_check(self):
        return self._media('check')

    @http.route('/sga_gateway/api/v1/media/image', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False, max_content_length=MAX_BODY)
    def media_image(self):
        return self._media('image')
