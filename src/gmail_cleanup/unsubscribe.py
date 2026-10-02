import time

from gmail_cleanup.db import Database


def parse_list_unsubscribe(header: str) -> tuple[str | None, str | None]:
    """Extract mailto and https URLs from List-Unsubscribe header."""
    mailto, https_url = None, None
    for part in header.split(","):
        part = part.strip().strip("<>")
        if part.startswith("mailto:"):
            mailto = part[7:]
        elif part.startswith("http"):
            https_url = part
    return mailto, https_url


def _unsubscribe_https(url: str) -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=10000)
            page.wait_for_load_state("networkidle", timeout=5000)

            for selector in [
                "button:has-text('Unsubscribe')", "a:has-text('Unsubscribe')",
                "input[value*='nsubscribe']", "[id*='unsubscribe']",
            ]:
                try:
                    el = page.locator(selector).first
                    if el.is_visible():
                        el.click()
                        page.wait_for_load_state("networkidle", timeout=3000)
                        browser.close()
                        return True
                except Exception:
                    continue

            browser.close()
            return False
    except Exception as e:
        print(f"    https unsubscribe failed: {e}")
        return False


def run_unsubscribe(db: Database, account: str):
    """Process all UNSUBSCRIBE-classified emails for this account."""
    to_unsub = [e for e in db.get_by_classification("UNSUBSCRIBE")
                if e["account"] == account]

    # One unsubscribe attempt per unique sender
    seen: set = set()
    unique = [e for e in to_unsub
              if e["sender"] not in seen
              and e["list_unsubscribe"]
              and not seen.add(e["sender"])]  # type: ignore

    print(f"Unsubscribing from {len(unique)} senders on {account}...")
    success = 0

    for email in unique:
        print(f"  {email['sender']}")
        mailto, https_url = parse_list_unsubscribe(email["list_unsubscribe"])

        done = False
        if https_url:
            done = _unsubscribe_https(https_url)
        if not done and mailto:
            print(f"    mailto unsubscribe: {mailto}")
            done = True

        if done:
            success += 1
        time.sleep(1)

    print(f"Unsubscribe complete: {success}/{len(unique)}")
