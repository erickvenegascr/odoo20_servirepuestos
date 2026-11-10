from odoo.tests import TransactionCase, tagged
from odoo.exceptions import AccessError, ValidationError


@tagged('post_install', '-at_install')
class TestCatalog(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env['sga.catalog.import']
        cls.nodes = [{'key': 'root', 'name': 'SGA', 'parent': None},
                     {'key': 'category:1', 'name': 'MOTOS', 'parent': 'root'}]
        cls.article = {'article_id': 101, 'name': 'MOTOR', 'default_code': 'M-101',
                       'service': False, 'active': True, 'category_key': 'category:1',
                       'units_per_pack': 12, 'keywords': 'MOTOR', 'cabys': '1234567890123'}

    def apply(self, categories=None, products=None, dry_run=False, **kwargs):
        return self.importer.apply_catalog_batch(
            instance='synthetic-sga', categories=categories or [], products=products or [],
            dry_run=dry_run, **kwargs)

    def product(self):
        return self.env['product.template'].with_context(active_test=False).search([
            ('sga_catalog_instance', '=', 'synthetic-sga'), ('sga_catalog_article_id', '=', 101)])

    def test_simulation_creates_nothing(self):
        before = self.env['product.category'].search_count([])
        self.apply(self.nodes, [self.article], dry_run=True)
        self.assertFalse(self.product())
        self.assertEqual(self.env['product.category'].search_count([]), before)

    def test_idempotent_create_update_archive_and_restore(self):
        result = self.apply(self.nodes, [self.article])
        self.assertEqual(result['created'], 1)
        product = self.product()
        self.assertEqual(product.categ_id.parent_id.name, 'SGA')
        self.assertEqual(product.company_id, self.env.company)
        self.assertEqual(self.apply(self.nodes, [self.article])['unchanged'], 1)
        self.assertEqual(self.product(), product)
        archived = dict(self.article, active=False, name='MOTOR NUEVO')
        self.assertEqual(self.apply(products=[archived])['updated'], 1)
        self.assertFalse(product.active)
        self.apply(products=[self.article])
        self.assertTrue(product.active)

    def test_lost_ack_retry_does_not_duplicate(self):
        self.apply(self.nodes, [self.article])
        first = self.product().id
        self.apply(self.nodes, [self.article])
        self.assertEqual(self.product().ids, [first])

    def test_local_product_edit_blocks_source_overwrite(self):
        self.apply(self.nodes, [self.article])
        self.product().name = 'EDITADO EN ODOO'
        with self.assertRaises(ValidationError):
            self.apply(products=[dict(self.article, name='EDITADO EN SGA')])
        self.assertEqual(self.product().name, 'EDITADO EN ODOO')

    def test_local_category_edit_blocks_source_overwrite(self):
        self.apply(self.nodes)
        category = self.env['product.category'].search([('sga_catalog_key', '=', 'category:1')])
        category.name = 'EDITADO EN ODOO'
        with self.assertRaises(ValidationError):
            self.apply(self.nodes)

    def test_same_sku_is_not_an_identity(self):
        local = self.env['product.template'].create({'name': 'LOCAL', 'default_code': 'M-101'})
        self.apply(self.nodes, [self.article])
        self.assertNotEqual(self.product(), local)
        self.assertEqual(local.name, 'LOCAL')

    def test_unknown_parent_or_category_rejected(self):
        with self.assertRaises(ValidationError):
            self.apply([self.nodes[1]])
        with self.assertRaises(ValidationError):
            self.apply(products=[self.article])

    def test_bad_article_rolls_back_entire_batch(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.apply(self.nodes, [self.article, dict(self.article, article_id=-1)])
        self.assertFalse(self.product())
        self.assertFalse(self.env['product.category'].search([('sga_catalog_instance', '=', 'synthetic-sga')]))

    def test_company_scope(self):
        self.apply(self.nodes, [self.article])
        other = self.env['res.company'].create({'name': 'Synthetic other company'})
        self.importer.with_company(other).apply_catalog_batch(
            instance='synthetic-sga', categories=self.nodes, products=[self.article], dry_run=False)
        self.assertEqual(len(self.product()), 2)

    def test_ordinary_user_cannot_import(self):
        user = self.env['res.users'].create({'name': 'Synthetic catalog reader', 'login': 'synthetic-reader'})
        with self.assertRaises(AccessError):
            self.importer.with_user(user).catalog_info()
        with self.assertRaises(AccessError):
            self.importer.with_user(user).apply_catalog_batch('synthetic', [], [])

    def test_service_group_imports_without_general_product_write_access(self):
        group = self.env.ref('sga_gateway_connector.group_catalog_import')
        user = self.env['res.users'].create({'name': 'Synthetic SGA service', 'login': 'synthetic-sga-service',
                                            'group_ids': [(6, 0, group.ids)]})
        self.assertTrue(user.share)
        service = self.importer.with_user(user)
        result = service.apply_catalog_batch('synthetic-sga', self.nodes, [self.article], dry_run=False)
        self.assertEqual(result['created'], 1)
        with self.assertRaises(AccessError):
            self.product().with_user(user).write({'list_price': 999})
        with self.assertRaises(AccessError):
            self.env['stock.quant'].with_user(user).create({'quantity': 100})

    def test_financial_values_preserved(self):
        self.apply(self.nodes, [self.article])
        product = self.product()
        product.write({'list_price': 150, 'standard_price': 90})
        tax_ids = product.taxes_id.ids if 'taxes_id' in product._fields else None
        self.apply(products=[dict(self.article, name='OTRO NOMBRE')])
        self.assertEqual(product.list_price, 150)
        self.assertEqual(product.standard_price, 90)
        if tax_ids is not None:
            self.assertEqual(product.taxes_id.ids, tax_ids)
