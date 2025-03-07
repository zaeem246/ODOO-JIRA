from datetime import datetime, timedelta, timezone
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import requests
import base64
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

_logger = logging.getLogger(__name__)

class JiraConfiguration(models.Model):
    _name = 'jira.config'
    _description = 'Jira Configuration'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(required=True, tracking=True)
    url = fields.Char('Jira URL', required=True, tracking=True)
    email = fields.Char('Email', required=True, tracking=True)
    api_token = fields.Char('API Token', required=True)
    is_active = fields.Boolean('Active', default=True, tracking=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    last_sync_date = fields.Datetime('Last Sync', readonly=True)

    _sql_constraints = [
        ('unique_active_config',
         'UNIQUE(is_active,company_id)',
         'Only one active Jira configuration is allowed per company!')
    ]

    def _get_headers(self):
        credentials = f"{self.email}:{self.api_token}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        return {
            'Authorization': f'Basic {encoded_credentials}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

    def _make_request(self, endpoint, method='GET', data=None, stream=False):
        headers = self._get_headers()
        # Check if endpoint is already a full URL
        if endpoint.startswith('http://') or endpoint.startswith('https://'):
            url = endpoint
        else:
            url = f"{self.url.rstrip('/')}/rest/api/3/{endpoint}"
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, timeout=120, stream=stream)
            elif method == 'PUT':
                response = requests.put(url, headers=headers, json=data, timeout=120, stream=stream)
            elif method == 'POST':
                response = requests.post(url, headers=headers, json=data, timeout=120, stream=stream)
            
            # Raise an exception for bad status codes
            response.raise_for_status()
            
            return response
        except requests.exceptions.Timeout:
            raise UserError("Connection timeout. Please try again.")
        except requests.exceptions.RequestException as e:
            raise UserError(f"Jira API request failed: {str(e)}")

    def sync_jira_projects(self):
        response = self._make_request('project')
        if response.status_code == 200:
            projects = response.json()
            ProjectModel = self.env['project.project']
            
            for project in projects:
                existing_project = ProjectModel.search([('jira_key', '=', project['key'])], limit=1)
                project_vals = {
                    'name': project['name'],
                    'jira_key': project['key'],
                    'jira_id': project['id'],
                    'is_jira_project': True,
                }
                if existing_project:
                    existing_project.write(project_vals)
                else:
                    ProjectModel.create(project_vals)

    def _sync_jira_tickets(self, batch_size=100):
        jql_query = "ORDER BY updated DESC"
        start_at = 0
        total_processed = 0
        error_occurred = False

        # Cache for better performance
        stage_cache = {}
        user_cache = {}

        while True:
            response = self._make_request(f'search?jql={jql_query}&startAt={start_at}&maxResults={batch_size}')
            
            if response.status_code != 200:
                _logger.error(f"Jira API Error: {response.status_code}, Response: {response.text}")
                raise UserError("Failed to fetch Jira tickets. Check logs for details.")

            data = response.json()
            tickets = data.get('issues', [])
            if not tickets:
                break

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for ticket in tickets:
                    future = executor.submit(self._process_single_ticket, ticket, stage_cache, user_cache)
                    futures.append((future, ticket))
                
                for future, ticket in futures:
                    try:
                        future.result()
                    except Exception as e:
                        _logger.error(f"Error processing Jira ticket {ticket['key']}: {str(e)}")
                        error_occurred = True

            total_processed += len(tickets)
            if total_processed >= data.get('total', 0):
                break
            start_at += batch_size

        self.write({'last_sync_date': fields.Datetime.now()})
        return error_occurred



    def _process_single_ticket(self, ticket, stage_cache, user_cache):
        new_cr = self.pool.cursor()
        env = api.Environment(new_cr, self.env.uid, self.env.context)
        try:
            HelpdeskTicket = env['helpdesk.ticket']
            fields = ticket.get('fields', {})
            
            # Log full ticket data
            # _logger.debug(f"Full ticket data for {ticket.get('key', '')}: {ticket}")
            
            # Basic ticket information
            summary = fields.get('summary', '')
            if not summary:
                summary = f"Ticket {ticket.get('key', '')}"
                
            jira_status = fields.get('status', {}).get('name', 'Open')
            stage_id = stage_cache.get(jira_status)
            if stage_id is None:
                stage = env['helpdesk.stage'].search([('name', '=', jira_status)], limit=1)
                if not stage:
                    team = env['helpdesk.team'].search([], limit=1)
                    stage = env['helpdesk.stage'].create({
                        'name': jira_status,
                        'sequence': 10,
                        'team_ids': [(6, 0, [team.id] if team else [])]
                    })
                stage_cache[jira_status] = stage.id
                stage_id = stage.id
                
            assignee = fields.get('assignee', {})
            assignee_email = assignee.get('emailAddress', '') if assignee else ''
            user_id = user_cache.get(assignee_email)
            if assignee_email and user_id is None:
                user = env['res.users'].search([('email', '=', assignee_email)], limit=1)
                user_cache[assignee_email] = user.id if user else False
                user_id = user_cache[assignee_email]
                
            description = fields.get('description', '')
            if isinstance(description, dict):
                try:
                    # Handle numbered lists
                    formatted_description = []
                    for content in description.get('content', []):
                        if content.get('type') == 'orderedList':
                            # Process ordered list items
                            for i, listItem in enumerate(content.get('content', []), 1):
                                item_text = []
                                for paragraph in listItem.get('content', []):
                                    for text_node in paragraph.get('content', []):
                                        if text_node.get('type') == 'text':
                                            item_text.append(text_node.get('text', ''))
                                formatted_description.append(f"{i}. {''.join(item_text)}")
                        elif content.get('type') == 'paragraph':
                            # Process regular paragraphs
                            paragraph_text = []
                            for text_node in content.get('content', []):
                                if text_node.get('type') == 'text':
                                    paragraph_text.append(text_node.get('text', ''))
                            formatted_description.append(''.join(paragraph_text))
                    
                    description = '\n\n'.join(formatted_description)
                except Exception as e:
                    _logger.error(f"Error parsing description: {str(e)}")
                    description = ''

                    
            created_date = fields.get('created')
            if created_date:
                try:
                    created_date = datetime.strptime(created_date, '%Y-%m-%dT%H:%M:%S.%f%z').astimezone(timezone.utc).replace(tzinfo=None)
                except Exception as e:
                    # _logger.error(f"Error parsing created_date for {ticket.get('key', '')}: {str(e)}")
                    created_date = fields.Datetime.now()
            else:
                created_date = fields.Datetime.now()
                
            # Initialize containers for comments and attachments
            comments_text = """
                <div class="jira-container" style="display: flex; gap: 20px; max-width: 100%;">
                    <div class="jira-comments-container" style="flex: 1; max-height: 400px; overflow-y: auto; padding: 15px; border: 1px solid #e0e0e0; border-radius: 8px; background-color: #f9f9f9;">
                        <h3 style="margin-top: 0; border-bottom: 1px solid #e0e0e0; padding-bottom: 10px;">COMMENTS</h3>
            """
            
            attachments_text = """
                    <div class="jira-attachments-container" style="flex: 1; max-height: 400px; overflow-y: auto; padding: 15px; border: 1px solid #e0e0e0; border-radius: 8px; background-color: #f9f9f9;">
                        <h3 style="margin-top: 0; border-bottom: 1px solid #e0e0e0; padding-bottom: 10px;">ALL ATTACHMENTS</h3>
            """
            
            all_attachments = []  # To collect all attachments
            
            # Process comments
            try:
                comments_response = self._make_request(f'issue/{ticket["key"]}/comment')
                if comments_response.status_code == 200:
                    comments = comments_response.json().get('comments', [])
                    # _logger.info(f"Found {len(comments)} comments for ticket {ticket['key']}")
                    if comments:
                        for comment in reversed(comments):
                            body = comment.get('body', '')
                            comment_attachments = []
                            
                            # _logger.debug(f"Raw comment data for {ticket['key']} (ID: {comment['id']}): {comment}")
                            # _logger.debug(f"Comment body for {ticket['key']} (ID: {comment['id']}): {body}")
                            
                            if isinstance(body, dict):
                                text_content = '\n'.join(
                                    item.get('text', '')
                                    for content in body.get('content', [])
                                    for item in content.get('content', [])
                                    if item.get('type') == 'text'
                                ) or ''
                                
                                for content in body.get('content', []):
                                    if content.get('type') in ['mediaGroup', 'attachment', 'file', 'media']:
                                        for item in content.get('content', []):
                                            if item.get('type') in ['media', 'file']:
                                                attachment_url = item.get('attrs', {}).get('url', '')
                                                attachment_name = item.get('attrs', {}).get('name', '')
                                                # _logger.info(f"Detected ADF comment attachment: {attachment_name} with URL: {attachment_url}")
                                                if attachment_url:
                                                    try:
                                                        attachment_response = self._make_request(attachment_url, stream=True)
                                                        if attachment_response.status_code == 200:
                                                            attachment_data = base64.b64encode(attachment_response.content).decode('utf-8')
                                                            # Create attachment immediately to get ID
                                                            attachment_record = env['ir.attachment'].create({
                                                                'name': attachment_name or f"attachment_{comment['id']}",
                                                                'datas': attachment_data,
                                                                'mimetype': attachment_response.headers.get('Content-Type', 'application/octet-stream'),
                                                                'res_model': 'helpdesk.ticket',
                                                                'res_id': ticket_id if 'ticket_id' in locals() else 0,  # Temporary 0, updated later
                                                            })
                                                            # _logger.info(f"Created comment attachment {attachment_name} (ID: {attachment_record.id})")
                                                            attachment_link = f"/web/content/{attachment_record.id}?download=true"
                                                            comment_attachments.append({'name': attachment_name, 'link': attachment_link})
                                                            all_attachments.append({'name': attachment_name, 'link': attachment_link})
                                                        else:
                                                            _logger.error(f"Failed to download ADF comment attachment {attachment_url}: Status {attachment_response.status_code}")
                                                    except UserError as e:
                                                        _logger.error(f"UserError downloading ADF comment attachment {attachment_url}: {str(e)}")
                                                    except Exception as e:
                                                        _logger.error(f"Error downloading ADF comment attachment {attachment_url}: {str(e)}")
                                body = text_content
                            elif isinstance(body, str):
                                body = body.strip()
                                url_pattern = r'(https?://[^\s]+\.(?:jpg|jpeg|png|gif|pdf|docx?|xlsx?|zip))'
                                attachment_urls = re.findall(url_pattern, body)
                                if attachment_urls:
                                    for attachment_url in attachment_urls:
                                        guessed_name = attachment_url.split('/')[-1]
                                        # _logger.info(f"Detected potential attachment URL in comment: {guessed_name} with URL: {attachment_url}")
                                        try:
                                            attachment_response = self._make_request(attachment_url, stream=True)
                                            if attachment_response.status_code == 200:
                                                attachment_data = base64.b64encode(attachment_response.content).decode('utf-8')
                                                # Create attachment immediately to get ID
                                                attachment_record = env['ir.attachment'].create({
                                                    'name': guessed_name or f"attachment_{comment['id']}",
                                                    'datas': attachment_data,
                                                    'mimetype': attachment_response.headers.get('Content-Type', 'application/octet-stream'),
                                                    'res_model': 'helpdesk.ticket',
                                                    'res_id': ticket_id if 'ticket_id' in locals() else 0,  # Temporary 0, updated later
                                                })
                                                # _logger.info(f"Created comment attachment {guessed_name} (ID: {attachment_record.id})")
                                                attachment_link = f"/web/content/{attachment_record.id}?download=true"
                                                comment_attachments.append({'name': guessed_name, 'link': attachment_link})
                                                all_attachments.append({'name': guessed_name, 'link': attachment_link})
                                                body = body.replace(attachment_url, '')
                                            else:
                                                _logger.error(f"Failed to download comment attachment URL {attachment_url}: Status {attachment_response.status_code}")
                                        except UserError as e:
                                            _logger.error(f"UserError downloading comment attachment URL {attachment_url}: {str(e)}")
                                        except Exception as e:
                                            _logger.error(f"Error downloading comment attachment URL {attachment_url}: {str(e)}")
                            
                            author = comment.get('author', {}).get('displayName', 'Unknown')
                            comment_created = datetime.strptime(comment['created'], '%Y-%m-%dT%H:%M:%S.%f%z').strftime('%Y-%m-%d %H:%M:%S')
                            
                            if body.strip():
                                comments_text += f"""
                                    <div class="jira-comment" style="background-color: white; margin-bottom: 15px; padding: 15px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                                        <p class="comment-header" style="margin: 0 0 10px 0; color: #666; font-size: 0.9em;">
                                            <strong style="color: #2c5282;">{author}</strong>
                                            <span style="color: #718096;"> - {comment_created}</span>
                                        </p>
                                        <p class="comment-body" style="margin: 0; line-height: 1.5; color: #2d3748; white-space: pre-wrap;">{body}</p>
                                    </div>
                                """
                else:
                    _logger.error(f"Failed to fetch comments for {ticket['key']}: Status {comments_response.status_code}")
            except UserError as e:
                _logger.error(f"UserError fetching comments for ticket {ticket['key']}: {str(e)}")
            except Exception as e:
                _logger.error(f"Error fetching comments for ticket {ticket['key']}: {str(e)}")
            
            # Fallback: Check attachments via issue endpoint
            try:
                attachments_response = self._make_request(f'issue/{ticket["key"]}')
                if attachments_response.status_code == 200:
                    issue_data = attachments_response.json()
                    attachments = issue_data.get('fields', {}).get('attachment', [])
                    if attachments:
                        # _logger.info(f"Found {len(attachments)} attachments via issue endpoint for {ticket['key']}")
                        for attachment in attachments:
                            attachment_url = attachment.get('content', '')
                            attachment_name = attachment.get('filename', '')
                            # _logger.info(f"Detected issue endpoint attachment: {attachment_name} with URL: {attachment_url}")
                            if attachment_url:
                                try:
                                    attachment_response = self._make_request(attachment_url, stream=True)
                                    if attachment_response.status_code == 200:
                                        attachment_data = base64.b64encode(attachment_response.content).decode('utf-8')
                                        # Create attachment immediately to get ID
                                        attachment_record = env['ir.attachment'].create({
                                            'name': attachment_name,
                                            'datas': attachment_data,
                                            'mimetype': attachment.get('mimeType', 'application/octet-stream'),
                                            'res_model': 'helpdesk.ticket',
                                            'res_id': ticket_id if 'ticket_id' in locals() else 0,  # Temporary 0, updated later
                                        })
                                        # _logger.info(f"Created issue attachment {attachment_name} (ID: {attachment_record.id})")
                                        attachment_link = f"/web/content/{attachment_record.id}?download=true"
                                        all_attachments.append({'name': attachment_name, 'link': attachment_link})
                                    else:
                                        _logger.error(f"Failed to download issue endpoint attachment {attachment_url}: Status {attachment_response.status_code}")
                                except UserError as e:
                                    _logger.error(f"UserError downloading issue endpoint attachment {attachment_url}: {str(e)}")
                                except Exception as e:
                                    _logger.error(f"Error downloading issue endpoint attachment {attachment_url}: {str(e)}")
            except UserError as e:
                _logger.error(f"UserError fetching attachments via issue endpoint for {ticket['key']}: {str(e)}")
            except Exception as e:
                _logger.error(f"Error fetching attachments via issue endpoint for {ticket['key']}: {str(e)}")
            
            # Add all attachments to the attachments section
            if all_attachments:
                attachments_text += '<div class="attachments-list" style="display: flex; flex-wrap: wrap; gap: 10px;">'
                for att in all_attachments:
                    attachments_text += f'''
                        <div class="attachment-item" style="background-color: white; padding: 10px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); width: calc(50% - 5px);">
                            <p style="margin: 0;">
                                <a href="{att["link"]}" target="_blank" style="display: flex; align-items: center; text-decoration: none; color: #2c5282;">
                                    <span style="margin-right: 5px; font-size: 1.2em;">📎</span>
                                    <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{att["name"]}</span>
                                </a>
                            </p>
                        </div>
                    '''
                attachments_text += '</div>'
            else:
                attachments_text += '<p style="color: #718096;">No attachments found</p>'
            
            # Close the containers
            comments_text += '</div>'
            attachments_text += '</div>'
            
            # Combine the two sections
            full_content = comments_text + attachments_text + '</div>'
            _logger.debug(f"Final content for {ticket.get('key', '')}: {full_content}")
            
            priority = fields.get('priority', {})
            
            ticket_vals = {
                'name': summary,
                'description': description,
                'jira_key': ticket.get('key', ''),
                'jira_id': ticket.get('id', ''),
                'jira_status': jira_status,
                'jira_priority': priority.get('name', '') if priority else '',
                'jira_created_date': created_date,
                'stage_id': stage_id,
                'is_jira_ticket': True,
                'user_id': user_id or False,
                'jira_comments': full_content,
            }
            
            # Create or update ticket
            existing_ticket = HelpdeskTicket.search([('jira_key', '=', ticket.get('key'))], limit=1)
            if existing_ticket:
                existing_ticket.with_context(from_jira_sync=True).write(ticket_vals)
                ticket_id = existing_ticket.id
            else:
                new_ticket = HelpdeskTicket.with_context(from_jira_sync=True).create(ticket_vals)
                ticket_id = new_ticket.id
            
            # Update res_id for attachments created before ticket_id was known
            env['ir.attachment'].search([
                ('res_model', '=', 'helpdesk.ticket'),
                ('res_id', '=', 0),
                ('create_date', '>=', datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5))
            ]).write({'res_id': ticket_id})
            
            new_cr.commit()
        except Exception as e:
            new_cr.rollback()
            _logger.error(f"Error processing ticket {ticket.get('key', '')}: {str(e)}")
            raise e
        finally:
            new_cr.close()


            

    def test_connection(self):
        self.ensure_one()
        response = self._make_request('myself')
        if response.status_code == 200:
            self.sync_jira_projects()
            # self._sync_jira_tickets()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Success',
                    'message': 'Connected to Jira.',
                    'type': 'success',
                }
            }
        raise UserError(f"Connection failed: {response.text}")

    def _auto_sync_jira_data(self):
        active_config = self.search([('is_active', '=', True)], limit=1)
        if active_config:
            active_config.sync_jira_projects()
            active_config._sync_jira_tickets()


    def sync_jira_data(self):
        self.ensure_one()
        try:
            # Test connection first
            response = self._make_request('myself')
            if response.status_code == 200:
                # Connection successful, schedule the sync
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
                        'message': 'Jira sync initiated. Your tickets will be updated shortly while you continue working.',
                        'type': 'success',
                    }
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Connection Failed',
                        'message': f'Could not connect to Jira. Please check your configuration.',
                        'type': 'danger',
                    }
                }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': f'Failed to connect to Jira: {str(e)}',
                    'type': 'danger',
                }
            }
