import time

from googleapiclient.errors import HttpError

from gmail_cleanup.db import Database


def _api_call_with_retry(fn, retries=5):
    """Execute a Gmail API call with exponential backoff on transient errors."""
    for attempt in range(retries):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def _get_or_create_label(service, user_id: str, label_name: str, cache: dict) -> str:
    if label_name in cache:
        return cache[label_name]

    existing = service.users().labels().list(userId=user_id).execute()
    for label in existing.get("labels", []):
        if label["name"] == label_name:
            cache[label_name] = label["id"]
            return label["id"]

    result = service.users().labels().create(
        userId=user_id,
        body={"name": label_name,
              "labelListVisibility": "labelShow",
              "messageListVisibility": "show"}
    ).execute()
    cache[label_name] = result["id"]
    return result["id"]


def run_apply_labels(service, db: Database, account: str, user_id: str = "me"):
    """Apply Archive/* labels to all KEEP emails for this account in Gmail."""
    keepers = [e for e in db.get_by_classification("KEEP")
               if e["account"] == account]
    print(f"Applying labels to {len(keepers)} emails for {account}...")

    label_cache = {}
    success = 0
    for i, email in enumerate(keepers):
        label_name = f"Archive/{email['category'] or 'Misc'}"
        try:
            label_id = _get_or_create_label(service, user_id, label_name, label_cache)
            _api_call_with_retry(lambda gid=email["gmail_id"], lid=label_id: service.users().messages().modify(
                userId=user_id, id=gid,
                body={"addLabelIds": [lid]}
            ).execute())
            success += 1
        except Exception as e:
            print(f"  Warning: label failed for {email['gmail_id']}: {e}")

        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(keepers)} labeled...")
            time.sleep(0.5)

    print(f"Labels applied: {success}/{len(keepers)}")


def run_delete_junk(service, db: Database, account: str, user_id: str = "me"):
    """Move all DELETE and UNSUBSCRIBE emails to Trash for this account."""
    to_delete = (
        [e for e in db.get_by_classification("DELETE") if e["account"] == account] +
        [e for e in db.get_by_classification("UNSUBSCRIBE") if e["account"] == account]
    )
    ids = [e["gmail_id"] for e in to_delete]
    print(f"Trashing {len(ids)} emails from {account}...")

    for i in range(0, len(ids), 1000):
        batch = ids[i:i+1000]
        try:
            _api_call_with_retry(lambda b=batch: service.users().messages().batchModify(
                userId=user_id,
                body={"ids": b, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]}
            ).execute())
        except HttpError as e:
            if e.resp.status == 400:
                print(f"  Warning: batch {i//1000 + 1} had bad IDs, skipping...")
            else:
                raise
        print(f"  Trashed {min(i+1000, len(ids))}/{len(ids)}...")
        time.sleep(1)

    print("Trash complete.")


def run_create_filters(service, db: Database, account: str, user_id: str = "me"):
    """Create Gmail filters to auto-delete future emails from junk senders."""
    junk = (
        [e for e in db.get_by_classification("DELETE") if e["account"] == account] +
        [e for e in db.get_by_classification("UNSUBSCRIBE") if e["account"] == account]
    )
    senders = {e["sender"] for e in junk}
    print(f"Creating filters for {len(senders)} senders on {account}...")

    created = 0
    for sender in senders:
        try:
            service.users().settings().filters().create(
                userId=user_id,
                body={
                    "criteria": {"from": sender},
                    "action": {"removeLabelIds": ["INBOX"], "addLabelIds": ["TRASH"]}
                }
            ).execute()
            created += 1
        except HttpError as e:
            print(f"  Warning: filter failed for {sender}: {e}")
        time.sleep(0.1)

    print(f"Filters created: {created}/{len(senders)}")
