import hashlib
import json
import math
import re
from collections import Counter, defaultdict

from odoo import api, fields, models
from odoo.exceptions import ValidationError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def number(value, minimum=0, maximum=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValidationError('Número de catálogo inválido.')
    return float(value)


MASTER_MODELS = {
    'division': 'product.catalog.division', 'line': 'product.catalog.line',
    'subcategory': 'product.catalog.subcategory', 'brand': 'product.catalog.brand',
}
PRODUCT_FIELDS = ['cabys_code', 'cabys_description', 'commercial_measurement', 'code_type_id',
                  'taxes_id', 'supplier_taxes_id', 'catalog_division_id', 'catalog_line_id',
                  'catalog_subcategory_id', 'catalog_brand_id', 'uom_id', 'standard_price', 'list_price']


class SourceMaster(models.AbstractModel):
    _name = 'sga.catalog.master.mixin'
    _description = 'Identidad del catálogo de origen SGA'

    sga_details_instance = fields.Char(readonly=True, copy=False, index=True)
    sga_details_key = fields.Char(readonly=True, copy=False, index=True)
    sga_details_checksum = fields.Char(readonly=True, copy=False)
    _sga_source_unique = models.Constraint('UNIQUE(sga_details_instance, sga_details_key)', 'La clave de catálogo SGA ya existe.')


class Division(models.Model):
    _name = 'product.catalog.division'
    _inherit = ['product.catalog.division', 'sga.catalog.master.mixin']


class Line(models.Model):
    _name = 'product.catalog.line'
    _inherit = ['product.catalog.line', 'sga.catalog.master.mixin']


class Brand(models.Model):
    _name = 'product.catalog.brand'
    _inherit = ['product.catalog.brand', 'sga.catalog.master.mixin']


class Subcategory(models.Model):
    _name = 'product.catalog.subcategory'
    _inherit = 'sga.catalog.master.mixin'
    _description = 'Subcategoría de producto'
    _order = 'name, id'

    name = fields.Char(string='Subcategoría', required=True)
    code = fields.Char(string='Código', required=True, index=True)
    line_id = fields.Many2one('product.catalog.line', string='Línea', required=True, ondelete='restrict')
    active = fields.Boolean(default=True)
    _code_unique = models.Constraint('UNIQUE(line_id, code)', 'El código de subcategoría ya existe en esta línea.')


class Product(models.Model):
    _inherit = 'product.template'

    catalog_subcategory_id = fields.Many2one('product.catalog.subcategory', string='Subcategoría', ondelete='restrict',
                                            domain="[('line_id', '=', catalog_line_id)]", index=True)
    sga_detail_binding_ids = fields.One2many('sga.catalog.detail.binding', 'product_id')
    sga_catalog_review_note = fields.Char(compute='_compute_sga_catalog_review_note', compute_sudo=True)

    @api.depends('sga_detail_binding_ids.missing_cabys', 'sga_detail_binding_ids.missing_price',
                 'sga_detail_binding_ids.unverified_cabys')
    def _compute_sga_catalog_review_note(self):
        for product in self:
            notes = []
            bindings = product.sga_detail_binding_ids
            if any(bindings.mapped('missing_cabys')):
                notes.append('SGA no tiene CABYS para este artículo.')
            if any(bindings.mapped('unverified_cabys')):
                notes.append('El CABYS de SGA requiere revisión en el catálogo oficial.')
            if any(bindings.mapped('missing_price')):
                notes.append('SGA no tiene una regla de precio para este artículo.')
            product.sga_catalog_review_note = ' '.join(notes) or False

    @api.onchange('catalog_line_id')
    def _onchange_sga_line(self):
        if self.catalog_subcategory_id.line_id != self.catalog_line_id:
            self.catalog_subcategory_id = False

    @api.constrains('catalog_subcategory_id', 'catalog_line_id')
    def _check_sga_subcategory(self):
        for product in self:
            if product.catalog_subcategory_id and product.catalog_subcategory_id.line_id != product.catalog_line_id:
                raise ValidationError('La subcategoría debe pertenecer a la línea del artículo.')


class DetailBinding(models.Model):
    _name = 'sga.catalog.detail.binding'
    _description = 'Revisión de categorías, datos fiscales y precios SGA'
    _order = 'article_id'
    _rec_name = 'product_id'

    company_id = fields.Many2one('res.company', required=True, index=True)
    source_instance = fields.Char(required=True, index=True)
    article_id = fields.Integer(string='Artículo SGA', required=True, index=True)
    product_id = fields.Many2one('product.template', string='Producto', required=True, ondelete='restrict', index=True)
    product_active = fields.Boolean(related='product_id.active', store=True, string='Artículo activo')
    source_checksum = fields.Char(readonly=True, groups='base.group_system')
    target_checksum = fields.Char(readonly=True, groups='base.group_system')
    managed_fields = fields.Json(readonly=True, groups='base.group_system')
    missing_cabys = fields.Boolean(string='Sin CABYS en SGA')
    unverified_cabys = fields.Boolean(string='CABYS requiere revisión')
    missing_price = fields.Boolean(string='Sin precio en SGA')
    tax_corrected = fields.Boolean(string='Tarifa inconsistente en origen')
    source_snapshot = fields.Json(readonly=True, groups='base.group_system')
    _source_unique = models.Constraint('UNIQUE(company_id, source_instance, article_id)', 'El artículo SGA ya está vinculado.')


class Pricelist(models.Model):
    _inherit = 'product.pricelist'

    sga_details_instance = fields.Char(readonly=True, copy=False, index=True)
    sga_details_level = fields.Integer(readonly=True, copy=False, index=True)
    sga_details_checksum = fields.Char(readonly=True, copy=False)
    _sga_level_unique = models.Constraint('UNIQUE(company_id, sga_details_instance, sga_details_level)', 'El nivel SGA ya existe.')


class PriceRule(models.Model):
    _inherit = 'product.pricelist.item'

    # SGA keeps fractional margins beyond two decimal places.
    price_discount = fields.Float(digits=(16, 8))
    price_markup = fields.Float(digits=(16, 8))
    sga_details_instance = fields.Char(readonly=True, copy=False, index=True)
    sga_details_article = fields.Integer(readonly=True, copy=False, index=True)
    sga_details_level = fields.Integer(readonly=True, copy=False, index=True)
    sga_details_checksum = fields.Char(readonly=True, copy=False)
    sga_details_target_checksum = fields.Char(readonly=True, copy=False)
    sga_details_source = fields.Json(readonly=True, groups='base.group_system')
    _sga_rule_unique = models.Constraint(
        'UNIQUE(pricelist_id, sga_details_instance, sga_details_article, sga_details_level)', 'La regla SGA ya existe.')


class DetailsImport(models.AbstractModel):
    _name = 'sga.catalog.details.import'
    _description = 'Contrato de catálogo comercial y fiscal SGA'

    def _product_snapshot(self, product, names):
        result = {}
        for name in names:
            field = product._fields[name]
            value = product[name]
            if field.type == 'many2one':
                value = value.id or False
            elif field.type in ('one2many', 'many2many'):
                value = sorted(value.ids)
            elif field.type in ('float', 'monetary'):
                value = round(value, 8)
            result[name] = value or False
        return result

    def _rule_snapshot(self, rule):
        result = {name: round(rule[name], 8) if isinstance(rule[name], float) else rule[name]
                for name in ['compute_price', 'base', 'price_discount', 'fixed_price', 'price_round',
                             'price_surcharge', 'price_min_margin', 'price_max_margin', 'min_quantity']}
        result.update(product_tmpl_id=rule.product_tmpl_id.id, product_id=rule.product_id.id,
                      pricelist_id=rule.pricelist_id.id, applied_on=rule.applied_on,
                      base_pricelist_id=rule.base_pricelist_id.id,
                      date_start=rule.date_start.isoformat() if rule.date_start else False,
                      date_end=rule.date_end.isoformat() if rule.date_end else False)
        return result

    def _masters(self, instance, rows, dry_run):
        if not isinstance(rows, list) or len(rows) > 3000:
            raise ValidationError('Catálogos maestros fuera de límite.')
        maps = {kind: {x.sga_details_key: x for x in self.env[model].sudo().with_context(active_test=False).search([
            ('sga_details_instance', '=', instance)])} for kind, model in MASTER_MODELS.items()}
        maps['pricelist'] = {str(x.sga_details_level): x for x in self.env['product.pricelist'].sudo().with_context(active_test=False).search([
            ('company_id', '=', self.env.company.id), ('sga_details_instance', '=', instance)])}
        seen = set();counts = Counter()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'kind', 'key', 'code', 'name', 'parent', 'currency'}
                    or row['kind'] not in maps or not isinstance(row['key'], str) or not 1 <= len(row['key']) <= 255
                    or not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 255
                    or not isinstance(row['code'], str) or not 1 <= len(row['code']) <= 64):
                raise ValidationError('Catálogo maestro inválido.')
            kind = row['kind'];key = row['key']
            if (kind, key) in seen:
                raise ValidationError('Clave de catálogo repetida.')
            seen.add((kind, key))
            values = {'name': row['name'], 'active': True}
            if kind == 'pricelist':
                if not re.fullmatch(r'[1-9][0-9]{0,5}', key) or row['currency'] not in ('CRC', 'USD') or row['parent']:
                    raise ValidationError('Nivel o moneda inválidos.')
                currency = self.env['res.currency'].sudo().with_context(active_test=False).search([('name', '=', row['currency'])], limit=1)
                if not currency or not currency.active:
                    raise ValidationError('La moneda de la lista debe estar activa.')
                if currency != self.env.company.currency_id:
                    raise ValidationError('La conversión de moneda requiere un contrato adicional.')
                values.update(company_id=self.env.company.id, currency_id=currency.id,
                              sga_details_instance=instance, sga_details_level=int(key))
                old = maps[kind].get(key)
                model = self.env['product.pricelist'].sudo()
                fingerprint = digest({'name': values['name'], 'currency': currency.name, 'active': True})
                if old and old.sga_details_checksum and digest({'name': old.name, 'currency': old.currency_id.name, 'active': old.active}) != old.sga_details_checksum:
                    raise ValidationError('La lista fue modificada en Odoo.')
            else:
                if row['currency']:
                    raise ValidationError('Un catálogo de clasificación no admite moneda.')
                values['code'] = row['code']
                parent_kind = {'line': 'division', 'subcategory': 'line'}.get(kind)
                parent = None
                if parent_kind:
                    if row['parent'] not in maps[parent_kind]:
                        raise ValidationError('Falta el catálogo padre.')
                    parent = maps[parent_kind][row['parent']]
                    values['division_id' if kind == 'line' else 'line_id'] = parent.id if parent else False
                elif row['parent']:
                    raise ValidationError('Padre de catálogo inesperado.')
                old = maps[kind].get(key)
                model = self.env[MASTER_MODELS[kind]].sudo()
                fingerprint = digest({'name': row['name'], 'code': row['code'], 'parent': row['parent'], 'active': True})
                if old and old.sga_details_checksum:
                    parent_value = old.division_id.sga_details_key if kind == 'line' else old.line_id.sga_details_key if kind == 'subcategory' else None
                    if digest({'name': old.name, 'code': old.code, 'parent': parent_value, 'active': old.active}) != old.sga_details_checksum:
                        raise ValidationError('La clasificación fue modificada en Odoo.')
                if not old:
                    domain = [('code', '=', row['code'])]
                    if parent_kind and parent:
                        domain += [('division_id' if kind == 'line' else 'line_id', '=', parent.id)]
                    candidates = model.with_context(active_test=False).search(domain) if not parent_kind or parent else model.browse()
                    if candidates:
                        if len(candidates) != 1 or candidates.name != row['name'] or candidates.sga_details_instance:
                            raise ValidationError('El código de clasificación ya está ocupado.')
                        old = candidates
                values.update(sga_details_instance=instance, sga_details_key=key)
            values['sga_details_checksum'] = fingerprint
            if not old or old.sga_details_checksum != fingerprint:
                counts['would_create' if not old and dry_run else 'would_update' if dry_run else 'created' if not old else 'updated'] += 1
                if not dry_run:
                    old = model.create(values) if not old else (old.write(values) and old)
            else:
                counts['unchanged'] += 1
            maps[kind][key] = old
        return maps, dict(counts)

    def _tax(self, usage, code, amount):
        tax = self.env['account.tax'].sudo().search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', usage),
            ('crfe_tax_code', '=', '01'), ('crfe_vat_rate_code', '=', code),
            ('amount_type', '=', 'percent'), ('amount', '>=', amount - .00001), ('amount', '<=', amount + .00001),
            ('active', '=', True)])
        if len(tax) != 1 or tax.price_include:
            raise ValidationError('Se requiere un impuesto CRFE único, con tarifa exacta y precio sin impuesto.')
        return tax

    def _validate_product(self, row):
        keys = {'article_id', 'division', 'line', 'subcategory', 'brand', 'cabys', 'cabys_description', 'cabys_status',
                'commercial_unit', 'unit_cost', 'vat_rate', 'vat_code', 'tax_corrected', 'prices', 'snapshot'}
        if not isinstance(row, dict) or set(row) != keys or type(row['article_id']) is not int or row['article_id'] <= 0:
            raise ValidationError('Artículo comercial inválido.')
        if row['cabys'] and (not isinstance(row['cabys'], str) or not re.fullmatch(r'[0-9]{13}', row['cabys'])):
            raise ValidationError('El CABYS debe contener 13 dígitos.')
        if (not isinstance(row['cabys_description'], str) or len(row['cabys_description']) > 1000
                or row['cabys_status'] not in ('found', 'not_found', 'rejected', 'missing')
                or row['commercial_unit'] not in ('Unid', 'cm', 'm') or type(row['tax_corrected']) is not bool
                or not isinstance(row['snapshot'], dict) or len(json.dumps(row['snapshot'])) > 6000):
            raise ValidationError('Datos fiscales inválidos.')
        if bool(row['cabys']) == (row['cabys_status'] == 'missing'):
            raise ValidationError('Estado CABYS incoherente.')
        number(row['unit_cost']);number(row['vat_rate'], 0, 100)
        if not isinstance(row['vat_code'], str) or not re.fullmatch(r'[0-9]{2}', row['vat_code']):
            raise ValidationError('Código de tarifa inválido.')
        if not isinstance(row['prices'], list) or len(row['prices']) > 50:
            raise ValidationError('Reglas de precio fuera de límite.')
        levels = set()
        for price in row['prices']:
            if (not isinstance(price, dict) or set(price) != {'level', 'mode', 'markup', 'fixed_price', 'source'}
                    or type(price['level']) is not int or not 1 <= price['level'] <= 999999
                    or price['mode'] not in ('markup', 'fixed') or not isinstance(price['source'], dict)
                    or len(json.dumps(price['source'])) > 6000 or price['level'] in levels):
                raise ValidationError('Regla de precio inválida.')
            number(price['markup'], -100, 10000000);number(price['fixed_price'])
            levels.add(price['level'])

    def _assert_cost_unit_safe(self, product, values):
        cost_changed = 'standard_price' in values and abs(product.standard_price - values['standard_price']) > .00000001
        unit_changed = values.get('uom_id', product.uom_id.id) != product.uom_id.id
        if not cost_changed and not unit_changed:
            return
        domain = [('product_id.product_tmpl_id', '=', product.id)]
        if (self.env['stock.move'].sudo().search_count(domain, limit=1)
                or self.env['stock.quant'].sudo().search_count(domain + [('quantity', '!=', 0)], limit=1)):
            raise ValidationError('El costo o unidad de un producto con inventario requiere conciliación previa.')
        if unit_changed and (self.env['sale.order.line'].sudo().search_count(domain, limit=1)
                             or self.env['account.move.line'].sudo().search_count(domain, limit=1)):
            raise ValidationError('La unidad ya tiene operaciones y requiere conciliación previa.')

    def _apply_details_batch(self, instance, masters, rows, dry_run=True):
        if type(dry_run) is not bool or not isinstance(rows, list) or len(rows) > 100:
            raise ValidationError('Lote comercial fuera de límite.')
        for row in rows:
            self._validate_product(row)
        if len({r.get('article_id') for r in rows if isinstance(r, dict)}) != len(rows):
            raise ValidationError('Artículo repetido en el lote.')
        lock = int.from_bytes(hashlib.sha256(f'details:{self.env.company.id}:{instance}'.encode()).digest()[:8], 'big', signed=True)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])
        maps, master_counts = self._masters(instance, masters, dry_run)
        code_type = self.env['crfe.code.type.product.template'].sudo().search([('code', '=', '04')], limit=1)
        if not code_type:
            raise ValidationError('Falta el tipo de código comercial interno CRFE.')
        Product = self.env['product.template'].sudo().with_company(self.env.company).with_context(
            active_test=False, tracking_disable=True, mail_notrack=True)
        Binding = self.env['sga.catalog.detail.binding'].sudo()
        Rule = self.env['product.pricelist.item'].sudo()
        articles = [r['article_id'] for r in rows]
        products = {p.sga_catalog_article_id: p for p in Product.search([
            ('company_id', '=', self.env.company.id), ('sga_catalog_instance', '=', instance), ('sga_catalog_article_id', 'in', articles)])}
        bindings = {b.article_id: b for b in Binding.search([
            ('company_id', '=', self.env.company.id), ('source_instance', '=', instance), ('article_id', 'in', articles)])}
        rules = {(r.sga_details_article, r.sga_details_level): r for r in Rule.search([
            ('company_id', '=', self.env.company.id), ('sga_details_instance', '=', instance), ('sga_details_article', 'in', articles)])}
        plans = [];new_rules = [];counts = Counter();price_counts = Counter();issues = Counter();tax_cache = {}
        for row in rows:
            article = row['article_id'];product = products.get(article);binding = bindings.get(article)
            if not product:
                raise ValidationError('El artículo debe estar vinculado al catálogo SGA de esta compañía.')
            if binding and binding.product_id != product:
                raise ValidationError('La vinculación del artículo cambió.')
            values = {}
            for kind, name in [('division', 'catalog_division_id'), ('line', 'catalog_line_id'),
                               ('subcategory', 'catalog_subcategory_id'), ('brand', 'catalog_brand_id')]:
                key = row[kind]
                if key is not None and key not in maps[kind]:
                    raise ValidationError('Falta una clasificación del artículo.')
                record = maps[kind].get(key)
                values[name] = record.id if record else False
            if row['cabys']:
                values['cabys_code'] = row['cabys']
                if row['cabys_description']:
                    values['cabys_description'] = row['cabys_description']
            for usage in ('sale', 'purchase'):
                tax_key = (usage, row['vat_code'], row['vat_rate'])
                if tax_key not in tax_cache:
                    tax_cache[tax_key] = self._tax(*tax_key)
            values.update(commercial_measurement=row['commercial_unit'], code_type_id=code_type.id,
                          taxes_id=[(6, 0, tax_cache[('sale', row['vat_code'], row['vat_rate'])].ids)],
                          supplier_taxes_id=[(6, 0, tax_cache[('purchase', row['vat_code'], row['vat_rate'])].ids)])
            uom = self.env.ref({'Unid': 'uom.product_uom_unit', 'cm': 'uom.product_uom_cm', 'm': 'uom.product_uom_meter'}[row['commercial_unit']])
            values['uom_id'] = uom.id
            if row['prices']:
                values['standard_price'] = row['unit_cost']
                primary = sorted(row['prices'], key=lambda p: p['level'])[0]
                values['list_price'] = (row['unit_cost'] * (1 + primary['markup'] / 100)
                                        if primary['mode'] == 'markup' else primary['fixed_price'])
                number(values['list_price'])
            if binding and digest(self._product_snapshot(product, binding.managed_fields)) != binding.target_checksum:
                raise ValidationError('Los datos fiscales, costo, precio o clasificación fueron modificados en Odoo.')
            new_names = set(values) - set(binding.managed_fields or []) if binding else set(values)
            if new_names:
                for name, default_name in [('taxes_id', 'account_sale_tax_id'), ('supplier_taxes_id', 'account_purchase_tax_id')]:
                    if name in new_names:
                        desired = set(values[name][0][2])
                        default_tax = self.env.company[default_name]
                        if set(product[name].ids) - desired - set(default_tax.ids):
                            raise ValidationError('El impuesto local requiere conciliación.')
                for name in ['cabys_code', 'cabys_description', 'commercial_measurement', 'catalog_division_id',
                             'catalog_line_id', 'catalog_subcategory_id', 'catalog_brand_id']:
                    if name not in new_names:
                        continue
                    current = product[name]
                    desired = values.get(name)
                    if current and (current.id if product._fields[name].type == 'many2one' else current) != desired:
                        raise ValidationError('Un dato fiscal o clasificación local requiere conciliación.')
                if 'list_price' in new_names and product.list_price not in (0, 1) and abs(product.list_price - values['list_price']) > .01:
                    raise ValidationError('El precio local requiere conciliación.')
                if 'standard_price' in new_names and product.standard_price and abs(product.standard_price - values['standard_price']) > .00000001:
                    raise ValidationError('El costo local requiere conciliación.')
            self._assert_cost_unit_safe(product, values)
            fingerprint = digest(row)
            status = 'unchanged' if binding and binding.source_checksum == fingerprint else 'updated' if binding else 'created'
            counts['would_' + status if dry_run and status != 'unchanged' else status] += 1
            for flag, condition in [('missing_cabys', not row['cabys']), ('unverified_cabys', row['cabys_status'] not in ('found', 'missing')),
                                    ('missing_price', not row['prices']), ('tax_corrected', row['tax_corrected'])]:
                issues[flag] += bool(condition)
            rule_plans = []
            if any(a == article and level not in {p['level'] for p in row['prices']} for a, level in rules):
                raise ValidationError('Una regla de precio desapareció del origen y requiere revisión.')
            for price in row['prices']:
                level = price['level']
                if str(level) not in maps['pricelist']:
                    raise ValidationError('Falta el nivel de precio.')
                pricelist = maps['pricelist'][str(level)]
                if pricelist and pricelist.currency_id != self.env.company.currency_id:
                    raise ValidationError('La conversión de moneda requiere un contrato adicional.')
                old_rule = rules.get((article, level))
                if old_rule and digest(self._rule_snapshot(old_rule)) != old_rule.sga_details_target_checksum:
                    raise ValidationError('La regla de precio fue modificada en Odoo.')
                price_checksum = digest(price)
                rule_status = 'unchanged' if old_rule and old_rule.sga_details_checksum == price_checksum else 'updated' if old_rule else 'created'
                price_counts['would_' + rule_status if dry_run and rule_status != 'unchanged' else rule_status] += 1
                rule_values = dict(pricelist_id=pricelist.id if pricelist else False, applied_on='1_product',
                    product_tmpl_id=product.id, min_quantity=0, compute_price=price['mode'], base='standard_price',
                    price_discount=-price['markup'] if price['mode'] == 'markup' else 0,
                    fixed_price=price['fixed_price'] if price['mode'] == 'fixed' else 0,
                    price_round=0, price_surcharge=0, price_min_margin=0, price_max_margin=0,
                    date_start=False, date_end=False,
                    sga_details_instance=instance, sga_details_article=article, sga_details_level=level,
                    sga_details_checksum=price_checksum, sga_details_source=price['source'])
                rule_plans.append((old_rule, rule_values, rule_status))
            plans.append((product, binding, values, row, fingerprint, status, rule_plans))
        if not dry_run:
            grouped = defaultdict(list)
            for product, binding, values, row, fingerprint, status, rule_plans in plans:
                if status != 'unchanged':
                    grouped[json.dumps(values, sort_keys=True)].append(product.id)
            for encoded, ids in grouped.items():
                values = json.loads(encoded)
                unit = self.env['uom.uom'].browse(values['uom_id'])
                if not unit.active:
                    unit.active = True
                Product.browse(ids).write(values)
            new_bindings = []
            for product, binding, values, row, fingerprint, status, rule_plans in plans:
                for old_rule, rule_values, rule_status in rule_plans:
                    if rule_status == 'created':
                        new_rules.append(rule_values)
                    elif rule_status == 'updated':
                        old_rule.write(rule_values)
                        old_rule.sga_details_target_checksum = digest(self._rule_snapshot(old_rule))
                if status == 'unchanged':
                    continue
                data = dict(company_id=self.env.company.id, source_instance=instance, article_id=row['article_id'],
                    product_id=product.id, source_checksum=fingerprint, managed_fields=sorted(values),
                    target_checksum=digest(self._product_snapshot(product, sorted(values))),
                    missing_cabys=not row['cabys'], unverified_cabys=row['cabys_status'] not in ('found', 'missing'),
                    missing_price=not row['prices'], tax_corrected=row['tax_corrected'], source_snapshot=row['snapshot'])
                if binding:
                    binding.write(data)
                else:
                    new_bindings.append(data)
            if new_rules:
                for rule in Rule.create(new_rules):
                    rule.sga_details_target_checksum = digest(self._rule_snapshot(rule))
            if new_bindings:
                Binding.create(new_bindings)
        return {'products': dict(counts), 'prices': dict(price_counts), 'masters': master_counts,
                'issues': dict(issues), 'last_article': max(articles, default=0), 'dry_run': dry_run}
