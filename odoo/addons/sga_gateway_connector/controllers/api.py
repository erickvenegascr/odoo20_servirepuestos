import json
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, ValidationError
from werkzeug.exceptions import RequestEntityTooLarge

_logger = logging.getLogger(__name__)
MAX_BODY = 262144


class GatewayApi(http.Controller):
    def _json(self, value, status=200):
        response = request.make_json_response(value, status=status)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def _application(self):
        scheme, _, token = request.httprequest.headers.get('Authorization', '').partition(' ')
        if scheme.lower() != 'bearer':
            return request.env['sga.gateway.application'].browse()
        return request.env['sga.gateway.application']._authenticate_token(token.strip())

    def _body(self):
        if request.httprequest.mimetype != 'application/json':
            raise ValueError('invalid_content_type')
        request.httprequest.max_content_length = MAX_BODY
        raw = request.httprequest.get_data()
        if len(raw) > MAX_BODY:
            raise ValueError('body_too_large')
        def invalid_number(value):
            raise ValueError('invalid_number')
        result = json.loads(raw, parse_constant=invalid_number)
        if not isinstance(result, dict):
            raise ValueError('invalid_json')
        return result

    def _handle(self, action):
        app = self._application()
        if not app:
            return self._json({'error': 'unauthorized'}, 401)
        if (action == 'catalog' and not app.catalog_enabled
                or action in ('poll', 'ack') and not app.exchange_enabled):
            return self._json({'error': 'scope_disabled'}, 403)
        try:
            body = self._body()
        except RequestEntityTooLarge:
            return self._json({'error': 'body_too_large'}, 413)
        except (ValueError, UnicodeError):
            return self._json({'error': 'invalid_or_oversized_json'}, 400)
        try:
            # A rejected/failed batch also rolls back all its earlier items.
            with request.env.cr.savepoint():
                model = request.env['sga.catalog.import'].sudo().with_context(
                    allowed_company_ids=[app.company_id.id]).with_company(app.company_id)
                if action == 'info':
                    if body:
                        raise ValidationError('Unknown request fields')
                    value = {'contract': 1, 'company_id': app.company_id.id,
                             'database': request.env.cr.dbname, 'application_id': app.application_uuid,
                             'source_instance': app.source_instance, 'sga_writes_enabled': False,
                             'authentication': 'application_token', 'reverse_transport': 'durable_poll'}
                elif action == 'catalog':
                    if set(body) - {'instance', 'categories', 'products', 'dry_run'} or body.get('instance') != app.source_instance:
                        return self._json({'error': 'source_scope_mismatch'}, 403)
                    value = model._apply_catalog_batch(
                        app.source_instance, body.get('categories', []), body.get('products', []),
                        dry_run=body.get('dry_run', True))
                elif action == 'poll':
                    if body:
                        raise ValidationError('Unknown request fields')
                    value = {'application_id': app.application_uuid,
                             'messages': request.env['sga.gateway.message']._claim(app)}
                else:
                    if set(body) != {'message_id', 'lease', 'result'} or not isinstance(body['result'], dict):
                        raise ValidationError('Invalid acknowledgement')
                    if not isinstance(body['message_id'], str) or not isinstance(body['lease'], str):
                        raise ValidationError('Invalid message/lease')
                    outcome = request.env['sga.gateway.message']._ack(app, body['message_id'], body['lease'], body['result'])
                    if outcome != 'ok':
                        return self._json({'error': outcome}, 404 if outcome == 'not_found' else 409)
                    value = {'status': 'acknowledged', 'message_id': body['message_id']}
                app._touch()
                return self._json(value)
        except (ValidationError, AccessError):
            return self._json({'error': 'catalog_conflict_or_invalid_values'}, 409)
        except Exception:
            # No tokens, payloads or exception strings in logs/responses.
            _logger.error('SGA application request rolled back (%s)', action)
            return self._json({'error': 'request_failed'}, 500)

    @http.route('/sga_gateway/api/v1/info', type='http', auth='public', methods=['POST'], csrf=False, save_session=False, max_content_length=MAX_BODY)
    def info(self):
        return self._handle('info')

    @http.route('/sga_gateway/api/v1/catalog/batch', type='http', auth='public', methods=['POST'], csrf=False, save_session=False, max_content_length=MAX_BODY)
    def catalog(self):
        return self._handle('catalog')

    @http.route('/sga_gateway/api/v1/exchange/poll', type='http', auth='public', methods=['POST'], csrf=False, save_session=False, max_content_length=MAX_BODY)
    def poll(self):
        return self._handle('poll')

    @http.route('/sga_gateway/api/v1/exchange/ack', type='http', auth='public', methods=['POST'], csrf=False, save_session=False, max_content_length=MAX_BODY)
    def ack(self):
        return self._handle('ack')
