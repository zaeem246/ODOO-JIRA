import traceback
from odoo import models, fields, api
import re
import logging

_logger = logging.getLogger(__name__)

class HelpdeskTicket(models.Model):
    _inherit = 'helpdesk.ticket'

    jira_key = fields.Char('Jira Key')
    jira_id = fields.Char('Jira ID')
    jira_status = fields.Char('Jira Status')
    is_jira_ticket = fields.Boolean('Is Jira Ticket')
    jira_priority = fields.Char('Jira Priority') 
    jira_created_date = fields.Datetime('Jira Created Date') 
    jira_comments = fields.Html('Jira Comments', readonly=True, sanitize=False)
    new_jira_comment = fields.Text('New Comment')

    


    def sync_jira_data(self):
        jira_config = self.env['jira.config'].search([('is_active', '=', True)], limit=1)
        if jira_config:
            # Schedule the next cron run immediately
            cron = self.env.ref('jira_connector.ir_cron_sync_jira_data')
            cron.write({
                'nextcall': fields.Datetime.now(),
                'active': True
            })

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Success',
                    'message': 'Jira tickets sync has started. Your tickets will be updated automatically in the background. You can continue working while the sync completes.',
                    'type': 'success',
                }
            }
    def write(self, vals):
        result = super().write(vals)
        
        # Only update Jira if this write didn't come from Jira sync
        if not self.env.context.get('from_jira_sync'):
            for ticket in self:
                if ticket.is_jira_ticket and ticket.jira_key:
                    # Handle new comment
                    if 'new_jira_comment' in vals and vals['new_jira_comment']:
                        comment_text = f"{vals['new_jira_comment']}"
                        jira_config = self.env['jira.config'].search([('is_active', '=', True)], limit=1)
                        if jira_config:
                            data = {
                                "body": {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": comment_text
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                            response = jira_config._make_request(
                                f'issue/{ticket.jira_key}/comment',
                                method='POST',
                                data=data
                            )
                            if response.status_code == 201:
                                ticket.with_context(from_jira_sync=True).write({'new_jira_comment': ''})
                                ticket.sync_jira_data()
                    
                    # Handle other field updates
                    ticket._update_jira_ticket(vals)
        
        return result










    def _update_jira_ticket(self, vals):
        jira_config = self.env['jira.config'].search([('is_active', '=', True)], limit=1)
        if not jira_config:
            return

        update_fields = {}
        
        if 'name' in vals:
            update_fields['summary'] = vals['name']
        
        if 'description' in vals:
            # Remove HTML entities and special characters
            clean_description = vals['description'].replace('&nbsp;', ' ')
            # Remove HTML tags
            clean_description = re.sub(r'<[^>]+>', '', clean_description)
            # Remove multiple spaces and trim
            clean_description = ' '.join(clean_description.split())
            
            update_fields['description'] = {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": clean_description
                            }
                        ]
                    }
                ]
            }


        if update_fields:
            data = {"fields": update_fields}
            jira_config._make_request(
                f'issue/{self.jira_key}',
                method='PUT',
                data=data
            )

        # Handle stage changes
        if 'stage_id' in vals:
            stage = self.env['helpdesk.stage'].browse(vals['stage_id'])
            transitions_response = jira_config._make_request(
                f'issue/{self.jira_key}/transitions'
            )
            
            if transitions_response.status_code == 200:
                transitions = transitions_response.json()['transitions']
                for transition in transitions:
                    if transition['to']['name'] == stage.name:
                        jira_config._make_request(
                            f'issue/{self.jira_key}/transitions',
                            method='POST',
                            data={
                                "transition": {
                                    "id": transition['id']
                                }
                            }
                        )
                        break
