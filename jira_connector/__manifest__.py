{
    'name': 'Jira Connector for Odoo',
    'version': '1.0',
    'category': 'Integration',
    'summary': 'Jira Integration for Odoo',
    'author': 'MUHAMMAD ZAEEM MOHZAR',
    'depends': [
        'base',
        'mail',
        'project',
    ],
    'data': [
        'security/jira_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/menu_views.xml',
        'views/jira_config_views.xml',
        'views/project_views.xml',
        'views/helpdesk_views.xml',
    ],
    
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
    'currency': 'USD',
    'price': 50.0,
}
