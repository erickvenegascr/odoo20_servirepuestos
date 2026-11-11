import hashlib
import json
from collections import Counter

from odoo import fields, models
from odoo.exceptions import ValidationError
from odoo.addons.sga_gateway_catalog_details.models.catalog import digest


DIMENSIONS = {
    'division': ('SGA_DIVISION', 'División', 10),
    'category': ('SGA_CATEGORIA', 'Categoría', 20),
    'subcategory': ('SGA_SUBCATEGORIA', 'Subcategoría', 30),
    'brand': ('SGA_MARCA', 'Marca', 40),
}
NATIVE_MASTERS = {
    'division': 'product.catalog.division', 'line': 'product.catalog.line',
    'subcategory': 'product.catalog.subcategory', 'brand': 'product.catalog.brand',
}


def key(*parts):
    return json.dumps(list(parts), separators=(',', ':'), ensure_ascii=False)


def code(value):
    return str(value).strip() if value is not None and str(value).strip() else None


class SourceMixin(models.AbstractModel):
    _name = 'sga.classification.source.mixin'
    _description = 'Procedencia de las clasificaciones SGA'

    sga_source_instance = fields.Char(readonly=True, copy=False, index=True, groups='base.group_system')
    sga_source_key = fields.Char(readonly=True, copy=False, index=True, groups='base.group_system')
    sga_source_hash = fields.Char(readonly=True, copy=False, groups='base.group_system')
    sga_target_hash = fields.Char(readonly=True, copy=False, groups='base.group_system')
    _sga_identity_unique = models.Constraint(
        'UNIQUE(company_id, sga_source_instance, sga_source_key)', 'La clasificación SGA ya está vinculada.')


class Dimension(models.Model):
    _name = 'product.classification.dimension'
    _inherit = ['product.classification.dimension', 'sga.classification.source.mixin']


class Value(models.Model):
    _name = 'product.classification.value'
    _inherit = ['product.classification.value', 'sga.classification.source.mixin']


class Assignment(models.Model):
    _name = 'product.classification.assignment'
    _inherit = ['product.classification.assignment', 'sga.classification.source.mixin']

    active = fields.Boolean(default=True)


class ClassificationImport(models.AbstractModel):
    _name = 'sga.article.classification.import'
    _description = 'Integración SGA con clasificaciones de artículos'

    def _lock(self, instance):
        if not isinstance(instance, str) or not 1 <= len(instance) <= 80:
            raise ValidationError('Origen de clasificaciones inválido.')
        lock = int.from_bytes(hashlib.sha256(
            f'details:{self.env.company.id}:{instance}'.encode()).digest()[:8], 'big', signed=True)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])

    def _snapshot(self, record):
        names = (['product_id', 'dimension_id', 'value_id', 'active']
                 if record._name == 'product.classification.assignment' else
                 ['dimension_id', 'code', 'name', 'active']
                 if record._name == 'product.classification.value' else
                 ['company_id', 'code', 'name', 'active'])
        return {n: record[n].id if record._fields[n].type == 'many2one' else record[n] for n in names}

    def _check_owned(self, record):
        if record.sga_source_instance and (
                not record.sga_target_hash or digest(self._snapshot(record)) != record.sga_target_hash):
            raise ValidationError('Una clasificación SGA fue modificada en Odoo; requiere conciliación.')

    def _native_specs(self, instance, incoming=None):
        native = {}
        for kind, model in NATIVE_MASTERS.items():
            records = self.env[model].sudo().with_context(active_test=False).search([
                ('sga_details_instance', '=', instance)])
            for record in records:
                parent = record.division_id.sga_details_key if kind == 'line' else record.line_id.sga_details_key if kind == 'subcategory' else None
                current = {'name': record.name, 'code': record.code, 'parent': parent, 'active': record.active}
                if not record.sga_details_checksum or digest(current) != record.sga_details_checksum:
                    raise ValidationError('El catálogo SGA de origen fue modificado localmente.')
                native[(kind, record.sga_details_key)] = dict(current, kind=kind)
        for row in incoming or []:
            if row['kind'] in NATIVE_MASTERS:
                native[(row['kind'], row['key'])] = {
                    n: row[n] for n in ['kind', 'code', 'name', 'parent']}
        return native

    def _native_values(self, instance, incoming=None):
        native = self._native_specs(instance, incoming)
        rows = {};brand_codes = {}
        for (kind, _native_key), record in native.items():
            value_code = record['code']
            if kind == 'division':
                if value_code == 'SGA:_':
                    continue
                target_kind = 'division';source_key = key(target_kind, value_code)
            elif kind == 'line':
                target_kind = 'category';source_key = key(target_kind, value_code)
            elif kind == 'subcategory':
                parent = native.get(('line', record['parent']))
                if not parent:
                    raise ValidationError('Falta la categoría padre de la subcategoría SGA.')
                target_kind = 'subcategory';source_key = key(target_kind, parent['code'], value_code)
                value_code = parent['code'] + '/' + value_code
            else:
                target_kind = 'brand';source_key = key(target_kind, value_code)
                folded = value_code.casefold()
                if folded in brand_codes and brand_codes[folded] != value_code:
                    raise ValidationError('El código de marca es ambiguo.')
                brand_codes[folded] = value_code
            row = {'kind': target_kind, 'code': value_code, 'name': record['name']}
            if source_key in rows and rows[source_key] != row:
                raise ValidationError('El mismo código SGA tiene nombres incompatibles entre divisiones.')
            rows[source_key] = row
        if not rows:
            raise ValidationError('Primero debe importarse el catálogo comercial SGA.')
        return rows, brand_codes

    def _normalized_snapshots(self, instance, masters, rows):
        native = self._native_specs(instance, masters)
        snapshots = {}
        for row in rows:
            snapshot = {}
            for kind, field, label in [('division', 'division', 'Division'), ('line', 'line', 'Categoria'),
                                       ('subcategory', 'subcategory', 'SubCategoria'), ('brand', 'brand', 'Marca')]:
                record = native.get((kind, row[field])) if row[field] else None
                if row[field] and not record:
                    raise ValidationError('La clasificación normalizada no existe en el catálogo de origen.')
                snapshot[label] = record['code'] if record and record['code'] != 'SGA:_' else None
            snapshots[row['article_id']] = snapshot
        return snapshots

    def _upsert_master(self, model, instance, source_key, values, existing, occupied, dry_run, counts):
        record = existing.get(source_key)
        if record:
            self._check_owned(record)
        elif occupied:
            if (len(occupied) != 1 or occupied.sga_source_instance or not occupied.active
                    or occupied.code != values['code'] or occupied.name != values['name']):
                raise ValidationError('Un código de clasificación local ya está ocupado.')
            record = occupied
        fingerprint = digest({k: v for k, v in values.items() if k != 'sequence'})
        status = 'unchanged' if record and record.sga_source_hash == fingerprint else 'updated' if record else 'created'
        counts[('would_' + status) if dry_run and status != 'unchanged' else status] += 1
        if not dry_run and status != 'unchanged':
            data = {**values, 'sga_source_instance': instance, 'sga_source_key': source_key,
                    'sga_source_hash': fingerprint, 'sga_target_hash': fingerprint}
            if record:
                data.pop('sequence', None)
                record.write(data)
            else:
                record = model.create(data)
        return record

    def _masters(self, instance, dry_run, incoming=None):
        rows, brand_codes = self._native_values(instance, incoming)
        Dimension = self.env['product.classification.dimension'].sudo().with_context(active_test=False)
        Value = self.env['product.classification.value'].sudo().with_context(active_test=False)
        company = self.env.company.id
        owned_dimensions = {r.sga_source_key: r for r in Dimension.search([
            ('company_id', '=', company), ('sga_source_instance', '=', instance)])}
        occupied_dimensions = {r.code: r for r in Dimension.search([
            ('company_id', '=', company), ('code', 'in', [v[0] for v in DIMENSIONS.values()])])}
        owned_values = {r.sga_source_key: r for r in Value.search([
            ('company_id', '=', company), ('sga_source_instance', '=', instance)])}
        dims = {};values = {};counts = Counter()
        for kind, (dimension_code, label, sequence) in DIMENSIONS.items():
            dims[kind] = self._upsert_master(Dimension, instance, kind,
                {'company_id': company, 'code': dimension_code, 'name': label, 'sequence': sequence, 'active': True},
                owned_dimensions, occupied_dimensions.get(dimension_code), dry_run, counts)
        ids = [r.id for r in dims.values() if r]
        occupied_values = {(r.dimension_id.id, r.code): r for r in Value.search([('dimension_id', 'in', ids)])}
        for source_key, row in rows.items():
            dimension = dims[row['kind']]
            dimension_id = dimension.id if dimension else False
            values[source_key] = self._upsert_master(Value, instance, source_key,
                {'dimension_id': dimension_id, 'code': row['code'], 'name': row['name'], 'active': True},
                owned_values, occupied_values.get((dimension_id, row['code'])), dry_run, counts)
        return dims, values, brand_codes, dict(counts)

    def _expected(self, snapshot, brand_codes):
        if not isinstance(snapshot, dict) or not {'Division', 'Categoria', 'SubCategoria', 'Marca'} <= set(snapshot):
            raise ValidationError('Falta la evidencia de clasificación original SGA.')
        division, category, sub = [code(snapshot.get(n)) for n in ['Division', 'Categoria', 'SubCategoria']]
        brand = code(snapshot.get('CatalogMarca') or snapshot.get('Marca'))
        if brand:
            brand = brand_codes.get(brand.casefold())
            if not brand:
                raise ValidationError('La marca del artículo no existe en el catálogo SGA.')
        if sub and not category:
            raise ValidationError('La subcategoría del artículo no tiene categoría.')
        return {'division': key('division', division) if division else None,
                'category': key('category', category) if category else None,
                'subcategory': key('subcategory', category, sub) if sub else None,
                'brand': key('brand', brand) if brand else None}

    def _sync_products(self, instance, items, dry_run=True, incoming=None):
        if type(dry_run) is not bool or len(items) > 500:
            raise ValidationError('Lote de clasificaciones fuera de límite.')
        self._lock(instance)
        if any(product.company_id != self.env.company or product.sga_catalog_instance != instance
               or product.sga_catalog_article_id != article for article, product, snapshot in items):
            raise ValidationError('El lote pertenece a otro origen o compañía.')
        dims, values, brand_codes, master_counts = self._masters(instance, dry_run, incoming)
        Assignment = self.env['product.classification.assignment'].sudo().with_context(active_test=False)
        entries = []
        for article, product, snapshot in items:
            expected = self._expected(snapshot, brand_codes)
            for variant in product.with_context(active_test=False).product_variant_ids:
                for kind, source_value in expected.items():
                    if source_value is not None and source_value not in values:
                        raise ValidationError('Falta una clasificación original del artículo.')
                    source_key = key(article, variant.id, kind)
                    entries.append((source_key, variant, kind, source_value))
        owned = {r.sga_source_key: r for r in Assignment.search([
            ('company_id', '=', self.env.company.id), ('sga_source_instance', '=', instance),
            ('sga_source_key', 'in', [e[0] for e in entries])])}
        occupied = {(r.product_id.id, r.dimension_id.id): r for r in Assignment.search([
            ('product_id', 'in', list({e[1].id for e in entries})),
            ('dimension_id', 'in', [d.id for d in dims.values() if d])])}
        plans = [];counts = Counter()
        for source_key, variant, kind, source_value in entries:
            dimension = dims[kind];value = values.get(source_value)
            old = owned.get(source_key)
            local = occupied.get((variant.id, dimension.id if dimension else False))
            if old:
                self._check_owned(old)
            elif local:
                if source_value is None and not local.sga_source_instance:
                    continue
                if (local.sga_source_instance or not local.active or not value or local.value_id != value):
                    raise ValidationError('Una asignación local requiere conciliación antes de importar SGA.')
                old = local
            if source_value is None and not old:
                continue
            fingerprint = digest({'kind': kind, 'value': source_value})
            status = 'unchanged' if old and old.sga_source_hash == fingerprint else 'updated' if old else 'created'
            counts[('would_' + status) if dry_run and status != 'unchanged' else status] += 1
            if status == 'unchanged':
                continue
            data = {'product_id': variant.id, 'dimension_id': dimension.id if dimension else False,
                    'value_id': value.id if value else old.value_id.id if old else False,
                    'active': source_value is not None, 'sga_source_instance': instance,
                    'sga_source_key': source_key, 'sga_source_hash': fingerprint}
            data['sga_target_hash'] = digest({n: data[n] for n in ['product_id', 'dimension_id', 'value_id', 'active']})
            plans.append((old, data))
        if not dry_run:
            create = []
            for old, data in plans:
                if old:
                    old.write(data)
                else:
                    create.append(data)
            if create:
                Assignment.create(create)
        return {'products': len(items), 'assignments': dict(counts), 'masters': master_counts,
                'last_article': max((item[0] for item in items), default=0), 'dry_run': dry_run}

    def _backfill_batch(self, instance, after_article=0, limit=500, dry_run=True):
        if type(after_article) is not int or after_article < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValidationError('Cursor o límite de clasificaciones inválido.')
        bindings = self.env['sga.catalog.detail.binding'].sudo().search([
            ('company_id', '=', self.env.company.id), ('source_instance', '=', instance),
            ('article_id', '>', after_article)], order='article_id', limit=limit)
        return self._sync_products(instance, [(b.article_id, b.product_id, b.source_snapshot) for b in bindings], dry_run)


class CatalogDetailsImport(models.AbstractModel):
    _inherit = 'sga.catalog.details.import'

    def _apply_details_batch(self, instance, masters, rows, dry_run=True):
        result = super()._apply_details_batch(instance, masters, rows, dry_run)
        products = self.env['product.template'].sudo().with_context(active_test=False).search([
            ('company_id', '=', self.env.company.id), ('sga_catalog_instance', '=', instance),
            ('sga_catalog_article_id', 'in', [r['article_id'] for r in rows])])
        by_article = {p.sga_catalog_article_id: p for p in products}
        classifier = self.env['sga.article.classification.import']
        snapshots = classifier._normalized_snapshots(instance, masters if dry_run else None, rows)
        items = [(r['article_id'], by_article[r['article_id']], snapshots[r['article_id']]) for r in rows]
        result['classifications'] = classifier._sync_products(
            instance, items, dry_run, incoming=masters if dry_run else None)
        return result
