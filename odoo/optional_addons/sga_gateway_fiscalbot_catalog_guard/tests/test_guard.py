from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestFiscalBotCatalogGuard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = cls.env['fiscalbot.document'].create({
            'name': 'Factura sintética', 'external_id': 'synthetic-sga-cabys-guard'})

    def line(self, product):
        return self.env['fiscalbot.document.line'].with_context(fiscalbot_skip_supplierinfo_sync=True).create({
            'document_id': self.document.id, 'name': 'ENCOMIENDA SINTÉTICA',
            'product_id': product.product_variant_id.id, 'uom_id': product.uom_id.id,
            'cabys_code': '6511600000000', 'quantity': 1, 'price_unit': 100})

    def test_invoice_preserves_bound_catalog_and_keeps_supplier_cabys(self):
        product = self.env['product.template'].create({'name': 'PISTÓN SINTÉTICO',
            'sga_catalog_instance': 'synthetic-guard', 'sga_catalog_article_id': 500,
            'company_id': self.env.company.id, 'cabys_code': '4331002000100', 'cabys_description': 'Rodamientos sintéticos'})
        line = self.line(product)
        self.assertEqual(line.cabys_code, '6511600000000')
        self.assertEqual(product.cabys_code, '4331002000100')
        self.assertEqual(product.cabys_description, 'Rodamientos sintéticos')
        line.with_context(fiscalbot_skip_supplierinfo_sync=False).write({'cabys_code': '6511900009900'})
        self.assertEqual(product.cabys_code, '4331002000100')

    def test_sga_product_waiting_for_initial_cabys_is_not_filled_from_invoice(self):
        product = self.env['product.template'].create({'name': 'CATÁLOGO SGA PENDIENTE',
            'sga_catalog_instance': 'synthetic-guard', 'sga_catalog_article_id': 501,
            'company_id': self.env.company.id, 'cabys_code': False, 'cabys_description': False})
        self.line(product)
        self.assertFalse(product.cabys_code)
        self.assertFalse(product.cabys_description)

    def test_local_catalog_preserves_existing_fiscalbot_behavior(self):
        product = self.env['product.template'].create({'name': 'PRODUCTO LOCAL', 'cabys_code': False})
        self.line(product)
        self.assertEqual(product.cabys_code, '6511600000000')
        self.assertEqual(product.cabys_description, 'ENCOMIENDA SINTÉTICA')
