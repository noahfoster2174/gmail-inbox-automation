import re
import time
from email.utils import parsedate_to_datetime

from gmail_cleanup.db import Database

METADATA_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe", "Precedence"]
BATCH_SIZE = 50


def extract_header(headers: list, name: str) -> str | None:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def extract_sender(from_value: str) -> str:
    match = re.search(r'<([^>]+)>', from_value)
    if match:
        return match.group(1).strip().lower()
    return from_value.strip().lower()


def extract_domain(email: str) -> str:
    parts = email.split("@")
    return parts[1] if len(parts) == 2 else email


def _has_attachments(payload: dict) -> bool:
    return any(p.get("filename") for p in payload.get("parts", []))


def parse_message_metadata(raw: dict, account: str) -> dict:
    headers = raw.get("payload", {}).get("headers", [])
    from_value = extract_header(headers, "From") or ""
    sender = extract_sender(from_value)

    raw_date = extract_header(headers, "Date") or ""
    try:
        date = parsedate_to_datetime(raw_date).isoformat()
    except Exception:
        date = raw_date

    return {
        "gmail_id": raw["id"],
        "thread_id": raw.get("threadId"),
        "sender": sender,
        "sender_domain": extract_domain(sender),
        "subject": extract_header(headers, "Subject") or "(no subject)",
        "date": date,
        "is_read": "UNREAD" not in raw.get("labelIds", []),
        "has_attachments": _has_attachments(raw.get("payload", {})),
        "body_preview": raw.get("snippet", "")[:200],
        "list_unsubscribe": extract_header(headers, "List-Unsubscribe"),
        "precedence": extract_header(headers, "Precedence"),
        "account": account,
    }


def fetch_all_metadata(service, db: Database, account: str, user_id: str = "me"):
    """Fetch metadata for all messages in this account and store in DB.
    Skips already-indexed messages (safe to resume)."""
    print(f"\nFetching message list for {account}...")
    page_token = None
    all_ids = []

    while True:
        kwargs = {"userId": user_id, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        all_ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        print(f"  Listed {len(all_ids)} messages...", end="\r")
        if not page_token:
            break

    print(f"\n  Total: {len(all_ids)} messages. Already indexed: {db.count_emails()}")

    fetched = 0
    errors = 0
    for i in range(0, len(all_ids), BATCH_SIZE):
        batch_ids = all_ids[i:i + BATCH_SIZE]
        batch = service.new_batch_http_request()
        results = []

        def make_callback(r):
            def callback(request_id, response, exception):
                if exception is None:
                    r.append(response)
            return callback

        for msg_id in batch_ids:
            batch.add(
                service.users().messages().get(
                    userId=user_id, id=msg_id,
                    format="metadata", metadataHeaders=METADATA_HEADERS,
                ),
                callback=make_callback(results)
            )

        try:
            batch.execute()
        except Exception:
            errors += 1
            time.sleep(2)
            continue

        for raw in results:
            db.upsert_email(parse_message_metadata(raw, account))

        fetched += len(results)
        if fetched % 500 == 0 or fetched == len(all_ids):
            print(f"  Indexed {fetched}/{len(all_ids)}...", end="\r")
        time.sleep(0.2)

    print(f"\n  Done. {db.count_emails()} total emails in DB. Errors: {errors}")
