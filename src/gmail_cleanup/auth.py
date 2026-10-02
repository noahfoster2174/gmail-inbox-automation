import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from gmail_cleanup.config import CREDENTIALS_PATH, GMAIL_SCOPES, TOKEN_PATHS


def get_service(account: str):
    """Return authenticated Gmail API service for the given account email.
    Opens browser on first run for that account.

    Args:
        account: Gmail address, e.g. 'you@gmail.com'
    """
    token_path = TOKEN_PATHS[account]
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # The OAuth browser flow blocks forever in a headless run (cron/
            # launchd), which stalls launchd's schedule indefinitely — fail
            # fast there instead of waiting on a sign-in that can't happen.
            # GMAIL_CLEANUP_ALLOW_OAUTH=1 deliberately permits the flow from
            # a non-TTY shell (supervised re-auth); cron never sets it.
            if not sys.stdin.isatty() and os.environ.get("GMAIL_CLEANUP_ALLOW_OAUTH") != "1":
                raise RuntimeError(
                    f"No valid token for {account} at {token_path} and this is a "
                    f"non-interactive run. Re-auth from a terminal: run any phase "
                    f"for {account} and complete the browser sign-in."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), GMAIL_SCOPES
            )
            print(f"\nAuthorizing account: {account}")
            print("A browser window will open. Sign in as:", account)
            # login_hint steers Google's chooser to the requested account —
            # without it, the browser's default session silently wins and the
            # identity check below rejects the token after the fact.
            creds = flow.run_local_server(port=0, login_hint=account, prompt="consent")
        token_path.write_text(creds.to_json())

    service = build("gmail", "v1", credentials=creds)

    # Guard against a token authorized as the wrong Google account (e.g. the
    # OAuth browser flow completed under a different signed-in session).
    # Without this, "all accounts" silently processes one mailbox twice.
    profile = service.users().getProfile(userId="me").execute()
    actual = profile.get("emailAddress", "").lower()
    if actual != account.lower():
        raise RuntimeError(
            f"Token {token_path.name} is authorized as {actual}, not {account}.\n"
            f"Fix: delete {token_path} and re-run any phase for {account} — "
            f"when the browser opens, sign in as {account}."
        )
    return service


def get_all_services():
    """Return dict of {account: service} for all configured accounts."""
    from gmail_cleanup.config import GMAIL_ACCOUNTS
    return {account: get_service(account) for account in GMAIL_ACCOUNTS}
