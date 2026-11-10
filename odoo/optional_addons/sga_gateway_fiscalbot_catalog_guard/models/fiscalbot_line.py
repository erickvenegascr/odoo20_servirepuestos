from odoo import models


class FiscalBotDocumentLine(models.Model):
    _inherit = 'fiscalbot.document.line'

    def _sync_product_cabys_from_invoice(self):
        # Invoice CABYS belongs to the supplier document. SGA catalogue fields
        # are reconciled through the gateway, including before the first load.
        local_lines = self.filtered(lambda line: not line.product_id.product_tmpl_id.sga_catalog_instance)
        return super(FiscalBotDocumentLine, local_lines)._sync_product_cabys_from_invoice()
