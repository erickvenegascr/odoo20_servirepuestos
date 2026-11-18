from odoo import models


class FiscalBotDocumentLine(models.Model):
    _inherit = 'fiscalbot.document.line'

    def _sync_product_cabys_from_invoice(self):
        # Invoice CABYS belongs to the supplier document. SGA catalogue fields
        # are reconciled through the gateway, including before the first load.
        local_lines = self.filtered(lambda line: not line.product_id.product_tmpl_id.sga_catalog_instance)
        return super(FiscalBotDocumentLine, local_lines)._sync_product_cabys_from_invoice()

    def _prepare_supplierinfo_vals(self, supplierinfo=False):
        vals = super()._prepare_supplierinfo_vals(supplierinfo=supplierinfo)
        if supplierinfo and 'sga_binding_id' in supplierinfo._fields and supplierinfo.sga_binding_id:
            # Supplier invoice codes remain on their document; catalogue code
            # is owned by the source relation contract. Price updates still work.
            vals.pop('product_code', None)
        return vals
