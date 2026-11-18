from odoo import Command
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class SupplierContractCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = 'supplier-test'
        cls.service = cls.env['sga.supplier.import']
        cls.product = cls.env['product.template'].create({'name': 'Supplier relation test', 'company_id': cls.env.company.id,
            'sga_catalog_instance': cls.instance, 'sga_catalog_article_id': 8, 'is_storable': True})
        cls.vendors = [{'code': '019', 'name': 'Supplier zero', 'tax_id': '', 'active': True},
                       {'code': '074', 'name': 'Supplier one', 'tax_id': '', 'active': True}]

    def relation(self, code='019', priority=0, **values):
        return dict({'article_id': 8, 'vendor_code': code, 'product_code': '000008',
                     'priority': priority, 'disabled': False, 'vendor_active': True}, **values)

    def apply(self, rows, vendors=None, dry=False):
        return self.service._apply(self.instance, self.vendors if vendors is None else vendors, rows, dry)

    def rows(self):
        return self.product.seller_ids.sorted('sequence')

    def test_dry_run_and_leading_zeros(self):
        count = self.env['res.partner'].search_count([])
        result = self.apply([self.relation()], dry=True)
        self.assertEqual(result['relations']['created'], 1)
        self.assertEqual(self.env['res.partner'].search_count([]), count)
        self.assertFalse(self.rows())
        self.apply([self.relation()])
        row = self.rows()
        self.assertEqual((row.sga_vendor_code, row.product_code, row.sequence), ('019', '000008', 0))
        self.assertTrue(row.sga_price_pending)
        self.assertFalse(self.product.product_variant_id._select_seller(quantity=1))
        again = self.apply([self.relation()])
        self.assertEqual(again['relations']['unchanged'], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_priority_before_price_and_disabled(self):
        self.apply([self.relation(priority=1), self.relation('074', 2)])
        first, second = self.rows()
        first.price = 100; second.price = 1
        variant = self.product.product_variant_id
        self.assertEqual(variant._select_seller(quantity=1)['supplierinfo'], first)
        self.apply([self.relation(priority=1, disabled=True)], vendors=[])
        self.assertEqual(variant._select_seller(quantity=1)['supplierinfo'], second)
        self.apply([self.relation('074', 2, vendor_active=False)], vendors=[])
        self.assertFalse(variant._select_seller(quantity=1))

    def test_priority_ties_are_not_renumbered(self):
        self.apply([self.relation(), self.relation('074')])
        self.assertEqual(self.rows().mapped('sequence'), [0, 0])

    def test_existing_quote_is_preserved(self):
        partner = self.env['res.partner'].create({'name': 'Known vendor', 'vat': '123456789'})
        existing = self.env['product.supplierinfo'].create({'partner_id': partner.id, 'product_tmpl_id': self.product.id,
            'price': 321.1234, 'discount': 4, 'min_qty': 7, 'delay': 9, 'product_code': 'old-code', 'sequence': 99})
        vendors = [dict(self.vendors[0], tax_id='123-456-789')]
        result = self.apply([self.relation()], vendors=vendors)
        self.assertEqual(result['relations']['adopted'], 1)
        self.assertEqual(self.rows(), existing)
        self.assertEqual((existing.price, existing.discount, existing.min_qty, existing.delay), (321.1234, 4, 7, 9))
        self.assertEqual(existing.product_code, '000008')
        self.assertEqual(existing.sga_previous_values['product_code'], 'old-code')

    def test_manual_code_edit_conflicts_without_overwrite(self):
        self.apply([self.relation()])
        self.rows().product_code = 'manual'
        with self.assertRaises(ValidationError):
            self.apply([self.relation()])
        self.assertEqual(self.rows().product_code, 'manual')

    def test_reject_cross_company_and_malformed_rows(self):
        other = self.env['res.company'].create({'name': 'Other supplier company'})
        with self.assertRaises(ValidationError):
            self.service.with_company(other)._apply(self.instance, self.vendors, [self.relation()], True)
        for bad in [self.relation(priority=True), self.relation(priority=-1), self.relation(product_code=8),
                    dict(self.relation(), price=100)]:
            with self.assertRaises(ValidationError):
                self.apply([bad], dry=True)

    def test_ambiguous_supplier_tariffs_rejected(self):
        partner = self.env['res.partner'].create({'name': 'Known vendor', 'vat': '123456789'})
        for code in ['A', 'B']:
            self.env['product.supplierinfo'].create({'partner_id': partner.id, 'product_tmpl_id': self.product.id,
                                                     'price': 10, 'product_code': code})
        with self.assertRaises(ValidationError):
            self.apply([self.relation()], vendors=[dict(self.vendors[0], tax_id='123456789')], dry=True)

    def test_source_disabled_links_visible_but_not_selected(self):
        self.apply([self.relation(disabled=True)])
        self.rows().price = 100
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.product.product_variant_id._select_seller(partner_id=self.rows().partner_id, quantity=1))


from odoo.tests import HttpCase
import hashlib
import json


@tagged('post_install', '-at_install')
class SupplierHttpCase(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = 'sgw1_' + 's' * 64
        cls.app = cls.env['sga.gateway.application'].create({'name': 'Synthetic supplier HTTP',
            'source_instance': 'suppliers-http', 'token_hash': hashlib.sha256(cls.token.encode()).hexdigest()})

    def post(self, path, body, token=None):
        return self.url_open('/sga_gateway/api/v1/catalog/suppliers/' + path,
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + (self.token if token is None else token)})

    def test_auth_scope_and_fixed_fields(self):
        self.assertEqual(self.post('info', {}, token='bad').status_code, 401)
        result = self.post('info', {})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['suppliers_contract'], 1)
        self.assertFalse(result.json()['sga_writes_enabled'])
        self.assertEqual(self.post('batch', {'instance': 'other', 'vendors': [], 'relations': [], 'dry_run': False}).status_code, 403)
        self.assertEqual(self.post('batch', {'instance': self.app.source_instance, 'vendors': [], 'relations': [],
                                          'dry_run': False, 'model': 'res.users'}).status_code, 403)

    def test_failed_relation_rolls_back_vendor_creation(self):
        before = self.env['res.partner'].search_count([])
        response = self.post('batch', {'instance': self.app.source_instance,
            'vendors': [{'code': '019', 'name': 'Synthetic pending vendor', 'tax_id': '', 'active': True}],
            'relations': [{'article_id': 999999, 'vendor_code': '019', 'product_code': '000008',
                           'priority': 0, 'disabled': False, 'vendor_active': True}], 'dry_run': False})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.env['res.partner'].search_count([]), before)
        self.assertFalse(self.env['sga.supplier.binding'].search([('source_instance', '=', self.app.source_instance)]))
