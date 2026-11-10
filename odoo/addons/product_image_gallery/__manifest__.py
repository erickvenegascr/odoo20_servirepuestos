{
    "name": "Product Image Gallery",
    "version": "20.0.1.1.1",
    "category": "Sales/Sales",
    "summary": "Multiple ordered images on products without requiring eCommerce",
    "author": "Erick Venegas",
    "license": "LGPL-3",
    "depends": ["servirepuestos_20_compat", "product"],
    "data": [
        "views/product_template_views.xml",
        'security/ir.access.csv',
    ],
    "installable": True,
    "assets": {
        "web.assets_backend": ["product_image_gallery/static/src/product_form_lifecycle.js"],
        "web.assets_unit_tests": ["product_image_gallery/static/tests/product_form_lifecycle.test.js"],
    },
    "application": False,
}
