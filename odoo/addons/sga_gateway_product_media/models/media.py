import base64
import binascii
import hashlib
import io
import re

from PIL import Image, UnidentifiedImageError
from odoo import fields, models
from odoo.exceptions import ValidationError
from odoo.addons.servirepuestos_20_compat.binary import as_binary, binary_content

MAX_IMAGE_BYTES = 6 * 1024 * 1024
KEY = re.compile(r'^(\d+)(?:_\d+)?\.(?:jpe?g|png|webp|bmp)$')


def image_hash(value):
    return hashlib.sha256(binary_content(value)).hexdigest() if value else ''


class MediaProduct(models.Model):
    _inherit = 'product.template'

    sga_media_primary_key = fields.Char(readonly=True, copy=False)
    sga_media_primary_checksum = fields.Char(readonly=True, copy=False)
    sga_media_primary_target_checksum = fields.Char(readonly=True, copy=False)


class MediaGallery(models.Model):
    _inherit = 'product.gallery.image'

    sga_media_instance = fields.Char(readonly=True, copy=False, index=True)
    sga_media_key = fields.Char(readonly=True, copy=False, index=True)
    sga_media_checksum = fields.Char(readonly=True, copy=False)
    sga_media_target_checksum = fields.Char(readonly=True, copy=False)
    sga_media_company_id = fields.Many2one(related='product_tmpl_id.company_id', store=True)
    _unique_source_image = models.Constraint(
        'UNIQUE(sga_media_company_id, sga_media_instance, sga_media_key)',
        'Esta fotografía SGA ya está vinculada.')


class MediaImport(models.AbstractModel):
    _name = 'sga.media.import'
    _description = 'Contrato de fotografías SGA'

    def _validate_item(self, item):
        if not isinstance(item, dict) or set(item) != {'article_id', 'key', 'checksum', 'primary', 'sequence'}:
            raise ValidationError('Metadatos de imagen inválidos.')
        key = item['key']
        match = KEY.fullmatch(key) if isinstance(key, str) and len(key) <= 255 else None
        if (not match or type(item['article_id']) is not int or item['article_id'] <= 0
                or int(match[1]) != item['article_id'] or type(item['primary']) is not bool
                or type(item['sequence']) is not int or not 0 <= item['sequence'] <= 1000000
                or not isinstance(item['checksum'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', item['checksum'])):
            raise ValidationError('Identidad, checksum o posición de imagen inválidos.')

    def _product(self, instance, article):
        return self.env['product.template'].sudo().with_context(active_test=False).search([
            ('company_id', '=', self.env.company.id), ('sga_catalog_instance', '=', instance),
            ('sga_catalog_article_id', '=', article)], limit=1)

    def _state(self, instance, item):
        self._validate_item(item)
        product = self._product(instance, item['article_id'])
        if not product:
            return {'key': item['key'], 'status': 'missing_product'}
        if item['primary']:
            previous = product.sga_media_primary_target_checksum
            local = image_hash(product.image_1920)
            if (previous and local != previous) or (not previous and product.image_1920):
                return {'key': item['key'], 'status': 'conflict'}
            same = (product.sga_media_primary_key == item['key']
                    and product.sga_media_primary_checksum == item['checksum'])
        else:
            record = self.env['product.gallery.image'].sudo().with_context(active_test=False).search([
                ('product_tmpl_id', '=', product.id), ('sga_media_instance', '=', instance),
                ('sga_media_key', '=', item['key'])], limit=1)
            if record and image_hash(record.image_1920) != record.sga_media_target_checksum:
                return {'key': item['key'], 'status': 'conflict'}
            same = bool(record and record.sga_media_checksum == item['checksum'])
        return {'key': item['key'], 'status': 'unchanged' if same else 'upload'}

    def _check_images(self, instance, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ValidationError('Se requieren entre 1 y 200 imágenes por consulta.')
        return {'items': [self._state(instance, item) for item in items]}

    def _apply_image(self, instance, item, encoded):
        self._validate_item(item)
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValidationError('Tamaño de imagen inválido.')
        try:
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_IMAGE_BYTES or hashlib.sha256(raw).hexdigest() != item['checksum']:
                raise ValueError('checksum')
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {'JPEG', 'PNG', 'WEBP', 'BMP'} or image.width * image.height > 40000000:
                    raise ValueError('image_bounds')
                image.verify()
        except (ValueError, OSError, UnidentifiedImageError, binascii.Error, Image.DecompressionBombError):
            raise ValidationError('Contenido de imagen inválido.') from None
        product = self._product(instance, item['article_id'])
        if not product:
            raise ValidationError('El artículo SGA aún no existe en esta compañía.')
        lock = int.from_bytes(hashlib.sha256(
            f'media:{self.env.company.id}:{instance}:{item["article_id"]}'.encode()).digest()[:8], 'big', signed=True)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [lock])
        product.invalidate_recordset()
        state = self._state(instance, item)['status']
        if state == 'conflict':
            raise ValidationError('La fotografía fue modificada localmente en Odoo.')
        if state == 'unchanged':
            return {'key': item['key'], 'status': 'unchanged'}
        if item['primary']:
            status = 'updated' if product.sga_media_primary_key else 'created'
            product.write({'image_1920': as_binary(encoded), 'sga_media_primary_key': item['key'],
                           'sga_media_primary_checksum': item['checksum']})
            product.sga_media_primary_target_checksum = image_hash(product.image_1920)
        else:
            Gallery = self.env['product.gallery.image'].sudo().with_context(active_test=False)
            record = Gallery.search([('product_tmpl_id', '=', product.id),
                                     ('sga_media_instance', '=', instance), ('sga_media_key', '=', item['key'])], limit=1)
            values = {'image_1920': as_binary(encoded), 'sga_media_checksum': item['checksum']}
            status = 'updated' if record else 'created'
            if record:
                record.write(values)
            else:
                record = Gallery.create({**values, 'name': item['key'], 'sequence': item['sequence'],
                    'product_tmpl_id': product.id, 'sga_media_instance': instance, 'sga_media_key': item['key']})
            record.sga_media_target_checksum = image_hash(record.image_1920)
        return {'key': item['key'], 'status': status}
