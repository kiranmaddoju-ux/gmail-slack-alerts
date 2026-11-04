import os
import datetime
import json
import email.utils
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

def load_active_projects(file_path="active_projects.txt"):
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]

def send_slack_block_message(client, channel, sender, subject, snippet, mins, thread_id, project_tag):
    gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
    time_display = f"{mins} mins" if mins < 60 else f"{mins // 60}h {mins % 60}m"
    
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🚨 *Active Project Update Waiting!* [`{project_tag}`] (Overdue by *{time_display}*)"}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n{sender}"},
                {"type": "mrkdwn", "text": f"*Project:*\n`{project_tag}`"}
            ]
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Subject:*\n{subject}\n*Preview:*\n>{snippet}"}
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
        client.chat_postMessage(channel=channel, blocks=blocks, text=f"Action Required on {project_tag} from {sender}")
        print(f"--> [SUCCESS] Sent Slack alert for thread {thread_id} ({project_tag})")
    except SlackApiError as e:
        print(f"--> [ERROR] Slack send failed: {e.response['error']}")

def check_unanswered_emails():
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL", "#general")
    
    if not slack_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable is missing.")

    gmail_service = get_gmail_service()
    slack_client = WebClient(token=slack_token)
    
    user_profile = gmail_service.users().getProfile(userId='me').execute()
    my_email = user_profile.get('emailAddress', '').lower()
    my_first_name = my_email.split('@')[0].split('.')[0].lower()
    print(f"Authenticated as: {my_email}")
    
    active_projects = load_active_projects()
    print(f"Tracking {len(active_projects)} active projects: {active_projects}")

    # Query recent emails from the last 2 days
    query = "to:me newer_than:2d"
    results = gmail_service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    print(f"Found {len(messages)} recent messages matching 'to:me newer_than:2d'")
    
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

        # 1. Skip if you already sent the latest reply
        if my_email and my_email in sender.lower():
            continue

        # 2. Check if matched to any active project ID
        search_target = f"{subject} {snippet}".upper()
        matched_project = None
        for prj in active_projects:
            if prj in search_target:
                matched_project = prj
                break
        
        if not matched_project:
            continue

        # 3. Direct check: In 'To' field OR first name in snippet
        is_direct = (my_email in to_header.lower()) or (my_first_name and my_first_name in snippet.lower())
        if not is_direct:
            print(f"Skipping thread {thread_id} ({matched_project}): not directly addressed in To/body.")
            continue

        # 4. Check time elapsed
        if date_str:
            try:
                email_time = email.utils.parsedate_to_datetime(date_str)
                if email_time.tzinfo is None:
                    email_time = email_time.replace(tzinfo=datetime.timezone.utc)
                else:
                    email_time = email_time.astimezone(datetime.timezone.utc)
                
                time_diff = now - email_time
                minutes_unanswered = time_diff.total_seconds() / 60
                
                print(f"Found active project email! ID: {matched_project} | Subject: '{subject[:40]}...' | Waiting: {int(minutes_unanswered)} mins")

                # Alerts if waiting more than 30 minutes (covers from 30 mins up to 24 hours ago)
                if 30 <= minutes_unanswered <= 1440:
                    send_slack_block_message(
                        slack_client, 
                        slack_channel, 
                        sender, 
                        subject, 
                        snippet, 
                        int(minutes_unanswered), 
                        thread_id,
                        matched_project
                    )
                else:
                    print(f"Skipping alert: {int(minutes_unanswered)} mins is outside the window (requires >= 30 mins).")
            except Exception as e:
                print(f"Error parsing date on thread {thread_id}: {e}")

if __name__ == '__main__':
    check_unanswered_emails()
