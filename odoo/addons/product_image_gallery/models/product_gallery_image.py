from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    gallery_image_ids = fields.One2many(
        "product.gallery.image",
        "product_tmpl_id",
        string="Galería de imágenes",
        copy=True,
    )


class ProductGalleryImage(models.Model):
    _name = "product.gallery.image"
    _description = "Imagen secundaria de producto"
    _inherit = ["image.mixin"]
    _order = "sequence, id"

    name = fields.Char(string="Nombre", required=True, translate=True)
    sequence = fields.Integer(default=10)
    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Producto",
        required=True,
        index=True,
        ondelete="cascade",
    )
    active = fields.Boolean(default=True)
