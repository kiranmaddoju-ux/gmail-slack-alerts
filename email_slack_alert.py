import os
import datetime
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if not token_json:
        raise ValueError("GMAIL_TOKEN_JSON environment variable is missing.")
    
    creds_data = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
    
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        
    return build('gmail', 'v1', credentials=creds)

def send_slack_block_message(client, channel, sender, subject, snippet, mins, thread_id):
    gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
    
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🚨 *Overdue Email Reply Alert!* (Sitting for *{mins} mins*)"}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n{sender}"},
                {"type": "mrkdwn", "text": f"*Subject:*\n{subject}"}
            ]
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Preview Snippet:*\n>{snippet}"}
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Email in Gmail ↗"},
                    "url": gmail_url,
                    "action_id": "open_gmail_thread"
                }
            ]
        },
        {"type": "divider"}
    ]
    
    try:
        client.chat_postMessage(channel=channel, blocks=blocks, text=f"Alert: Unanswered email from {sender}")
    except SlackApiError as e:
        print(f"Error pushing to Slack: {e.response['error']}")

def check_unanswered_emails():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL", "#general")
    
    if not slack_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable is missing.")

    gmail_service = get_gmail_service()
    slack_client = WebClient(token=slack_token)
    
    query = "to:me is:unread"
    results = gmail_service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for msg in messages:
        msg_id = msg['id']
        thread_id = msg['threadId']
        
        msg_data = gmail_service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        payload = msg_data.get('payload', {})
        headers = payload.get('headers', [])
        
        subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown Sender')
        date_str = next((h['value'] for h in headers if h['name'].lower() == 'date'), None)
        snippet = msg_data.get('snippet', '')
        
        thread_data = gmail_service.users().threads().get(userId='me', id=thread_id).execute()
        thread_messages = thread_data.get('messages', [])
        
        already_replied = any("SENT" in t_msg.get('labelIds', []) for t_msg in thread_messages)
        if already_replied:
            continue
            
        if date_str:
            try:
                clean_date = date_str.split(' (')[0].strip()
                try:
                    email_time = datetime.datetime.strptime(clean_date, '%a, %d %b %Y %H:%M:%S %z')
                except ValueError:
                    email_time = datetime.datetime.strptime(clean_date, '%d %b %Y %H:%M:%S %z')
                
                time_diff = now - email_time
                minutes_unanswered = time_diff.total_seconds() / 60
                
                if minutes_unanswered >= 30:
                    send_slack_block_message(slack_client, slack_channel, sender, subject, snippet, int(minutes_unanswered), thread_id)
            except Exception as e:
                print(f"Skipping date parse on message {msg_id}: {e}")

if __name__ == '__main__':
    check_unanswered_emails()