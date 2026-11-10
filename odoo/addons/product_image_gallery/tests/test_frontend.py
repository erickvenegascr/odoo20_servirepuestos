from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestProductGalleryFrontend(HttpCase):
    def test_product_lifecycle_hoot(self):
        value = 0
        for char in '@product_image_gallery':
            value = ((value << 5) - value + ord(char)) & 0xFFFFFFFF
        self.browser_js(
            f'/web/tests?headless&loglevel=2&preset=desktop&timeout=15000&id={value:08x}',
            '', '', login='admin', timeout=180,
            success_signal='[HOOT] Test suite succeeded',
            error_checker=lambda message: '[HOOT]' not in message)
