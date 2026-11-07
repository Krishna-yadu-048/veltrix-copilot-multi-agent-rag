"""Connect (or disconnect) Google Calendar. Run once; repeat if the sign-in expires."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_servers.common import env, env_path  # noqa: E402
from mcp_servers.google_calendar import SCOPES, GoogleCalendarBackend, GoogleCalendarError, save_token, token_summary  # noqa: E402

CLIENT_FILE = env_path("GOOGLE_CLIENT_SECRET_FILE", ROOT / "secrets" / "google_client_secret.json")
TOKEN_FILE = env_path("GOOGLE_TOKEN_FILE", ROOT / "secrets" / "google_token.json")


def sign_in() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_FILE.exists():
        sys.exit(f"Missing {CLIENT_FILE}.\nCreate a 'Desktop app' OAuth client in Google Cloud and save its JSON there "
                 "(README.md -> 'Google Calendar').")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="Opening your browser to sign in to Google…\n"
                                                               "If it doesn't open, visit: {url}",
                                  success_message="Connected. You can close this tab and return to the terminal.")
    save_token(TOKEN_FILE, creds.to_json())
    print(f"Saved token to {TOKEN_FILE} (keep this file private; it is git-ignored).")
    test()


def test() -> None:
    cal = GoogleCalendarBackend(TOKEN_FILE, env("GOOGLE_CALENDAR_ID", "primary"), env("TIMEZONE", "Asia/Kolkata"))
    try:
        events = cal.list_events()
    except GoogleCalendarError as e:
        sys.exit(f"Test failed: {e}")
    print(f"Test OK: calendar '{cal.calendar_id}' reachable; {len(events)} event(s) created by the assistant so far.")
    print("The running app picks this up by itself (no restart needed). Open the Calendar page to check.")


def revoke() -> None:
    import httpx
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        print("No saved token; nothing to revoke.")
        return
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    token = creds.refresh_token or creds.token
    try:
        r = httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
        print("Revoked at Google." if r.status_code == 200 else f"Google answered HTTP {r.status_code}; "
              "you can also remove access at https://myaccount.google.com/permissions")
    except httpx.HTTPError:
        print("Could not reach Google; remove access at https://myaccount.google.com/permissions")
    TOKEN_FILE.unlink()
    print(f"Deleted {TOKEN_FILE}. Calendar features stay off until you sign in again.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--revoke", action="store_true")
    a = ap.parse_args()
    if a.revoke:
        revoke()
    elif a.status:
        print(f"Sign-in file: {TOKEN_FILE}")
        print(token_summary(TOKEN_FILE))
        if TOKEN_FILE.exists():
            test()
    else:
        sign_in()
