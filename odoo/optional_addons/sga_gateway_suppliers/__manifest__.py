{
    'name': 'SGA: proveedores por artículo',
    'version': '20.0.1.0.0',
    'summary': 'Códigos de compra, prioridad y estado de los proveedores de SGA',
    'author': 'Servirepuestos', 'license': 'LGPL-3',
    'depends': ['sga_gateway_connector', 'purchase'],
    'data': ['security/ir.access.csv', 'views/supplier_views.xml'],
    'installable': True,
}
