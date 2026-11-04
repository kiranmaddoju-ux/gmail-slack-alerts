import os
import datetime
import json
import email.utils
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Added modify scope to tag alerted threads so you don't get duplicate notifications
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify'
]

ALERT_LABEL_NAME = "Slack-Alerted"

def get_gmail_service():
    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if not token_json:
        raise ValueError("GMAIL_TOKEN_JSON environment variable is missing.")
    
    creds_data = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
    
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        
    return build('gmail', 'v1', credentials=creds)

def get_or_create_alert_label(gmail_service):
    """Finds or automatically creates the 'Slack-Alerted' label in Gmail."""
    labels_res = gmail_service.users().labels().list(userId='me').execute()
    labels = labels_res.get('labels', [])
    for label in labels:
        if label['name'].lower() == ALERT_LABEL_NAME.lower():
            return label['id']
            
    # If it doesn't exist, create it
    label_body = {
        'name': ALERT_LABEL_NAME,
        'labelListVisibility': 'labelShow',
        'messageListVisibility': 'show'
    }
    created = gmail_service.users().labels().create(userId='me', body=label_body).execute()
    return created['id']

def send_slack_block_message(client, channel, sender, subject, snippet, mins, thread_id):
    gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
    
    # Format overdue time cleanly (e.g., in hours if older than 60 mins)
    time_display = f"{mins} mins" if mins < 60 else f"{mins // 60}h {mins % 60}m"
    
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🚨 *Needs Your Reply!* (Overdue by *{time_display}*)"}
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
                    "text": {"type": "plain_text", "text": "Open Email to Reply ↗"},
                    "url": gmail_url,
                    "action_id": "open_gmail_thread"
                }
            ]
        },
        {"type": "divider"}
    ]
    
    try:
        client.chat_postMessage(channel=channel, blocks=blocks, text=f"Action Required: Unreplied email from {sender}")
        return True
    except SlackApiError as e:
        print(f"Error pushing to Slack: {e.response['error']}")
        return False

def check_unanswered_emails():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL", "#general")
    
    if not slack_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable is missing.")

    gmail_service = get_gmail_service()
    slack_client = WebClient(token=slack_token)
    
    # 1. Fetch user's email address
    user_profile = gmail_service.users().getProfile(userId='me').execute()
    my_email = user_profile.get('emailAddress', '').lower()
    
    # 2. Get or create tracker label
    try:
        alert_label_id = get_or_create_alert_label(gmail_service)
    except Exception as e:
        print(f"Could not manage tracking label (falling back without label tagging): {e}")
        alert_label_id = None

    # 3. Look across the last 3 days to catch yesterday's pending emails
    query = "to:me newer_than:3d"
    results = gmail_service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    now = datetime.datetime.now(datetime.timezone.utc)
    processed_threads = set()

    for msg in messages:
        thread_id = msg['threadId']
        if thread_id in processed_threads:
            continue
        processed_threads.add(thread_id)

        thread_data = gmail_service.users().threads().get(userId='me', id=thread_id).execute()
        thread_messages = thread_data.get('messages', [])
        if not thread_messages:
            continue

        # Look at the most recent message in the thread
        latest_msg = thread_messages[-1]
        latest_payload = latest_msg.get('payload', {})
        latest_headers = latest_payload.get('headers', [])
        latest_labels = latest_msg.get('labelIds', [])

        sender = next((h['value'] for h in latest_headers if h['name'].lower() == 'from'), 'Unknown Sender')
        to_header = next((h['value'] for h in latest_headers if h['name'].lower() == 'to'), '')
        subject = next((h['value'] for h in latest_headers if h['name'].lower() == 'subject'), 'No Subject')
        date_str = next((h['value'] for h in latest_headers if h['name'].lower() == 'date'), None)
        snippet = latest_msg.get('snippet', '')

        # Filter A: Skip if you were the last one to reply
        if my_email and my_email in sender.lower():
            # If you replied, clear the alert label if present so it's clean for future emails
            if alert_label_id and alert_label_id in latest_labels:
                gmail_service.users().threads().modify(
                    userId='me', id=thread_id, body={'removeLabelIds': [alert_label_id]}
                ).execute()
            continue

        # Filter B: Skip if you're not in the To: field
        if my_email and my_email not in to_header.lower():
            continue

        # Filter C: Skip if an alert was already sent for this unanswered thread
        if alert_label_id and alert_label_id in latest_labels:
            continue

        # Filter D: Check if waiting time exceeds 30 minutes
        if date_str:
            try:
                email_time = email.utils.parsedate_to_datetime(date_str)
                if email_time.tzinfo is None:
                    email_time = email_time.replace(tzinfo=datetime.timezone.utc)
                else:
                    email_time = email_time.astimezone(datetime.timezone.utc)
                
                time_diff = now - email_time
                minutes_unanswered = time_diff.total_seconds() / 60
                
                # Triggers on anything pending older than 30 minutes (yesterday, earlier today, etc.)
                if minutes_unanswered >= 30:
                    sent = send_slack_block_message(
                        slack_client, 
                        slack_channel, 
                        sender, 
                        subject, 
                        snippet, 
                        int(minutes_unanswered), 
                        thread_id
                    )
                    # Mark with label so it doesn't alert again every 10 mins
                    if sent and alert_label_id:
                        gmail_service.users().threads().modify(
                            userId='me', id=thread_id, body={'addLabelIds': [alert_label_id]}
                        ).execute()
                        
            except Exception as e:
                print(f"Skipping date parse on thread {thread_id}: {e}")

if __name__ == '__main__':
    check_unanswered_emails()
