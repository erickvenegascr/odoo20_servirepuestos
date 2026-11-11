import copy
import hashlib
import json

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.addons.sga_gateway_catalog_details.tests.test_details import DetailsFixture


@tagged('post_install', '-at_install')
class TestArticleClassification(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = 'synthetic-article-classification'
        cls.classifier = cls.env['sga.article.classification.import']
        rows = []
        def add(kind, key, code, name, parent=None):
            rows.append(dict(kind=kind, key=key, code=code, name=name, parent=parent, currency=None))
        add('division', 'd1', 'C-D1', 'MOTOS')
        add('division', 'd2', 'C-D2', 'ALMACÉN')
        add('division', 'missing-div', 'SGA:_', 'Sin división')
        add('line', 'l1', '38', 'ABANICO', 'd1')
        add('line', 'l2', '38', 'ABANICO', 'd2')
        add('line', 'l3', '71', 'FERRETERÍA', 'd2')
        add('subcategory', 's1', '1', 'DE PISO', 'l1')
        add('subcategory', 's2', '1', 'DE PISO', 'l2')
        add('subcategory', 's3', '1', 'TORNILLOS', 'l3')
        add('brand', 'b1', 'C-YAM', 'YAMAHA')
        add('brand', 'b2', 'C-MAB', 'MABE SIN ARTÍCULOS')
        cls.env['sga.catalog.details.import']._masters(cls.instance, rows, False)
        cls.product = cls.env['product.template'].create({'name': 'PRODUCTO CLASIFICACIÓN SINTÉTICA',
            'company_id': cls.env.company.id, 'sga_catalog_instance': cls.instance, 'sga_catalog_article_id': 78001})
        cls.snapshot = {'Division': 'C-D1', 'Categoria': '38', 'SubCategoria': '1', 'Marca': 'c-yam'}
        cls.binding = cls.env['sga.catalog.detail.binding'].create({'company_id': cls.env.company.id,
            'source_instance': cls.instance, 'article_id': 78001, 'product_id': cls.product.id,
            'source_snapshot': cls.snapshot})

    def apply(self, dry=False, after=0):
        return self.classifier._backfill_batch(self.instance, after_article=after, dry_run=dry)

    def assignments(self, archived=False):
        return self.env['product.classification.assignment'].with_context(active_test=not archived).search([
            ('sga_source_instance', '=', self.instance)])

    def test_simulation_leaves_generic_module_empty(self):
        result = self.apply(dry=True)
        self.assertEqual(result['assignments'], {'would_created': 4})
        self.assertEqual(result['masters'], {'would_created': 12})
        self.assertFalse(self.assignments())
        self.assertFalse(self.env['product.classification.dimension'].search_count([('sga_source_instance', '=', self.instance)]))

    def test_complete_masters_deduplicate_context_and_preserve_unused_values(self):
        result = self.apply()
        self.assertEqual(result['masters'], {'created': 12})
        Value = self.env['product.classification.value']
        self.assertEqual(Value.search_count([('sga_source_instance', '=', self.instance)]), 8)
        self.assertEqual(Value.search_count([('dimension_id.code', '=', 'SGA_CATEGORIA'), ('code', '=', '38')]), 1)
        self.assertEqual(set(Value.search([('dimension_id.code', '=', 'SGA_SUBCATEGORIA')]).mapped('code')), {'38/1', '71/1'})
        self.assertEqual(Value.search_count([('dimension_id.code', '=', 'SGA_MARCA'), ('code', '=', 'C-MAB')]), 1)
        self.assertFalse(Value.search_count([('code', '=', 'SGA:_')]))
        self.assertEqual(len(self.product.product_variant_id.classification_assignment_ids), 4)
        self.assertEqual(self.assignments().filtered(lambda a:a.dimension_id.code=='SGA_MARCA').value_id.name, 'YAMAHA')

    def test_repetition_has_no_new_records_or_updates(self):
        self.apply();ids = self.assignments().ids
        result = self.apply()
        self.assertEqual(result['assignments'], {'unchanged': 4})
        self.assertEqual(result['masters'], {'unchanged': 12})
        self.assertEqual(self.assignments().ids, ids)

    def test_source_change_and_clear_archive_owned_assignment_only(self):
        self.apply()
        snapshot = dict(self.snapshot, Marca='C-MAB', SubCategoria=None)
        self.binding.source_snapshot = snapshot
        result = self.apply()
        self.assertEqual(result['assignments'], {'unchanged': 2, 'updated': 2})
        self.assertEqual(len(self.assignments()), 3)
        self.assertEqual(len(self.assignments(archived=True)), 4)
        self.assertEqual(self.apply()['assignments'], {'unchanged': 4})
        self.binding.source_snapshot = self.snapshot
        self.apply()
        self.assertEqual(len(self.assignments()), 4)

    def test_local_assignment_edit_and_retarget_are_not_overwritten(self):
        self.apply()
        assignment = self.assignments().filtered(lambda a:a.dimension_id.code=='SGA_MARCA')
        local = self.env['product.classification.value'].search([('dimension_id', '=', assignment.dimension_id.id), ('code', '=', 'C-MAB')])
        assignment.value_id = local
        with self.assertRaises(ValidationError):self.apply()
        self.assertEqual(assignment.value_id, local)

    def test_local_master_edit_is_protected(self):
        self.apply()
        dimension = self.env['product.classification.dimension'].search([('sga_source_instance', '=', self.instance)], limit=1)
        dimension.name = 'EDITADO LOCALMENTE'
        with self.assertRaises(ValidationError):self.apply()

    def test_local_value_edit_is_protected(self):
        self.apply()
        value = self.assignments().filtered(lambda a:a.dimension_id.code=='SGA_MARCA').value_id
        value.name = 'MARCA MODIFICADA LOCALMENTE'
        with self.assertRaises(ValidationError):self.apply()

    def test_each_variant_receives_its_own_assignments(self):
        attribute = self.env['product.attribute'].create({'name': 'Color sintético'})
        values = self.env['product.attribute.value'].create([
            {'name': 'Rojo', 'attribute_id': attribute.id}, {'name': 'Azul', 'attribute_id': attribute.id}])
        self.env['product.template.attribute.line'].create({'product_tmpl_id': self.product.id,
            'attribute_id': attribute.id, 'value_ids': [(6, 0, values.ids)]})
        self.assertEqual(len(self.product.product_variant_ids), 2)
        self.apply()
        self.assertEqual(len(self.assignments()), 8)
        for variant in self.product.product_variant_ids:
            self.assertEqual(len(variant.classification_assignment_ids), 4)

    def test_incomplete_source_evidence_does_not_clear_assignments(self):
        self.apply()
        self.binding.source_snapshot = {'unrelated': True}
        with self.assertRaises(ValidationError):self.apply()
        self.assertEqual(len(self.assignments()), 4)

    def test_other_dimensions_and_pricing_are_preserved(self):
        dim = self.env['product.classification.dimension'].create({'code': 'LOCAL', 'name': 'Clasificación local'})
        value = self.env['product.classification.value'].create({'dimension_id': dim.id, 'code': 'L1', 'name': 'Valor local'})
        local = self.env['product.classification.assignment'].create({'product_id': self.product.product_variant_id.id,
            'dimension_id': dim.id, 'value_id': value.id})
        before = self.product.read(['list_price', 'standard_price', 'cabys_code', 'taxes_id', 'uom_id'])[0]
        self.apply()
        self.assertEqual(self.product.read(['list_price', 'standard_price', 'cabys_code', 'taxes_id', 'uom_id'])[0], before)
        self.assertTrue(local.exists())
        self.assertFalse(local.sga_source_instance)

    def test_foreign_company_and_invalid_limits_rejected(self):
        other = self.env['res.company'].create({'name': 'Otra compañía sintética'})
        with self.assertRaises(ValidationError):
            self.classifier.with_company(other)._sync_products(self.instance, [(78001, self.product, self.snapshot)], True)
        for params in [{'limit': 0}, {'limit': 501}, {'after_article': -1}]:
            with self.assertRaises(ValidationError):self.classifier._backfill_batch(self.instance, **params)

    def test_archived_products_are_included_and_cursor_is_stable(self):
        self.product.active = False
        result = self.apply()
        self.assertEqual((result['products'], result['last_article']), (1,78001))
        self.assertEqual(len(self.assignments()),4)
        self.assertEqual(self.apply(after=78001)['products'],0)

    def test_company_rules_keep_values_private_to_company(self):
        self.apply()
        other = self.env['res.company'].create({'name': 'Compañía aislada sintética'})
        user = self.env['res.users'].create({'name':'Usuario de otra compañía','login':'synthetic-classification-isolation',
            'company_id':other.id,'company_ids':[(6,0,other.ids)],'group_ids':[(6,0,self.env.ref('base.group_user').ids)]})
        values = self.env['product.classification.value'].with_user(user).with_context(allowed_company_ids=other.ids)
        target_ids = self.env['product.classification.value'].search([('sga_source_instance','=',self.instance)]).ids
        self.assertFalse(values.search_count([('id','in',target_ids)]))


@tagged('post_install', '-at_install')
class TestClassificationLiveHook(DetailsFixture, TransactionCase):
    def test_first_catalog_dry_run_validates_generic_without_writing(self):
        result = self.apply(dry=True)
        self.assertEqual(result['classifications']['assignments'], {'would_created':4})
        self.assertFalse(self.env['product.classification.assignment'].search_count([('sga_source_instance','=',self.instance)]))

    def test_live_catalog_populates_generic_from_normalized_fields(self):
        result = self.apply()
        self.assertEqual(result['classifications']['assignments'], {'created':4})
        self.assertEqual(self.apply()['classifications']['assignments'], {'unchanged':4})
        self.assertEqual(len(self.product.product_variant_id.classification_assignment_ids),4)
        row=copy.deepcopy(self.row);row['brand']='brand:unused'
        self.apply([row])
        assignment=self.product.product_variant_id.classification_assignment_ids.filtered(lambda a:a.dimension_id.code=='SGA_MARCA')
        self.assertEqual(assignment.value_id.code,'TEST-B2')


@tagged('post_install', '-at_install')
class TestClassificationHttpAtomicity(DetailsFixture, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = 'sgw1_' + 'c' * 64
        cls.env['sga.gateway.application'].create({'name': 'Synthetic classification scope',
            'source_instance': cls.instance, 'token_hash': hashlib.sha256(cls.token.encode()).hexdigest()})

    def test_generic_conflict_rolls_back_fiscal_product_and_masters(self):
        self.env['product.classification.dimension'].create({'code': 'SGA_MARCA', 'name': 'Dimensión local ocupada'})
        body = {'instance': self.instance, 'masters': self.masters, 'products': [self.row], 'dry_run': False}
        response = self.url_open('/sga_gateway/api/v1/catalog/details/batch', data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.product.cabys_code)
        self.assertEqual(self.product.list_price, 1)
        self.assertFalse(self.env['sga.catalog.detail.binding'].search_count([('source_instance', '=', self.instance)]))
        self.assertFalse(self.env['product.catalog.division'].search_count([('sga_details_instance', '=', self.instance)]))
