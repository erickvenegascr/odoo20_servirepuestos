{
    'name': 'SGA Gateway Connector',
    'version': '20.0.1.7.0',
    'summary': 'Read-only SGA catalog import and durable gateway outbox',
    'license': 'LGPL-3',
    'author': 'Servirepuestos',
    'depends': ['stock'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/catalog_group.xml',
        'security/ir.access.csv',
        'views/operation_views.xml',
        'views/application_views.xml',
        'views/product_views.xml',
        'data/cron.xml',
    ],
    'installable': True,
    'application': True,
}
