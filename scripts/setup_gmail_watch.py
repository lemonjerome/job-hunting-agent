"""
Setup (and renew) Gmail Watch API for push notifications via Cloud Pub/Sub.

Gmail Watch expires every 7 days. This script registers (or renews) the watch
so Gmail publishes inbox changes to the Pub/Sub topic.

Usage:
  python scripts/setup_gmail_watch.py

Called by:
  - One-time during initial GCloud setup
  - Daily by Cloud Scheduler via POST /renew-watch on the agent server

Required env vars / config:
  GMAIL_CREDENTIALS — path to OAuth credentials JSON (for local run)
  GCP_PROJECT       — Google Cloud project ID
  PUBSUB_TOPIC      — Pub/Sub topic name (default: gmail-job-alerts)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import GMAIL_CREDENTIALS

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / ".gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

GCP_PROJECT  = os.environ.get("GCP_PROJECT", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "gmail-job-alerts")


def _get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def setup_watch() -> str:
    """
    Register or renew the Gmail Watch on the inbox.
    Returns the expiration timestamp string.
    """
    if not GCP_PROJECT:
        raise ValueError(
            "GCP_PROJECT env var is required. "
            "Set it to your Google Cloud project ID."
        )

    topic_name = f"projects/{GCP_PROJECT}/topics/{PUBSUB_TOPIC}"
    service = _get_service()

    response = service.users().watch(
        userId="me",
        body={
            "topicName": topic_name,
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        },
    ).execute()

    expiration = response.get("expiration", "unknown")
    history_id = response.get("historyId", "unknown")
    print(f"[setup_gmail_watch] Watch registered.")
    print(f"  Topic:      {topic_name}")
    print(f"  HistoryId:  {history_id}")
    print(f"  Expires at: {expiration} (ms since epoch)")
    return str(expiration)


if __name__ == "__main__":
    setup_watch()
