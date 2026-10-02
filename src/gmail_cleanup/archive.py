import base64
import re
import time
from pathlib import Path

from gmail_cleanup.config import ARCHIVE_DIR
from gmail_cleanup.db import Database


def sanitize_filename(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s)
    s = s.strip(". ")
    return s[:80]


def make_archive_path(base_dir: Path, category: str | None, date: str,
                      subject: str, sender: str) -> Path:
    category = category or "Misc"
    folder = base_dir
    for part in category.split("/"):
        folder = folder / part

    date_prefix = str(date)[:10] if date else "0000-00-00"
    safe_subject = sanitize_filename(subject)
    safe_sender = sanitize_filename(sender.split("@")[0])
    return folder / f"{date_prefix}_{safe_subject}_{safe_sender}.eml"


def download_email_as_eml(service, gmail_id: str, user_id: str = "me") -> bytes | None:
    try:
        response = service.users().messages().get(
            userId=user_id, id=gmail_id, format="raw"
        ).execute()
        return base64.urlsafe_b64decode(response.get("raw", "") + "==")
    except Exception as e:
        print(f"  Warning: failed to download {gmail_id}: {e}")
        return None


def run_archive(service, db: Database, account: str, user_id: str = "me"):
    """Download all KEEP emails for this account to local archive."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    keepers = [e for e in db.get_by_classification("KEEP")
               if e["account"] == account]
    print(f"Archiving {len(keepers)} emails from {account} to {ARCHIVE_DIR}...")

    success = 0
    for i, email in enumerate(keepers):
        path = make_archive_path(
            ARCHIVE_DIR, email["category"], email["date"],
            email["subject"], email["sender"]
        )

        if path.exists():
            success += 1
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        raw = download_email_as_eml(service, email["gmail_id"], user_id)
        if raw:
            path.write_bytes(raw)
            success += 1

        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(keepers)} archived...")
            time.sleep(0.5)

    print(f"Archive complete: {success}/{len(keepers)} emails saved.")
