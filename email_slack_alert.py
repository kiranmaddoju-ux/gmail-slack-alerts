import os
import datetime
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly'][cite: 18]

def get_gmail_service():
    token_json = os.environ.get("GMAIL_TOKEN_JSON")[cite: 18]
    if not token_json:
        raise ValueError("GMAIL_TOKEN_JSON environment variable is missing.")[cite: 18]
    
    creds_data = json.loads(token_json)[cite: 18]
    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)[cite: 18]
    
    if creds and creds.expired and creds.refresh_token:[cite: 18]
        creds.refresh(Request())[cite: 18]
        
    return build('gmail', 'v1', credentials=creds)[cite: 18]

def send_slack_block_message(client, channel, sender, subject, snippet, mins, thread_id):
    gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"[cite: 19]
    
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🚨 *Needs Your Reply!* (Waiting for *{mins} mins*)"}
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
    except SlackApiError as e:
        print(f"Error pushing to Slack: {e.response['error']}")

def check_unanswered_emails():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")[cite: 18]
    slack_channel = os.environ.get("SLACK_CHANNEL", "#general")[cite: 18]
    
    if not slack_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable is missing.")[cite: 18]

    gmail_service = get_gmail_service()[cite: 18]
    slack_client = WebClient(token=slack_token)[cite: 18]
    
    # 1. Fetch your exact authenticated email address
    user_profile = gmail_service.users().getProfile(userId='me').execute()
    my_email = user_profile.get('emailAddress', '').lower()
    
    # 2. Query recent active emails
    query = "to:me newer_than:2d"
    results = gmail_service.users().messages().list(userId='me', q=query).execute()[cite: 18]
    messages = results.get('messages', [])[cite: 18]
    
    now = datetime.datetime.now(datetime.timezone.utc)[cite: 18]
    processed_threads = set()

    for msg in messages:
        thread_id = msg['threadId'][cite: 18]
        if thread_id in processed_threads:
            continue
        processed_threads.add(thread_id)

        thread_data = gmail_service.users().threads().get(userId='me', id=thread_id).execute()[cite: 18]
        thread_messages = thread_data.get('messages', [])[cite: 18]
        if not thread_messages:
            continue

        # 3. Identify the most recent message in this thread
        latest_msg = thread_messages[-1]
        latest_payload = latest_msg.get('payload', {})
        latest_headers = latest_payload.get('headers', [])

        sender = next((h['value'] for h in latest_headers if h['name'].lower() == 'from'), 'Unknown Sender')[cite: 18]
        to_header = next((h['value'] for h in latest_headers if h['name'].lower() == 'to'), '')
        subject = next((h['value'] for h in latest_headers if h['name'].lower() == 'subject'), 'No Subject')[cite: 18]
        date_str = next((h['value'] for h in latest_headers if h['name'].lower() == 'date'), None)[cite: 18]
        snippet = latest_msg.get('snippet', '')[cite: 18]

        # Condition A: Skip if YOU sent the last message (means you already replied)
        if my_email and my_email in sender.lower():
            continue

        # Condition B: Skip if the email wasn't addressed directly to you in 'To:'
        if my_email and my_email not in to_header.lower():
            continue

        # Condition C: Calculate unanswered wait time
        if date_str:
            try:
                clean_date = date_str.split(' (')[0].strip()[cite: 19]
                try:
                    email_time = datetime.datetime.strptime(clean_date, '%a, %d %b %Y %H:%M:%S %z')[cite: 19]
                except ValueError:
                    email_time = datetime.datetime.strptime(clean_date, '%d %b %Y %H:%M:%S %z')[cite: 19]
                
                time_diff = now - email_time[cite: 19]
                minutes_unanswered = time_diff.total_seconds() / 60[cite: 19]
                
                # Condition D: Trigger alert only around the 30-minute overdue mark
                if 30 <= minutes_unanswered <= 45:
                    send_slack_block_message(
                        slack_client, 
                        slack_channel, 
                        sender, 
                        subject, 
                        snippet, 
                        int(minutes_unanswered), 
                        thread_id
                    )
            except Exception as e:
                print(f"Skipping date parse on thread {thread_id}: {e}")

if __name__ == '__main__':
    check_unanswered_emails()[cite: 19]
