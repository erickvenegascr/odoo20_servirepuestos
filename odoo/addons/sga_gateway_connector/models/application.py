import hashlib
import re
import secrets
import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class GatewayApplication(models.Model):
    _name = 'sga.gateway.application'
    _description = 'Aplicación gateway SGA'
    _order = 'name, id'

    name = fields.Char(required=True)
    application_uuid = fields.Char(default=lambda _: str(uuid.uuid4()), required=True, readonly=True, copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    source_instance = fields.Char(required=True)
    active = fields.Boolean(default=True)
    catalog_enabled = fields.Boolean(default=True, string='Recibir catálogo')
    exchange_enabled = fields.Boolean(default=True, string='Comunicación de regreso')
    token_hash = fields.Char(readonly=True, copy=False, groups='base.group_system', index=True)
    last_seen_at = fields.Datetime(readonly=True, copy=False)
    token_changed_at = fields.Datetime(readonly=True, copy=False)
    message_ids = fields.One2many('sga.gateway.message', 'application_id', readonly=True)
    _unique_uuid = models.Constraint('UNIQUE(application_uuid)', 'La identidad de aplicación ya existe.')
    _unique_token = models.Constraint('UNIQUE(token_hash)', 'El token ya pertenece a otra aplicación.')
    _unique_source = models.Constraint('UNIQUE(company_id, source_instance)', 'Este origen ya tiene una conexión en la compañía.')

    @api.constrains('token_hash', 'source_instance')
    def _check_configuration(self):
        for app in self:
            if not app.source_instance or len(app.source_instance) > 80:
                raise ValidationError('La instancia SGA debe contener entre 1 y 80 caracteres.')
            if app.token_hash and not re.fullmatch(r'[a-f0-9]{64}', app.token_hash):
                raise ValidationError('Se requiere la huella SHA-256 del token.')

    def _require_admin(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError('Solo un administrador puede configurar aplicaciones gateway.')

    @api.model_create_multi
    def create(self, values_list):
        self._require_admin()
        return super().create(values_list)

    def write(self, values):
        self._require_admin()
        return super().write(values)

    def action_generate_token(self):
        self.ensure_one()
        self._require_admin()
        token = 'sgw1_' + secrets.token_urlsafe(48)
        self.write({'token_hash': hashlib.sha256(token.encode()).hexdigest(),
                    'token_changed_at': fields.Datetime.now(), 'active': True})
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': 'Token de aplicación: copie ahora', 'message': token,
            'type': 'warning', 'sticky': True,
        }}

    def action_revoke(self):
        self._require_admin()
        self.write({'active': False})

    def action_ping(self):
        self.ensure_one()
        self._require_admin()
        self.env['sga.gateway.message'].create({'application_id': self.id, 'kind': 'ping', 'payload': {}})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.model
    def _authenticate_token(self, token):
        if not isinstance(token, str) or not 40 <= len(token) <= 128 or not token.startswith('sgw1_'):
            return self.browse()
        digest = hashlib.sha256(token.encode()).hexdigest()
        app = self.sudo().search([('active', '=', True), ('token_hash', '=', digest)], limit=1)
        if not app or not secrets.compare_digest(app.token_hash, digest):
            return self.browse()
        return app

    def _touch(self):
        # Public-route callers cannot use the administrative write override.
        self.ensure_one()
        super(GatewayApplication, self.sudo()).write({'last_seen_at': fields.Datetime.now()})


class GatewayMessage(models.Model):
    _name = 'sga.gateway.message'
    _description = 'Bandeja Odoo hacia gateway SGA'
    _order = 'id desc'

    message_uuid = fields.Char(default=lambda _: str(uuid.uuid4()), required=True, readonly=True, copy=False)
    application_id = fields.Many2one('sga.gateway.application', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='application_id.company_id', store=True, readonly=True)
    kind = fields.Selection([('ping', 'Prueba de conexión'), ('request', 'Solicitud futura')], required=True, default='ping')
    payload = fields.Json(default=dict)
    state = fields.Selection([('pending', 'Pendiente'), ('leased', 'Entregado'),
                              ('acknowledged', 'Confirmado')], default='pending', readonly=True, index=True)
    lease_uuid = fields.Char(readonly=True, copy=False)
    lease_until = fields.Datetime(readonly=True, copy=False)
    result = fields.Json(readonly=True, copy=False)
    acknowledged_at = fields.Datetime(readonly=True, copy=False)
    _unique_message = models.Constraint('UNIQUE(message_uuid)', 'El mensaje ya existe.')

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError('Solo un administrador puede crear solicitudes gateway.')
        for vals in values_list:
            if vals.get('state', 'pending') != 'pending':
                raise ValidationError('Los mensajes nuevos deben iniciar pendientes.')
            if 'payload' in vals and not isinstance(vals['payload'], dict):
                raise ValidationError('El contenido del mensaje debe ser un objeto JSON.')
        return super().create(values_list)

    def write(self, vals):
        raise AccessError('La bandeja solo cambia mediante su protocolo controlado.')

    @api.model
    def _claim(self, app, limit=10):
        now = fields.Datetime.now()
        self.env.cr.execute('''SELECT id FROM sga_gateway_message
            WHERE application_id=%s AND (state='pending' OR (state='leased' AND lease_until <= %s))
            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED''', [app.id, now, limit])
        records = self.sudo().browse([row[0] for row in self.env.cr.fetchall()])
        result = []
        for message in records:
            lease = str(uuid.uuid4())
            super(GatewayMessage, message).write({'state': 'leased', 'lease_uuid': lease,
                                                  'lease_until': now + timedelta(seconds=120)})
            result.append({'message_id': message.message_uuid, 'lease': lease,
                           'kind': message.kind, 'payload': message.payload or {}})
        return result

    @api.model
    def _ack(self, app, message_id, lease, result):
        record = self.sudo().search([('application_id', '=', app.id), ('message_uuid', '=', message_id)], limit=1)
        if not record:
            return 'not_found'
        self.env.cr.execute('SELECT id FROM sga_gateway_message WHERE id=%s FOR UPDATE', [record.id])
        record.invalidate_recordset()
        if record.state == 'acknowledged':
            return 'ok' if record.lease_uuid == lease and (record.result or {}) == result else 'conflict'
        if record.state != 'leased' or record.lease_uuid != lease or record.lease_until < fields.Datetime.now():
            return 'conflict'
        super(GatewayMessage, record).write({'state': 'acknowledged', 'result': result,
                                             'acknowledged_at': fields.Datetime.now()})
        return 'ok'
