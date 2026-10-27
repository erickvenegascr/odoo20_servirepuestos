{
    'name': 'SGA Gateway Connector',
    'version': '20.0.1.1.0',
    'summary': 'Immediate API delivery after commit with a durable recovery outbox',
    'license': 'LGPL-3',
    'author': 'Servirepuestos',
    'depends': ['base'],
    'external_dependencies': {'python': ['requests']},
    'data': ['security/ir.access.csv', 'views/operation_views.xml', 'data/cron.xml'],
    'installable': True,
    'application': False,
}
