from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.article_codes import article_code


@tagged('post_install','-at_install')
class TestArticleCodeMask(TransactionCase):
    def product(self,article=123,**values):
        return self.env['product.template'].with_context(sga_card_internal=True).create({
            'name':'MASK LAB','company_id':self.env.company.id,'sga_catalog_instance':'mask-lab',
            'sga_catalog_article_id':article,**values})

    def test_identity_is_six_digits_and_commercial_reference_is_separate(self):
        product=self.product(default_code='COMMERCIAL')
        self.assertEqual('000123',product.default_code)
        self.assertEqual('COMMERCIAL',product.sga_commercial_reference)
        self.assertEqual('1000000',article_code(1000000))
        with self.assertRaises(ValidationError):product.with_context(sga_card_internal=False).default_code='999999'
        with self.assertRaises(ValidationError):product.product_variant_id.with_context(sga_card_internal=False).default_code='999999'

    def test_normalization_preserves_identity_finance_and_import_tracking(self):
        product=self.product(default_code='COMMERCIAL',list_price=130,standard_price=100,
            sga_catalog_values={'name':'MASK LAB','default_code':'COMMERCIAL'})
        product.with_context(sga_card_internal=True).write({'default_code':'COMMERCIAL','sga_commercial_reference':False})
        ids=(product.id,product.product_variant_id.id)
        dry=self.env['product.template']._normalize_sga_article_codes(after_id=product.id-1,limit=1,dry_run=True)
        self.assertEqual(1,dry['changed']);self.assertEqual('COMMERCIAL',product.default_code)
        self.env['product.template']._normalize_sga_article_codes(after_id=product.id-1,limit=1,dry_run=False)
        self.assertEqual(ids,(product.id,product.product_variant_id.id))
        self.assertEqual('000123',product.default_code)
        self.assertEqual('COMMERCIAL',product.sga_commercial_reference)
        self.assertEqual((130,100),(product.list_price,product.standard_price))
        self.assertEqual('000123',product.sga_catalog_values['default_code'])
        self.env['sga.catalog.import']._unchanged_locally(product,{})
        self.assertEqual(0,self.env['product.template']._normalize_sga_article_codes(after_id=product.id-1,limit=1,dry_run=False)['changed'])

    def test_archived_native_numbers_do_not_become_commercial_references(self):
        product=self.product(active=False)
        product.with_context(sga_card_internal=True).default_code='123'
        self.env['product.template']._normalize_sga_article_codes(after_id=product.id-1,limit=1,dry_run=False)
        self.assertEqual('000123',product.default_code)
        self.assertFalse(product.sga_commercial_reference)
        self.assertFalse(product.active)
