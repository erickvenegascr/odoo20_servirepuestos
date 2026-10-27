import hashlib
import json
import os
from pathlib import Path
import uuid
from datetime import timezone
from urllib.parse import urlparse
import requests
from odoo import api, fields, models
from odoo.exceptions import UserError
from ..constants import ACTIONS, STATES

class GatewayOperation(models.Model):
    _name = 'sga.gateway.operation'
    _description = 'SGA gateway operation'
    _order = 'create_date desc, id desc'
    _unique_operation = models.Constraint('UNIQUE(operation_uuid, action, revision)', 'Operation revision must be unique')
    name = fields.Char(default=lambda _: str(uuid.uuid4()), required=True, readonly=True, copy=False)
    operation_uuid = fields.Char(default=lambda _: str(uuid.uuid4()), required=True, readonly=True, copy=False, index=True)
    event_uuid = fields.Char(default=lambda _: str(uuid.uuid4()), required=True, readonly=True, copy=False)
    aggregate_uuid = fields.Char(default=lambda _: str(uuid.uuid4()), required=True)
    revision = fields.Integer(default=1, required=True)
    action = fields.Selection([(s, s) for s in ACTIONS], required=True)
    payload = fields.Json(default=dict, required=True)
    expected_target_fingerprint = fields.Char(default='UNVERIFIED', required=True)
    target_company_key = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    state = fields.Selection([(s, s) for s in STATES], default='BORRADOR', readonly=True, copy=False, index=True)
    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    gateway_id = fields.Char(readonly=True, copy=False)
    last_error = fields.Char(readonly=True, copy=False)
    native_keys = fields.Json(readonly=True, copy=False)
    last_checked = fields.Datetime(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, values_list):
        prepared = []
        for original in values_list:
            vals = dict(original)
            if vals.get('state', 'BORRADOR') != 'BORRADOR':
                raise UserError(self.env._('New operations must start as drafts.'))
            vals.update({'state': 'BORRADOR', 'submitted_by': False, 'gateway_id': False,
                         'native_keys': False, 'last_error': False, 'last_checked': False,
                         'operation_uuid': str(uuid.uuid4()), 'event_uuid': str(uuid.uuid4()),
                         'name': str(uuid.uuid4())})
            prepared.append(vals)
        return super().create(prepared)

    def _gateway_update(self, vals):
        return super().write(vals)

    def write(self, vals):
        protected = {'state', 'submitted_by', 'gateway_id', 'native_keys', 'last_error', 'last_checked'}
        if protected.intersection(vals):
            raise UserError(self.env._('Gateway tracking fields can only be changed by controlled actions.'))
        immutable = {'action', 'payload', 'revision', 'aggregate_uuid', 'operation_uuid', 'event_uuid',
                     'company_id', 'target_company_key', 'expected_target_fingerprint'}
        if immutable.intersection(vals) and any(r.state != 'BORRADOR' for r in self):
            raise UserError(self.env._('Submitted operations are immutable; create a new revision.'))
        return super().write(vals)

    def action_queue(self):
        for rec in self:
            if rec.state != 'BORRADOR' or rec.revision < 1 or not isinstance(rec.payload, dict):
                raise UserError(self.env._('A valid draft operation with a JSON object is required.'))
            try:
                json.dumps(rec.payload, allow_nan=False)
                uuid.UUID(rec.aggregate_uuid)
            except (ValueError, TypeError):
                raise UserError(self.env._('Invalid payload or aggregate UUID.'))
            rec._gateway_update({'state': 'EN_COLA', 'submitted_by': self.env.user.id})
        # The user transaction commits before cron performs any network request.
        return True

    @api.model
    def _configuration(self):
        result = {}
        for name in ('SGA_GATEWAY_URL', 'SGA_GATEWAY_API_KEY', 'SGA_GATEWAY_ORIGIN'):
            path = os.environ.get(name + '_FILE')
            if not path:
                raise UserError(self.env._('Gateway secret file configuration is missing.'))
            try:
                result[name] = Path(path).read_text().strip()
            except OSError:
                raise UserError(self.env._('Gateway secret file is unavailable.'))
            if not result[name]:
                raise UserError(self.env._('Gateway configuration is empty.'))
        url = urlparse(result['SGA_GATEWAY_URL'])
        if url.username or url.password or url.query or url.fragment or not url.netloc:
            raise UserError(self.env._('Invalid gateway URL.'))
        if url.scheme != 'https' and not (url.scheme == 'http' and os.environ.get('SGA_GATEWAY_ALLOW_HTTP') == '1'):
            raise UserError(self.env._('HTTPS is required outside explicit local development.'))
        return result

    def _envelope(self, config):
        self.ensure_one()
        canonical = json.dumps(self.payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
        return {
            'schema_version': '1', 'operation_id': self.operation_uuid, 'event_id': self.event_uuid,
            'origin_instance_id': config['SGA_GATEWAY_ORIGIN'], 'target_company_key': self.target_company_key,
            'aggregate_id': self.aggregate_uuid, 'revision': self.revision, 'action': self.action,
            'origin_system': 'odoo', 'actor_user_id': str(self.submitted_by.id),
            'occurred_at_utc': self.create_date.replace(tzinfo=timezone.utc).isoformat(),
            'expected_target_fingerprint': self.expected_target_fingerprint,
            'payload_sha256': hashlib.sha256(canonical).hexdigest(), 'payload': self.payload,
        }

    def _exchange(self, config, poll=False):
        self.ensure_one()
        url = config['SGA_GATEWAY_URL'].rstrip('/') + '/api/v1/operations/'
        if poll:
            try:
                uuid.UUID(self.gateway_id)
            except (ValueError, TypeError):
                self._gateway_update({'state': 'CONFLICTO', 'last_error': 'INVALID_GATEWAY_ID'})
                return
            url += self.gateway_id + '/'
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request('GET' if poll else 'POST', url,
                    json=None if poll else self._envelope(config),
                    headers={'Authorization': 'Bearer ' + config['SGA_GATEWAY_API_KEY']},
                    timeout=(3, 10), allow_redirects=False)
            if response.status_code not in (200, 201):
                vals = {'last_error': 'GATEWAY_HTTP_' + str(response.status_code)}
                if response.status_code == 409:
                    vals['state'] = 'CONFLICTO'
                elif 400 <= response.status_code < 500 and response.status_code != 429:
                    vals['state'] = 'RECHAZADO'
                self._gateway_update(vals)
                return
            data = response.json()
            uuid.UUID(data['id'])
            if data['state'] not in STATES or data['operation_id'] != self.operation_uuid or data['revision'] != self.revision or data['action'] != self.action or data['target_company_key'] != self.target_company_key or data['origin_instance_id'] != config['SGA_GATEWAY_ORIGIN']:
                raise ValueError('Response identity mismatch')
            self._gateway_update({'gateway_id': data['id'], 'state': data['state'], 'native_keys': data.get('native_keys', {}),
                        'last_error': data.get('last_error_code', ''), 'last_checked': fields.Datetime.now()})
        except requests.RequestException:
            self._gateway_update({'last_error': 'GATEWAY_CONNECTION_UNCONFIRMED'})
        except (ValueError, KeyError, TypeError):
            self._gateway_update({'state': 'CONFLICTO', 'last_error': 'INVALID_GATEWAY_RESPONSE'})

    @api.model
    def _cron_exchange(self):
        config = self._configuration()
        pending = self.search([('state', '=', 'EN_COLA'), ('gateway_id', '=', False)], limit=20)
        for op in pending:
            op._exchange(config)
        terminal = ['BORRADOR', 'SINCRONIZADO', 'RECHAZADO', 'CANCELADO_SIN_APLICAR', 'CONFLICTO']
        for op in self.search([('gateway_id', '!=', False), ('state', 'not in', terminal)], limit=20):
            op._exchange(config, poll=True)
