import os
import datetime
import json
import email.utils
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Kept strictly to readonly so your existing token.json works without re-authenticating
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
    except SlackApiError as e:
        print(f"Error pushing to Slack: {e.response['error']}")

def check_unanswered_emails():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL", "#general")
    
    if not slack_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable is missing.")

    gmail_service = get_gmail_service()
    slack_client = WebClient(token=slack_token)
    
    # Fetch your authenticated email address
    user_profile = gmail_service.users().getProfile(userId='me').execute()
    my_email = user_profile.get('emailAddress', '').lower()
    
    # Query unread emails directly to you from the last 2 days (catches yesterday's backlog)
    query = "to:me is:unread newer_than:2d"
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

        latest_msg = thread_messages[-1]
        latest_payload = latest_msg.get('payload', {})
        latest_headers = latest_payload.get('headers', [])

        sender = next((h['value'] for h in latest_headers if h['name'].lower() == 'from'), 'Unknown Sender')
        to_header = next((h['value'] for h in latest_headers if h['name'].lower() == 'to'), '')
        subject = next((h['value'] for h in latest_headers if h['name'].lower() == 'subject'), 'No Subject')
        date_str = next((h['value'] for h in latest_headers if h['name'].lower() == 'date'), None)
        snippet = latest_msg.get('snippet', '')

        # Filter A: Skip if you already sent the last message
        if my_email and my_email in sender.lower():
            continue

        # Filter B: Skip if you are not directly addressed in the 'To:' field
        if my_email and my_email not in to_header.lower():
            continue

        # Filter C: Check if waiting time is greater than 30 minutes
        if date_str:
            try:
                email_time = email.utils.parsedate_to_datetime(date_str)
                if email_time.tzinfo is None:
                    email_time = email_time.replace(tzinfo=datetime.timezone.utc)
                else:
                    email_time = email_time.astimezone(datetime.timezone.utc)
                
                time_diff = now - email_time
                minutes_unanswered = time_diff.total_seconds() / 60
                
                # Alerts for any email older than 30 minutes (yesterday or today)
                if minutes_unanswered >= 30:
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
    check_unanswered_emails()
