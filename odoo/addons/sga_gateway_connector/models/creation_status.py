from odoo import api, fields, models


CREATION_STATES = [
    ('not_sent', 'Solo en Odoo'), ('disabled', 'Alta en SGA deshabilitada'),
    ('pending', 'Alta en SGA pendiente'), ('waiting', 'Esperando confirmación'),
    ('synced', 'Confirmado en SGA'), ('linked', 'Vinculado a SGA'),
    ('conflict', 'Requiere revisión'), ('error', 'Solicitud rechazada'),
    ('paused', 'Sincronización pausada'),
]


class Product(models.Model):
    _inherit = 'product.template'

    sga_creation_visible = fields.Boolean(compute='_compute_sga_creation_status', compute_sudo=True)
    sga_creation_state = fields.Selection(CREATION_STATES, string='Estado en SGA',
        compute='_compute_sga_creation_status', compute_sudo=True)
    sga_creation_message = fields.Char(string='Proceso de alta',
        compute='_compute_sga_creation_status', compute_sudo=True)
    sga_code_managed = fields.Boolean(compute='_compute_sga_creation_status',compute_sudo=True)
    sga_reference_deferred = fields.Boolean(compute='_compute_sga_creation_status', compute_sudo=True)

    @api.model
    def _sga_creation_application(self, company=None):
        company = company or self.env.company
        return self.env['sga.gateway.application'].sudo().search([
            ('company_id', '=', company.id), ('active', '=', True),
            ('catalog_enabled', '=', True)], limit=1)

    @api.depends('company_id', 'sga_catalog_article_id', 'sga_catalog_instance')
    def _compute_sga_creation_status(self):
        apps = {}
        for product in self:
            company = product.company_id or self.env.company
            if company.id not in apps:
                apps[company.id] = self._sga_creation_application(company)
            app = apps[company.id]
            product.sga_code_managed = bool(product.sga_catalog_article_id)
            product.sga_creation_visible = bool(app or product.sga_catalog_instance)
            linked = bool(product.sga_catalog_instance and product.sga_catalog_article_id)
            enabled = app and 'article_sync_enabled' in app._fields and app.article_sync_enabled
            product.sga_creation_state = 'linked' if linked else 'not_sent' if enabled else 'disabled'
            product.sga_reference_deferred = bool(app and not linked)
            product.sga_creation_message = (
                'Artículo vinculado a SGA: %s.' % str(product.sga_catalog_article_id).zfill(6) if linked else
                'Solo guardado en Odoo. Inicie el alta en SGA para obtener su código definitivo.' if enabled else
                'Solo guardado en Odoo. El alta en SGA está deshabilitada; el código definitivo está pendiente.')

    @api.model_create_multi
    def create(self, vals_list):
        cleaned = []
        for incoming in vals_list:
            vals = dict(incoming)
            if (not vals.get('sga_catalog_article_id') and not vals.get('sga_catalog_instance')
                    and not self.env.context.get('sga_card_internal')):
                company = self.env['res.company'].browse(vals.get('company_id')) if vals.get('company_id') else self.env.company
                if self._sga_creation_application(company):
                    vals['default_code'] = False
            cleaned.append(vals)
        products = super().create(cleaned)
        if not self.env.context.get('sga_card_internal'):
            for product in products:
                app = product._sga_creation_application(product.company_id or self.env.company)
                automatic = (app and 'article_sync_enabled' in app._fields and app.article_sync_enabled
                             and app.article_auto_create and product.company_id)
                if app and not product.sga_catalog_article_id and not automatic:
                    product._sga_creation_notify(product.create_uid,
                        product.sga_creation_message, notification_type='warning')
        return products

    def _sga_creation_notify(self, user, message, *, notification_type='info'):
        self.ensure_one()
        self.sudo().message_post(body=message, message_type='notification',
            subtype_xmlid='mail.mt_note', author_id=self.env.ref('base.partner_root').id)
        if (user and user.active and user._is_internal()
                and self.with_user(user).has_access('read')):
            user.sudo()._bus_send('simple_notification', {
                'title': 'Alta de artículo en SGA', 'message': self.name+': '+message,
                'type': notification_type, 'sticky': True})


class Variant(models.Model):
    _inherit = 'product.product'

    @api.model
    def _sga_reference_deferred(self, template=None):
        if template and (template.sga_catalog_instance or template.sga_catalog_article_id):
            return False
        company = template.company_id if template and template.company_id else self.env.company
        return bool(self.env['product.template']._sga_creation_application(company))
