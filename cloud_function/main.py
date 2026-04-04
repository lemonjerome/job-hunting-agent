"""
Cloud Run Function — Gmail Push Notification Trigger

Triggered by Cloud Pub/Sub whenever Gmail receives a new email.
Checks if the sender is a job alert address. If yes, fetches the full
email and forwards it to the Cloud Run agent for processing.

Environment variables:
  AGENT_CLOUD_RUN_URL  — URL of the Cloud Run job-agent service
  GMAIL_TOKEN_SECRET   — Secret Manager secret name for the Gmail OAuth token JSON
  GCP_PROJECT          — Google Cloud project ID

Pub/Sub message format (from Gmail Watch API):
  {
    "emailAddress": "user@gmail.com",
    "historyId": "12345"
  }
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import functions_framework
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.cloud import secretmanager

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AGENT_URL = os.environ.get("AGENT_CLOUD_RUN_URL", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GMAIL_TOKEN_SECRET = os.environ.get("GMAIL_TOKEN_SECRET", "gmail-oauth-token")
HISTORY_ID_SECRET = os.environ.get("HISTORY_ID_SECRET", "gmail-history-id")

EMAIL_SENDERS: dict[str, str] = {
    "linkedin":  "jobalerts-noreply@linkedin.com",
    "jobstreet": "noreply@e.jobstreet.com",
    "glassdoor": "noreply@glassdoor.com",
    "indeed":    "donotreply@match.indeed.com",
}

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_gmail_service():
    """Build a Gmail API service using OAuth credentials from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{GCP_PROJECT}/secrets/{GMAIL_TOKEN_SECRET}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    token_json = response.payload.data.decode("utf-8")

    creds = Credentials.from_authorized_user_info(json.loads(token_json), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Update secret with refreshed token
        _update_secret(token_json=creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _update_secret(token_json: str) -> None:
    """Overwrite the Gmail token secret with refreshed credentials."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{GCP_PROJECT}/secrets/{GMAIL_TOKEN_SECRET}"
        client.add_secret_version(
            request={"parent": parent, "payload": {"data": token_json.encode()}}
        )
    except Exception as e:
        print(f"[gmail-trigger] Warning: could not update token secret: {e}")


# ---------------------------------------------------------------------------
# HistoryId persistence (survives cold starts)
# ---------------------------------------------------------------------------

def _load_history_id() -> str | None:
    """Read the last processed historyId from Secret Manager."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{GCP_PROJECT}/secrets/{HISTORY_ID_SECRET}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        value = response.payload.data.decode("utf-8").strip()
        return value or None
    except Exception as e:
        print(f"[gmail-trigger] Warning: could not load historyId: {e}")
        return None


def _save_history_id(history_id: str) -> None:
    """Persist the current historyId to Secret Manager for the next invocation."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{GCP_PROJECT}/secrets/{HISTORY_ID_SECRET}"
        client.add_secret_version(
            request={"parent": parent, "payload": {"data": history_id.encode()}}
        )
    except Exception as e:
        print(f"[gmail-trigger] Warning: could not save historyId: {e}")


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _identify_site(sender_addr: str) -> str | None:
    sender_lower = sender_addr.lower()
    for site, addr in EMAIL_SENDERS.items():
        if addr.lower() in sender_lower:
            return site
    return None


def _extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _extract_html_body(payload: dict) -> str:
    """Recursively extract the first text/html body part."""
    import base64 as _b64
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")

    if mime == "text/html" and data:
        return _b64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime == "text/plain" and data:
        plain = _b64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return f"<pre>{plain}</pre>"

    for part in payload.get("parts", []):
        result = _extract_html_body(part)
        if result:
            return result
    return ""


def _parse_sender(from_header: str) -> str:
    import re
    match = re.search(r"<([^>]+)>", from_header)
    return match.group(1) if match else from_header.strip()


def _get_new_messages(service, history_id: str, start_history_id: str) -> list[dict]:
    """Fetch messages added since start_history_id via Gmail History API."""
    messages = []
    try:
        resp = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
        ).execute()
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                full = service.users().messages().get(
                    userId="me", id=msg_id, format="full",
                ).execute()
                messages.append(full)
    except Exception as e:
        print(f"[gmail-trigger] History API error: {e}")
    return messages


# ---------------------------------------------------------------------------
# Cloud Run Function entry point
# ---------------------------------------------------------------------------

@functions_framework.http
def handle_gmail_notification(request):
    """
    HTTP Cloud Function triggered by Gmail Watch via Pub/Sub push subscription.

    Pub/Sub wraps the Gmail notification in a base64 envelope:
      {
        "message": {
          "data": "<base64({emailAddress, historyId})>",
          "messageId": "...",
          ...
        },
        "subscription": "..."
      }
    """
    # -- Decode Pub/Sub envelope --
    try:
        envelope = request.get_json(silent=True) or {}
        raw_data = envelope.get("message", {}).get("data", "")
        notification = json.loads(base64.b64decode(raw_data).decode("utf-8"))
    except Exception as e:
        print(f"[gmail-trigger] Failed to decode Pub/Sub message: {e}")
        return "Bad Request", 400

    history_id: str = str(notification.get("historyId", ""))
    if not history_id:
        return "OK", 200  # Nothing to do

    # Load the last known historyId from Secret Manager (survives cold starts).
    # Falls back to history_id - 1 only on very first run when no secret exists yet.
    start_id = _load_history_id() or str(int(history_id) - 1)
    print(f"[gmail-trigger] Processing historyId={history_id}, start={start_id}")

    # Persist current historyId immediately so the next invocation knows where to resume,
    # even if this invocation crashes mid-way.
    _save_history_id(history_id)

    if not AGENT_URL:
        print("[gmail-trigger] AGENT_CLOUD_RUN_URL not set — skipping.")
        return "OK", 200

    # -- Fetch new messages --
    try:
        service = _get_gmail_service()
    except Exception as e:
        print(f"[gmail-trigger] Gmail auth failed: {e}")
        return "Internal Error", 500

    messages = _get_new_messages(service, history_id, start_id)
    print(f"[gmail-trigger] {len(messages)} new message(s) since historyId={start_id}")

    dispatched = 0
    for msg in messages:
        headers = msg.get("payload", {}).get("headers", [])
        from_header = _extract_header(headers, "From")
        sender = _parse_sender(from_header)
        site = _identify_site(sender)

        if not site:
            print(f"[gmail-trigger] Skipping non-job-alert sender: {sender!r}")
            continue  # Not a job alert sender — ignore

        subject = _extract_header(headers, "Subject")
        date_header = _extract_header(headers, "Date")
        html_body = _extract_html_body(msg.get("payload", {}))

        payload = {
            "message_id":    msg["id"],
            "site":          site,
            "sender":        sender,
            "subject":       subject,
            "html_body":     html_body,
            "time_received": date_header,
        }

        try:
            # Cloud Run requires a Google-signed identity token for authenticated requests.
            # Fetch a token from the GCE metadata server targeting the Cloud Run URL as audience.
            token_resp = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
                params={"audience": AGENT_URL.rstrip("/")},
                headers={"Metadata-Flavor": "Google"},
                timeout=5,
            )
            id_token = token_resp.text.strip()
            resp = requests.post(
                AGENT_URL.rstrip("/") + "/process",
                json=payload,
                headers={"Authorization": f"Bearer {id_token}"},
                timeout=530,  # Cloud Function timeout is 540s — give Cloud Run 530s to respond
            )
            print(
                f"[gmail-trigger] Dispatched {site} email '{subject}' → "
                f"agent responded {resp.status_code}"
            )
            dispatched += 1
        except Exception as e:
            print(f"[gmail-trigger] Failed to call agent for {site}: {e}")

    print(f"[gmail-trigger] Done. {dispatched} email(s) dispatched to agent.")
    return "OK", 200
