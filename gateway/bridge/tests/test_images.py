import hashlib
from io import StringIO
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from bridge.image_source import ImageSourceChanged, ProductImageSource
from bridge.models import ProductImageRun


class ImageSourceTests(SimpleTestCase):
    def test_primary_priority_and_every_secondary_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ['8_1.jpg', '8_1.bmp', '8_2.jpg', '9_3.png', '9_2.jpg', 'Thumbs.db']:
                Path(folder, name).write_bytes(b'synthetic source')
            source = ProductImageSource(folder);rows = source.snapshot()
            self.assertEqual(len(rows), 5)
            self.assertEqual([r['key'] for r in rows if r['primary']], ['8_1.jpg', '9_2.jpg'])
            self.assertEqual(len([r for r in rows if not r['primary']]), 3)
            before = {p.name: p.read_bytes() for p in Path(folder).iterdir()}
            for row in rows:
                item, raw = source.read(row)
                self.assertEqual(item['checksum'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(folder).iterdir()})

    def test_links_and_case_collisions_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, '8_1.jpg').write_bytes(b'image')
            Path(folder, '8_2.jpg').symlink_to(Path(folder, '8_1.jpg'))
            with self.assertRaises(ValueError):ProductImageSource(folder).snapshot()

    def test_changed_file_is_not_uploaded_under_a_stale_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, '8_1.jpg');path.write_bytes(b'old')
            source = ProductImageSource(folder);row = source.snapshot()[0]
            path.write_bytes(b'new larger content')
            with self.assertRaises(ImageSourceChanged):source.read(row)


class ImageCommandTests(TestCase):
    def config(self, name):
        return ({'instance': 'synthetic-images'} if name == 'SGA_CATALOG_SOURCE' else
                {'url': 'https://odoo.invalid', 'database': 'synthetic', 'company_id': 1, 'application_id': 'synthetic-app'})

    def run_scan(self, folder, apply=False, fail_after_write=False, client=None):
        class Remote:
            close = lambda self: None
            def __init__(self):self.bound = set();self.uploads = 0
            def call(self, method, **body):
                if method == 'catalog_info':return {'contract': 1, 'media_contract': 1,
                    'database': 'synthetic', 'company_id': 1, 'application_id': 'synthetic-app',
                    'source_instance': 'synthetic-images', 'authentication': 'application_token', 'sga_writes_enabled': False}
                if method == 'check_images':return {'items': [
                    {'key': r['key'], 'status': 'unchanged' if r['key'] in self.bound else 'upload'} for r in body['items']]}
                self.bound.add(body['item']['key']);self.uploads += 1
                if fail_after_write:raise RuntimeError('synthetic lost ack')
                return {'key': body['item']['key'], 'status': 'created'}
        client = client or Remote()
        with patch('bridge.management.commands.sync_product_images.configuration', side_effect=self.config), \
             patch('bridge.management.commands.sync_product_images.CatalogOdoo', return_value=client):
            call_command('sync_product_images', source_directory=folder, apply=apply, stdout=StringIO())
        return client

    def test_default_simulation_sends_no_image_content(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, '8_1.jpg').write_bytes(b'synthetic')
            client = self.run_scan(folder)
        self.assertEqual(client.uploads, 0)
        self.assertEqual(ProductImageRun.objects.get().result_counts, {'would_upload': 1})

    def test_apply_and_complete_replay_do_not_duplicate_uploads(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ['8_1.jpg', '8_2.jpg']:Path(folder, name).write_bytes(b'synthetic')
            client = self.run_scan(folder, apply=True)
            self.run_scan(folder, apply=True, client=client)
        self.assertEqual(client.uploads, 2)
        run = ProductImageRun.objects.latest('created_at')
        self.assertEqual((run.files, run.products, run.primary, run.secondary), (2, 1, 1, 1))
        self.assertEqual(run.result_counts, {'unchanged': 2})

    def test_lost_ack_keeps_failure_audited_without_skipping_file(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, '8_1.jpg').write_bytes(b'synthetic')
            with self.assertRaises(CommandError):self.run_scan(folder, apply=True, fail_after_write=True)
        run = ProductImageRun.objects.get()
        self.assertEqual(run.status, 'failed');self.assertEqual(run.files, 0)
