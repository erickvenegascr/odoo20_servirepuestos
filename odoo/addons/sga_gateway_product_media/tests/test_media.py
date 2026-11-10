import base64
import hashlib
import io
import json

from PIL import Image
from odoo.tests import HttpCase, tagged
from odoo.addons.servirepuestos_20_compat.binary import as_binary, binary_content


@tagged('post_install', '-at_install')
class TestMediaApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = 'sgw1_' + 'm' * 64
        cls.app = cls.env['sga.gateway.application'].create({
            'name': 'Synthetic media app', 'source_instance': 'synthetic-media',
            'token_hash': hashlib.sha256(cls.token.encode()).hexdigest()})
        cls.product = cls.env['product.template'].create({
            'name': 'Synthetic image product', 'company_id': cls.app.company_id.id,
            'sga_catalog_instance': cls.app.source_instance, 'sga_catalog_article_id': 8})

    def image(self, key='8_1.jpg', primary=True, color='red', size=(100, 70)):
        stream = io.BytesIO(); Image.new('RGB', size, color).save(stream, format='JPEG')
        raw = stream.getvalue()
        return {'article_id': 8, 'key': key, 'primary': primary, 'sequence': 0 if primary else 10,
                'checksum': hashlib.sha256(raw).hexdigest()}, base64.b64encode(raw).decode()

    def post(self, path, body, token=None):
        return self.url_open('/sga_gateway/api/v1/' + path, data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + (self.token if token is None else token), 'Content-Type': 'application/json'})

    def upload(self, item, encoded):
        return self.post('media/image', {'instance': self.app.source_instance, 'item': item, 'content': encoded})

    def check(self, *items):
        return self.post('media/check', {'instance': self.app.source_instance, 'items': list(items)})

    def test_main_and_all_secondary_images_are_idempotent(self):
        rows = [self.image(), self.image('8_2.jpg', False, 'blue'), self.image('8_3.jpg', False, 'green')]
        for item, data in rows:
            self.assertEqual(self.upload(item, data).json()['status'], 'created')
        self.assertTrue(self.product.image_1920)
        self.assertEqual(len(self.product.gallery_image_ids), 2)
        self.assertEqual([r['status'] for r in self.check(*(r[0] for r in rows)).json()['items']], ['unchanged'] * 3)
        for item, data in rows:
            self.assertEqual(self.upload(item, data).json()['status'], 'unchanged')
        self.assertEqual(len(self.product.gallery_image_ids), 2)

    def test_native_resizing_does_not_break_checksum_replay(self):
        item, data = self.image(size=(2400, 1600))
        self.assertEqual(self.upload(item, data).status_code, 200)
        self.assertNotEqual(self.product.sga_media_primary_checksum, self.product.sga_media_primary_target_checksum)
        self.assertEqual(self.check(item).json()['items'][0]['status'], 'unchanged')

    def test_local_edits_are_conflicts_and_are_preserved(self):
        item, data = self.image();self.upload(item, data)
        _, local = self.image(color='blue')
        self.product.image_1920 = as_binary(local)
        value = binary_content(self.product.image_1920)
        self.assertEqual(self.check(item).json()['items'][0]['status'], 'conflict')
        self.assertEqual(self.upload(item, data).status_code, 409)
        self.assertEqual(binary_content(self.product.image_1920), value)

    def test_secondary_update_and_local_secondary_conflict(self):
        item, data = self.image('8_2.jpg', False);self.upload(item, data)
        item, data = self.image('8_2.jpg', False, 'green')
        self.assertEqual(self.upload(item, data).json()['status'], 'updated')
        self.assertEqual(len(self.product.gallery_image_ids), 1)
        _, local = self.image(color='blue')
        self.product.gallery_image_ids.image_1920 = as_binary(local)
        self.assertEqual(self.check(item).json()['items'][0]['status'], 'conflict')
        self.assertEqual(self.upload(item, data).status_code, 409)

    def test_authentication_and_source_scope(self):
        item, data = self.image()
        self.assertEqual(self.post('media/check', {'instance': self.app.source_instance, 'items': [item]}, token='').status_code, 401)
        self.assertEqual(self.post('media/check', {'instance': 'another-source', 'items': [item]}).status_code, 403)
        self.assertEqual(self.post('media/check', {'instance': self.app.source_instance, 'items': [item], 'company_id': 999}).status_code, 403)
        self.app.catalog_enabled = False
        self.assertEqual(self.upload(item, data).status_code, 403)

    def test_other_company_and_missing_article_cannot_be_written(self):
        item, data = self.image()
        other = self.env['res.company'].create({'name': 'Synthetic other media company'})
        self.product.company_id = other
        self.assertEqual(self.check(item).json()['items'][0]['status'], 'missing_product')
        self.assertEqual(self.upload(item, data).status_code, 409)

    def test_invalid_payloads_create_no_photos(self):
        item, data = self.image()
        for changes in [{'key': '../8_1.jpg'}, {'key': '9_1.jpg'}, {'article_id': True}, {'checksum': 'x' * 64}]:
            self.assertEqual(self.upload({**item, **changes}, data).status_code, 409)
        self.assertEqual(self.upload(item, 'broken base64!').status_code, 409)
        raw = b'not an image';invalid = dict(item, checksum=hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.upload(invalid, base64.b64encode(raw).decode()).status_code, 409)
        self.assertFalse(self.product.image_1920)
        self.assertFalse(self.product.gallery_image_ids)

    def test_fallback_main_can_become_secondary_without_loss(self):
        old, data = self.image('8_2.jpg', True);self.upload(old, data)
        new, new_data = self.image('8_1.jpg', True, 'blue');self.upload(new, new_data)
        old['primary'] = False;old['sequence'] = 10
        self.assertEqual(self.upload(old, data).json()['status'], 'created')
        self.assertEqual(self.product.sga_media_primary_key, '8_1.jpg')
        self.assertEqual(self.product.gallery_image_ids.sga_media_key, '8_2.jpg')
