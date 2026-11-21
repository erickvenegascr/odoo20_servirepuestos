import hashlib
import json
import re
from collections import Counter, defaultdict

from odoo import api, fields, models
from odoo.exceptions import ValidationError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def tax_key(value):
    return re.sub(r'[^0-9]', '', value or '')


class VendorBinding(models.Model):
    _name = 'sga.supplier.binding'
    _description = 'Proveedor del catálogo SGA'
    _order = 'vendor_code'
    _rec_name = 'partner_id'

    company_id = fields.Many2one('res.company', required=True, index=True)
    source_instance = fields.Char(required=True, index=True)
    vendor_code = fields.Char(string='Código proveedor SGA', required=True, index=True)
    partner_id = fields.Many2one('res.partner', string='Proveedor', required=True, ondelete='restrict', check_company=True)
    source_name = fields.Char(string='Nombre en SGA', readonly=True)
    source_active = fields.Boolean(string='Activo en SGA', readonly=True)
    source_checksum = fields.Char(readonly=True, groups='base.group_system')
    source_snapshot = fields.Json(readonly=True, groups='base.group_system')
    _source_unique = models.Constraint('UNIQUE(company_id, source_instance, vendor_code)', 'Proveedor SGA ya vinculado.')


class SupplierInfo(models.Model):
    _inherit = 'product.supplierinfo'

    sga_binding_id = fields.Many2one('sga.supplier.binding', readonly=True, copy=False, ondelete='restrict', index=True)
    sga_vendor_code = fields.Char(related='sga_binding_id.vendor_code', string='Proveedor SGA', store=True)
    sga_article_id = fields.Integer(string='Artículo SGA', readonly=True, copy=False, index=True)
    sga_disabled = fields.Boolean(string='Deshabilitado en SGA', readonly=True, copy=False)
    sga_source_missing = fields.Boolean(string='Retirado de SGA', readonly=True, copy=False)
    sga_vendor_active = fields.Boolean(string='Proveedor activo en SGA', readonly=True, copy=False)
    sga_price_pending = fields.Boolean(string='Precio pendiente', compute='_compute_sga_price_pending', store=True)
    sga_source_checksum = fields.Char(readonly=True, copy=False, groups='base.group_system')
    sga_target_checksum = fields.Char(readonly=True, copy=False, groups='base.group_system')
    sga_source_snapshot = fields.Json(readonly=True, copy=False, groups='base.group_system')
    sga_previous_values = fields.Json(readonly=True, copy=False, groups='base.group_system')
    _source_unique = models.Constraint('UNIQUE(sga_binding_id, sga_article_id)', 'Relación artículo/proveedor SGA ya vinculada.')

    @api.depends('sga_binding_id', 'price')
    def _compute_sga_price_pending(self):
        for row in self:
            row.sga_price_pending = bool(row.sga_binding_id and row.price <= 0)

    def _get_filtered_supplier(self, company_id, product_id, params=False):
        rows = super()._get_filtered_supplier(company_id, product_id, params)
        return rows.filtered(lambda s: not s.sga_binding_id or (
            not s.sga_disabled and not s.sga_source_missing and s.sga_vendor_active and not s.sga_price_pending))


class Product(models.Model):
    _inherit = 'product.product'

    def _select_seller(self, partner_id=False, quantity=0.0, date=None, uom_id=False,
                       ordered_by='price_discounted', params=False):
        # Native Odoo 20 chooses price before sequence. Source priorities take
        # precedence only for a source product when choosing an unspecified vendor.
        if self.product_tmpl_id.sga_catalog_instance and not partner_id and ordered_by == 'price_discounted':
            ordered_by = 'sequence'
        return super()._select_seller(partner_id, quantity, date, uom_id, ordered_by, params)


class SupplierImport(models.AbstractModel):
    _name = 'sga.supplier.import'
    _description = 'Contrato de relaciones artículo/proveedor SGA'

    def _snapshot(self, row):
        return {'partner_id': row.partner_id.id, 'product_tmpl_id': row.product_tmpl_id.id,
                'product_id': row.product_id.id or False, 'company_id': row.company_id.id,
                'sequence': row.sequence, 'product_code': row.product_code or '',
                'disabled': row.sga_disabled, 'vendor_active': row.sga_vendor_active}

    def _lock_source(self, company, instance):
        lock = int(digest(['suppliers', company.id, instance])[:15], 16)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])

    def _flag_missing(self, instance, relations, dry_run=True):
        if (not isinstance(instance, str) or not 1 <= len(instance) <= 80 or type(dry_run) is not bool
                or not isinstance(relations, list) or not 1 <= len(relations) <= 1000):
            raise ValidationError('Contrato de relaciones retiradas fuera de límite.')
        company = self.env.company
        self._lock_source(company, instance)
        article_ids = set()
        keys = set()
        for row in relations:
            if (not isinstance(row, dict) or set(row) != {'article_id', 'vendor_code', 'source_checksum'}
                    or type(row['article_id']) is not int or not 0 < row['article_id'] < 2**31
                    or not isinstance(row['vendor_code'], str) or not re.fullmatch(r'[0-9A-Za-z]{1,3}', row['vendor_code'])
                    or not isinstance(row['source_checksum'], str) or not re.fullmatch(r'[0-9a-f]{64}', row['source_checksum'])):
                raise ValidationError('Identidad inválida para una relación retirada.')
            key = (row['article_id'], row['vendor_code'])
            if key in keys:
                raise ValidationError('Relación retirada repetida en el lote.')
            keys.add(key)
            article_ids.add(row['article_id'])
        Info = self.env['product.supplierinfo'].sudo()
        existing = {(row.sga_article_id, row.sga_vendor_code): row for row in Info.search([
            ('company_id', '=', company.id), ('sga_binding_id.company_id', '=', company.id),
            ('sga_binding_id.source_instance', '=', instance), ('sga_article_id', 'in', list(article_ids)),
        ])}
        archived = Info.browse()
        for row in relations:
            record = existing.get((row['article_id'], row['vendor_code']))
            if record and record.sga_source_checksum != row['source_checksum']:
                raise ValidationError('Cambió la relación en Odoo después de comprobar su retiro en SGA.')
            if record and not record.sga_source_missing:
                archived |= record
        if archived and not dry_run:
            archived.write({'sga_source_missing': True})
        return {'relations': {'missing': len(archived), 'unchanged': len(relations) - len(archived)},
                'count': len(relations), 'last_key': [relations[-1]['article_id'], relations[-1]['vendor_code']],
                'dry_run': dry_run, 'sga_writes_enabled': False}

    def _apply(self, instance, vendors, relations, dry_run=True):
        if (not isinstance(instance, str) or not 1 <= len(instance) <= 80 or type(dry_run) is not bool
                or not isinstance(vendors, list) or len(vendors) > 800
                or not isinstance(relations, list) or len(relations) > 1000):
            raise ValidationError('Contrato de proveedores fuera de límite.')
        company = self.env.company
        self._lock_source(company, instance)
        Bind = self.env['sga.supplier.binding'].sudo()
        Partner = self.env['res.partner'].sudo().with_context(active_test=False, tracking_disable=True)
        Info = self.env['product.supplierinfo'].sudo()
        bindings = {b.vendor_code: b for b in Bind.search([('company_id', '=', company.id), ('source_instance', '=', instance)])}
        partners = defaultdict(list)
        for partner in Partner.search([('parent_id', '=', False), ('company_id', 'in', [False, company.id]), ('vat', '!=', False)]):
            key = tax_key(partner.vat)
            if key:
                partners[key].append(partner)
        vendor_counts = Counter(); counts = Counter(); seen = set(); virtual = {}
        for row in vendors:
            if (not isinstance(row, dict) or set(row) != {'code', 'name', 'tax_id', 'active'}
                    or not isinstance(row['code'], str) or not re.fullmatch(r'[0-9A-Za-z]{1,3}', row['code'])
                    or not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 100
                    or not isinstance(row['tax_id'], str) or len(row['tax_id']) > 20 or type(row['active']) is not bool
                    or row['code'] in seen):
                raise ValidationError('Proveedor SGA inválido o repetido.')
            code = row['code']; seen.add(code); old = bindings.get(code); checksum = digest(row)
            if old:
                if old.partner_id.company_id and old.partner_id.company_id != company:
                    raise ValidationError('Proveedor vinculado a otra compañía.')
                status = 'unchanged' if old.source_checksum == checksum else 'updated'
                if status == 'updated' and not dry_run:
                    old.write({'source_name': row['name'], 'source_active': row['active'], 'source_checksum': checksum, 'source_snapshot': row})
                virtual[code] = old.partner_id
            else:
                matches = partners.get(tax_key(row['tax_id']), []) if tax_key(row['tax_id']) else []
                if len(matches) > 1:
                    raise ValidationError('Identidad fiscal ambigua para proveedor SGA %s.' % code)
                partner = matches[0] if matches else Partner.browse()
                vendor_counts['matched_partner' if partner else 'new_partner'] += 1
                if not dry_run:
                    if not partner:
                        # Fiscal/contact details are retained in the private source
                        # snapshot. This contract does not configure fiscal identities.
                        partner = Partner.create({'name': row['name'], 'is_company': True,
                            'company_id': company.id, 'supplier_rank': 1,
                            'ref': 'SGA:%s:%s' % (instance, code)})
                    old = Bind.create({'company_id': company.id, 'source_instance': instance, 'vendor_code': code,
                        'partner_id': partner.id, 'source_name': row['name'], 'source_active': row['active'],
                        'source_checksum': checksum, 'source_snapshot': row})
                    bindings[code] = old
                virtual[code] = partner
                status = 'created'
            vendor_counts[status] += 1
        # All article identities are resolved through the existing catalogue contract.
        article_ids = []; seen = set()
        for row in relations:
            if (not isinstance(row, dict) or set(row) != {'article_id', 'vendor_code', 'product_code', 'priority', 'disabled', 'vendor_active'}
                    or type(row['article_id']) is not int or not 0 < row['article_id'] < 2**31
                    or not isinstance(row['vendor_code'], str) or not re.fullmatch(r'[0-9A-Za-z]{1,3}', row['vendor_code'])
                    or not isinstance(row['product_code'], str) or len(row['product_code']) > 20
                    or type(row['priority']) is not int or not 0 <= row['priority'] <= 32767
                    or type(row['disabled']) is not bool or type(row['vendor_active']) is not bool):
                raise ValidationError('Relación artículo/proveedor inválida.')
            key = (row['article_id'], row['vendor_code'])
            if key in seen:
                raise ValidationError('Relación repetida en el lote.')
            seen.add(key); article_ids.append(row['article_id'])
        products = {p.sga_catalog_article_id: p for p in self.env['product.template'].sudo().with_context(active_test=False).search([
            ('company_id', '=', company.id), ('sga_catalog_instance', '=', instance), ('sga_catalog_article_id', 'in', article_ids)])}
        existing = {(r.sga_article_id, r.sga_vendor_code): r for r in Info.search([
            ('sga_binding_id.company_id', '=', company.id), ('sga_binding_id.source_instance', '=', instance),
            ('sga_article_id', 'in', article_ids)])}
        candidates = defaultdict(list)
        for r in Info.search([('company_id', '=', company.id), ('product_tmpl_id', 'in', [p.id for p in products.values()]), ('sga_binding_id', '=', False)]):
            candidates[(r.product_tmpl_id.id, r.partner_id.id)].append(r)
        create_values = []
        for row in relations:
            code = row['vendor_code']; product = products.get(row['article_id']); binding = bindings.get(code)
            if not product or (not binding and code not in virtual):
                raise ValidationError('Falta el artículo o proveedor SGA: %s/%s.' % (row['article_id'], code))
            if product.company_id != company:
                raise ValidationError('Artículo de otra compañía.')
            partner = binding.partner_id if binding else virtual[code]
            old = existing.get((row['article_id'], code)); adopted = False
            if not old and partner:
                matches = candidates.get((product.id, partner.id), [])
                exact = [r for r in matches if (r.product_code or '') == row['product_code']]
                matches = exact if exact else matches
                if len(matches) > 1:
                    raise ValidationError('Varias tarifas existentes para %s/%s; requiere revisión.' % (row['article_id'], code))
                old = matches[0] if matches else False
                if old:
                    candidates[(product.id, partner.id)].remove(old); adopted = True
            checksum = digest(row)
            if old and old.sga_target_checksum and digest(self._snapshot(old)) != old.sga_target_checksum:
                raise ValidationError('Relación modificada localmente: %s/%s.' % (row['article_id'], code))
            desired = {'partner_id': partner.id or False, 'product_tmpl_id': product.id,
                'product_id': old.product_id.id if old else False, 'company_id': company.id,
                'sequence': row['priority'], 'product_code': row['product_code'],
                'disabled': row['disabled'], 'vendor_active': row['vendor_active']}
            unchanged = (old and not adopted and not old.sga_source_missing
                         and old.sga_source_checksum == checksum and self._snapshot(old) == desired)
            status = 'unchanged' if unchanged else 'adopted' if adopted else 'updated' if old else 'created'
            counts[status] += 1
            counts['disabled'] += int(row['disabled']); counts['inactive_vendor'] += int(not row['vendor_active'])
            counts['missing_code'] += int(not row['product_code'])
            if dry_run or unchanged:
                continue
            values = {'sga_binding_id': binding.id, 'sga_article_id': row['article_id'],
                'sequence': row['priority'], 'product_code': row['product_code'] or False,
                'sga_disabled': row['disabled'], 'sga_vendor_active': row['vendor_active'], 'sga_source_missing': False,
                'sga_source_checksum': checksum, 'sga_target_checksum': digest(desired), 'sga_source_snapshot': row}
            if old:
                if adopted:
                    values['sga_previous_values'] = dict(self._snapshot(old), price=old.price, currency=old.currency_id.name,
                        min_qty=old.min_qty, uom_id=old.uom_id.id, discount=old.discount, delay=old.delay)
                old.write(values)
            else:
                values.update(partner_id=partner.id, product_tmpl_id=product.id, company_id=company.id,
                    price=0, currency_id=company.currency_id.id, min_qty=1, uom_id=product.uom_id.id)
                create_values.append(values)
        if create_values:
            Info.create(create_values)
        return {'vendors': dict(vendor_counts), 'relations': dict(counts), 'count': len(relations),
                'last_key': [relations[-1]['article_id'], relations[-1]['vendor_code']] if relations else [0, ''],
                'dry_run': dry_run, 'sga_writes_enabled': False}
