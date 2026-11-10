import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


def checksum(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


class CatalogCategory(models.Model):
    _inherit = 'product.category'

    sga_catalog_instance = fields.Char(readonly=True, copy=False, index=True)
    sga_catalog_key = fields.Char(readonly=True, copy=False, index=True)
    sga_catalog_company_id = fields.Many2one('res.company', readonly=True, copy=False, index=True)
    sga_catalog_values = fields.Json(readonly=True, copy=False)
    _sga_catalog_identity = models.Constraint(
        'UNIQUE(sga_catalog_company_id, sga_catalog_instance, sga_catalog_key)',
        'The SGA category identity already exists.')


class CatalogProduct(models.Model):
    _inherit = 'product.template'

    sga_catalog_instance = fields.Char(readonly=True, copy=False, index=True)
    sga_catalog_article_id = fields.Integer(readonly=True, copy=False, index=True)
    sga_catalog_checksum = fields.Char(readonly=True, copy=False)
    sga_catalog_values = fields.Json(readonly=True, copy=False)
    sga_catalog_details = fields.Json(readonly=True, copy=False)
    _sga_catalog_identity = models.Constraint(
        'UNIQUE(company_id, sga_catalog_instance, sga_catalog_article_id)',
        'The SGA article identity already exists.')


class CatalogImport(models.AbstractModel):
    _name = 'sga.catalog.import'
    _description = 'SGA catalog import contract'

    def _authorize(self):
        if not (self.env.user.has_group('base.group_system')
                or self.env.user.has_group('sga_gateway_connector.group_catalog_import')):
            raise AccessError('Catalog synchronization requires the SGA catalog permission.')

    @api.model
    def catalog_info(self):
        self._authorize()
        return {'contract': 1, 'company_id': self.env.company.id,
                'database': self.env.cr.dbname, 'sga_writes_enabled': False}

    def _unchanged_locally(self, record, values):
        for name, expected in (record.sga_catalog_values or {}).items():
            value = record[name]
            if record._fields[name].type == 'many2one':
                value = value.id or False
            if value != expected:
                raise ValidationError(f'SGA catalog conflict: {record._name} {record.id}, field {name}. '
                                      'Resolve the Odoo edit before importing again.')

    @api.model
    def apply_catalog_batch(self, instance, categories, products, dry_run=True):
        self._authorize()
        return self._apply_catalog_batch(instance, categories, products, dry_run=dry_run)

    def _apply_catalog_batch(self, instance, categories, products, dry_run=True):
        """Private entry point for the authenticated application controller."""
        if (not isinstance(instance, str) or not 1 <= len(instance) <= 80
                or not isinstance(categories, list) or not isinstance(products, list)
                or len(categories) + len(products) > 200 or type(dry_run) is not bool):
            raise ValidationError('Invalid catalog batch.')
        company = self.env.company
        # All batches for one company/source serialize, including retries after a lost response.
        lock = int.from_bytes(hashlib.sha256(f'{company.id}:{instance}'.encode()).digest()[:8], 'big', signed=True)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])
        # The service group has no generic ORM write rights. Elevation is confined
        # to this validated, company-scoped contract and its fixed field whitelist.
        Category = self.env['product.category'].sudo().with_context(active_test=False, lang=False)
        Product = self.env['product.template'].sudo().with_context(active_test=False, lang=False)
        domain = [('sga_catalog_instance', '=', instance), ('sga_catalog_company_id', '=', company.id)]
        category_map = {r.sga_catalog_key: r for r in Category.search(domain)}
        seen = set()
        for node in categories:
            key, name, parent = node.get('key'), node.get('name'), node.get('parent')
            if (not isinstance(key, str) or not key or len(key) > 255 or key in seen
                    or not isinstance(name, str) or not name.strip() or len(name) > 255
                    or (parent is not None and not isinstance(parent, str)) or parent == key
                    or (key == 'root') != (parent is None)):
                raise ValidationError('Invalid or duplicate category identity/name/parent.')
            seen.add(key)
            parent_record = category_map.get(parent)
            if parent and not parent_record and not dry_run:
                raise ValidationError(f'SGA category parent has not been imported: {parent}')
            values = {'name': name, 'parent_id': parent_record.id if parent_record else False}
            record = category_map.get(key)
            if record:
                self._unchanged_locally(record, values)
            if not dry_run:
                if record:
                    if record.sga_catalog_values != values:
                        record.write({**values, 'sga_catalog_values': values})
                else:
                    record = Category.create({**values, 'sga_catalog_instance': instance,
                                              'sga_catalog_company_id': company.id,
                                              'sga_catalog_key': key, 'sga_catalog_values': values})
                category_map[key] = record

        seen = set()
        counts = {'categories': len(categories), 'products': len(products),
                  'created': 0, 'updated': 0, 'unchanged': 0, 'dry_run': dry_run}
        for row in products:
            article = row.get('article_id')
            if (type(article) is not int or article <= 0 or article in seen
                    or not isinstance(row.get('name'), str) or not row['name'].strip()
                    or len(row['name']) > 255 or not isinstance(row.get('default_code'), str)
                    or len(row['default_code']) > 255 or type(row.get('active')) is not bool
                    or type(row.get('service')) is not bool or type(row.get('units_per_pack')) is not int
                    or row['units_per_pack'] < 1 or not isinstance(row.get('category_key'), str)):
                raise ValidationError('Invalid or duplicate article identity/values.')
            seen.add(article)
            category = category_map.get(row['category_key'])
            if not category and not dry_run:
                raise ValidationError(f'Article {article} has an unknown category.')
            records = Product.search([('sga_catalog_instance', '=', instance),
                                      ('sga_catalog_article_id', '=', article),
                                      ('company_id', '=', company.id)], limit=2)
            if len(records) > 1:
                raise ValidationError('Duplicate SGA article binding.')
            values = {'name': row['name'], 'default_code': row['default_code'],
                      'type': 'service' if row['service'] else 'consu',
                      'is_storable': not row['service'], 'active': row['active']}
            if hasattr(Product, '_product_uppercase_name'):
                values['name'] = Product._product_uppercase_name(values['name'])
            if category:
                values['categ_id'] = category.id
            # Optional installed fields are descriptive; no pricing/fiscal writes in phase one.
            if 'search_keywords' in Product._fields:
                values['search_keywords'] = row.get('keywords') or False
            digest = checksum(row)
            if records:
                self._unchanged_locally(records, values)
            status = ('created' if not records else
                      'unchanged' if records.sga_catalog_checksum == digest else 'updated')
            counts[status] += 1
            if not dry_run and status != 'unchanged':
                tracked = {**values, 'sga_catalog_instance': instance, 'sga_catalog_article_id': article,
                           'sga_catalog_checksum': digest, 'sga_catalog_values': values,
                           'sga_catalog_details': row}
                if records:
                    records.write(tracked)
                else:
                    Product.create({**tracked, 'company_id': company.id})
        return counts
