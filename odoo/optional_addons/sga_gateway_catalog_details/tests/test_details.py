import copy
import hashlib
import json

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged


class DetailsFixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write({'country_id': cls.env.ref('base.cr').id,
                               'account_fiscal_country_id': cls.env.ref('base.cr').id})
        cls.instance = 'synthetic-commercial-sga'
        cls.importer = cls.env['sga.catalog.details.import']
        cls.masters = [
            {'kind': k, 'key': key, 'code': code, 'name': name, 'parent': parent, 'currency': currency}
            for k, key, code, name, parent, currency in [
                ('division', 'div:1', 'TEST-D1', 'División sintética', None, None),
                ('line', 'line:1', 'TEST-L1', 'Línea sintética', 'div:1', None),
                ('subcategory', 'sub:1', 'TEST-S1', 'Subcategoría sintética', 'line:1', None),
                ('brand', 'brand:1', 'TEST-B1', 'Marca sintética', None, None),
                ('brand', 'brand:unused', 'TEST-B2', 'Marca sin artículos', None, None),
                ('pricelist', '1', '1', 'Normal sintética (SGA)', None, cls.env.company.currency_id.name),
            ]]
        cls.product = cls.env['product.template'].create({
            'name': 'ARTÍCULO SINTÉTICO', 'list_price': 1, 'standard_price': 0,
            'company_id': cls.env.company.id, 'sga_catalog_instance': cls.instance,
            'sga_catalog_article_id': 88001, 'commercial_measurement': False, 'cabys_code': False})
        cls.row = {
            'article_id': 88001, 'division': 'div:1', 'line': 'line:1', 'subcategory': 'sub:1', 'brand': 'brand:1',
            'cabys': '1234567890123', 'cabys_description': 'Descripción oficial sintética', 'cabys_status': 'found',
            'commercial_unit': 'Unid', 'unit_cost': 125.5, 'vat_rate': 13, 'vat_code': '08', 'tax_corrected': False,
            'prices': [{'level': 1, 'mode': 'markup', 'markup': 23.45678912, 'fixed_price': 0, 'source': {'synthetic': True}}],
            'snapshot': {'synthetic': True},
        }
        # Hooks normally provision these in a configured Costa Rica company.
        tax_group = cls.env['account.tax.group'].create({'name': 'IVA sintético', 'company_id': cls.env.company.id})
        for usage in ['sale', 'purchase']:
            if not cls.env['account.tax'].search_count([
                    ('company_id', '=', cls.env.company.id), ('type_tax_use', '=', usage),
                    ('crfe_tax_code', '=', '01'), ('crfe_vat_rate_code', '=', '08'), ('amount', '=', 13)]):
                cls.env['account.tax'].create({'name': 'IVA sintético 13 ' + usage, 'type_tax_use': usage,
                    'company_id': cls.env.company.id, 'tax_group_id': tax_group.id, 'amount': 13, 'amount_type': 'percent',
                    'price_include_override': 'tax_excluded', 'crfe_tax_code': '01', 'crfe_vat_rate_code': '08'})

    def apply(self, rows=None, masters=None, dry=False):
        return self.importer._apply_details_batch(self.instance, self.masters if masters is None else masters,
                                                  [copy.deepcopy(self.row)] if rows is None else rows, dry)


@tagged('post_install', '-at_install')
class TestCatalogDetails(DetailsFixture, TransactionCase):
    def test_dry_run_has_no_catalog_product_or_price_writes(self):
        before = self.env['product.pricelist.item'].search_count([])
        result = self.apply(dry=True)
        self.assertEqual(result['products'], {'would_created': 1})
        self.assertEqual(self.product.list_price, 1)
        self.assertFalse(self.env['sga.catalog.detail.binding'].search_count([('source_instance', '=', self.instance)]))
        self.assertFalse(self.env['product.catalog.division'].search_count([('sga_details_instance', '=', self.instance)]))
        self.assertEqual(self.env['product.pricelist.item'].search_count([]), before)

    def test_complete_import_and_repeat_are_idempotent(self):
        self.assertEqual(self.apply()['products'], {'created': 1})
        self.assertEqual(self.product.cabys_code, self.row['cabys'])
        self.assertEqual(self.product.catalog_subcategory_id.line_id, self.product.catalog_line_id)
        self.assertEqual(self.product.catalog_line_id.division_id, self.product.catalog_division_id)
        self.assertEqual(self.product.taxes_id.amount, 13)
        self.assertEqual(self.product.taxes_id.crfe_vat_rate_code, '08')
        self.assertEqual(self.product.supplier_taxes_id.amount, 13)
        self.assertEqual(self.product.commercial_measurement, 'Unid')
        self.assertEqual(self.product.code_type_id.code, '04')
        self.assertEqual(self.product.standard_price, 125.5)
        self.assertAlmostEqual(self.product.list_price, 154.9382703456, places=6)
        self.assertEqual(self.apply()['products'], {'unchanged': 1})
        self.assertEqual(self.apply()['prices'], {'unchanged': 1})
        self.assertEqual(self.env['product.catalog.brand'].search_count([('sga_details_instance', '=', self.instance)]), 2)

    def test_margin_precision_and_native_rule_computation(self):
        self.apply()
        rule = self.env['product.pricelist.item'].search([('sga_details_instance', '=', self.instance)])
        self.assertEqual(rule.compute_price, 'markup')
        self.assertAlmostEqual(rule.price_markup, 23.45678912, places=8)
        self.assertAlmostEqual(rule.price_discount, -23.45678912, places=8)
        price = rule.pricelist_id._get_product_price(self.product.product_variant_id, 1)
        self.assertAlmostEqual(price, self.product.list_price, places=6)

    def test_fixed_price_and_centimeter_cost_are_not_pack_prices(self):
        row = copy.deepcopy(self.row)
        row.update(commercial_unit='cm', unit_cost=12.5)
        row['prices'][0].update(mode='fixed', fixed_price=25.25)
        self.apply([row])
        self.assertEqual(self.product.uom_id, self.env.ref('uom.product_uom_cm'))
        self.assertTrue(self.product.uom_id.active)
        self.assertEqual(self.product.standard_price, 12.5)
        rule = self.env['product.pricelist.item'].search([('sga_details_instance', '=', self.instance)])
        self.assertEqual(rule.compute_price, 'fixed')
        self.assertEqual(rule.fixed_price, 25.25)
        self.assertEqual(self.product.list_price, 25.25)

    def test_source_gaps_preserve_existing_cabys_and_price(self):
        self.product.write({'cabys_code': '9999999999999', 'cabys_description': 'Dato manual', 'list_price': 300})
        row = dict(self.row, cabys='', cabys_description='', cabys_status='missing', prices=[])
        self.apply([row])
        self.assertEqual(self.product.cabys_code, '9999999999999')
        self.assertEqual(self.product.cabys_description, 'Dato manual')
        self.assertEqual(self.product.list_price, 300)
        self.assertTrue(self.product.sga_detail_binding_ids.missing_cabys)
        self.assertTrue(self.product.sga_detail_binding_ids.missing_price)
        self.assertIn('SGA no tiene CABYS', self.product.sga_catalog_review_note)

    def test_local_product_edit_blocks_whole_batch(self):
        self.apply()
        self.product.cabys_code = '9999999999999'
        with self.assertRaises(ValidationError):
            self.apply()
        self.assertEqual(self.product.cabys_code, '9999999999999')

    def test_local_rule_and_master_edits_are_protected(self):
        self.apply()
        rule = self.env['product.pricelist.item'].search([('sga_details_instance', '=', self.instance)])
        rule.price_discount = -50
        with self.assertRaises(ValidationError):
            self.apply()
        self.product.catalog_brand_id.name = 'Marca editada en Odoo'
        with self.assertRaises(ValidationError):
            self.apply(rows=[])

    def test_existing_local_cabys_is_not_overwritten_on_first_import(self):
        self.product.cabys_code = '9999999999999'
        with self.assertRaises(ValidationError):
            self.apply(dry=True)

    def test_stock_move_blocks_cost_change(self):
        self.env['stock.move'].create({'product_id': self.product.product_variant_id.id, 'product_uom_qty': 1,
            'uom_id': self.product.uom_id.id, 'location_id': self.env.ref('stock.stock_location_stock').id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id})
        with self.assertRaises(ValidationError):
            self.apply(dry=True)

    def test_missing_product_and_bad_payload_rejected(self):
        with self.assertRaises(ValidationError):
            self.apply([dict(self.row, article_id=88002)], dry=True)
        for mutation in [dict(self.row, cabys='123'), dict(self.row, vat_rate=float('inf')),
                         dict(self.row, prices=[dict(self.row['prices'][0], markup=-101)])]:
            with self.assertRaises(ValidationError):
                self.apply([mutation], dry=True)

    def test_stock_user_can_review_but_cannot_read_raw_cost_or_write_binding(self):
        self.apply()
        user = self.env['res.users'].create({'name': 'Synthetic catalog reviewer', 'login': 'synthetic-details-review',
            'group_ids': [(6, 0, [self.env.ref('stock.group_stock_user').id])]})
        binding = self.product.sga_detail_binding_ids.with_user(user)
        self.assertEqual(binding.read(['article_id'])[0]['article_id'], 88001)
        with self.assertRaises(AccessError):
            binding.read(['source_snapshot'])
        with self.assertRaises(AccessError):
            binding.write({'missing_price': True})


@tagged('post_install', '-at_install')
class TestDetailsApi(DetailsFixture, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = 'sgw1_' + 'd' * 64
        cls.app = cls.env['sga.gateway.application'].create({'name': 'Synthetic details application',
            'source_instance': cls.instance, 'token_hash': hashlib.sha256(cls.token.encode()).hexdigest()})

    def post(self, body=None, token=None, path='batch'):
        return self.url_open('/sga_gateway/api/v1/catalog/details/' + path,
            data=json.dumps(body or {}).encode(), headers={'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + (self.token if token is None else token)})

    def body(self, **extra):
        return dict(instance=self.instance, masters=self.masters, products=[self.row], dry_run=False, **extra)

    def test_application_identity_and_scope(self):
        response = self.post(path='info')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['details_contract'], 1)
        self.assertEqual(response.json()['source_instance'], self.instance)
        self.assertFalse(response.json()['sga_writes_enabled'])
        self.assertEqual(self.post(path='info', token='personal-api-key').status_code, 401)
        self.app.catalog_enabled = False
        self.assertEqual(self.post(self.body()).status_code, 403)

    def test_wrong_source_company_override_and_unknown_fields_rejected(self):
        body = self.body(); body['instance'] = 'other'
        self.assertEqual(self.post(body).status_code, 403)
        self.assertEqual(self.post(self.body(company_id=99)).status_code, 403)
        self.assertEqual(self.post(self.body(context={'allowed_company_ids': [99]})).status_code, 403)

    def test_conflict_rolls_back_masters_and_full_retry_is_safe(self):
        body = self.body(); body['products'][0] = dict(self.row, article_id=999999)
        self.assertEqual(self.post(body).status_code, 409)
        self.assertFalse(self.env['product.catalog.division'].search_count([('sga_details_instance', '=', self.instance)]))
        self.assertEqual(self.post(self.body()).json()['products'], {'created': 1})
        self.assertEqual(self.post(self.body()).json()['products'], {'unchanged': 1})
