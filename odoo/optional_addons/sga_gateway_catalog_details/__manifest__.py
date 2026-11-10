{
    'name': 'SGA: categorías, datos fiscales y listas de precios',
    'version': '20.0.1.0.0',
    'summary': 'Completa la categorización nueva, CABYS, IVA, unidades y tarifas del catálogo SGA',
    'author': 'Servirepuestos',
    'license': 'LGPL-3',
    'depends': ['sga_gateway_connector', 'product_catalog_classification', 'crfe_connector'],
    'data': ['security/ir.access.csv', 'views/catalog_views.xml'],
    'installable': True,
    'application': False,
}
