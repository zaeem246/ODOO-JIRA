from odoo import models, fields, api

class JiraProject(models.Model):
    _inherit = 'project.project'

    jira_key = fields.Char('Jira Key')
    jira_id = fields.Char('Jira ID')
    is_jira_project = fields.Boolean('Is Jira Project')

    def write(self, vals):
        result = super().write(vals)
        for project in self:
            if project.is_jira_project and project.jira_key:
                project._update_jira_project(vals)
        return result
        
    def _update_jira_project(self, vals):
        jira_config = self.env['jira.config'].search([('is_active', '=', True)], limit=1)
        if not jira_config:
            return

        update_fields = {}
        if 'name' in vals:
            update_fields['name'] = vals['name']
        if 'description' in vals:
            update_fields['description'] = vals['description']

        if update_fields:
            data = {
                "name": update_fields.get('name', self.name),
                "description": update_fields.get('description', self.description or '')
            }
            jira_config._make_request(
                f'project/{self.jira_key}',
                method='PUT',
                data=data
            )
