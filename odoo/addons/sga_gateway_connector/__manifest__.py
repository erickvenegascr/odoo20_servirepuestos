{
    'name': 'SGA Gateway Connector',
    'version': '20.0.1.0.0',
    'summary': 'Durable Odoo outbox and status mirror for the Django gateway',
    'license': 'LGPL-3',
    'depends': ['base'],
    'external_dependencies': {'python': ['requests']},
    'data': ['security/ir.model.access.csv', 'security/rules.xml', 'views/operation_views.xml', 'data/cron.xml'],
    'installable': True,
    'application': False,
}
