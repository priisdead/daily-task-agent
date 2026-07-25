"""One-time Gmail OAuth setup — run ONCE PER GMAIL ACCOUNT, on your own
computer (it opens a browser). Then copy the token files to the server.

Prerequisites (once, shared by all accounts):
1. console.cloud.google.com -> create a project -> enable "Gmail API".
2. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
   -> Application type: Desktop app. Download the JSON as credentials.json
   and place it next to this script.
3. OAuth consent screen: add EVERY Gmail address you want to scan as a
   test user.

Usage (example for 4 accounts):
    pip install google-auth-oauthlib google-api-python-client
    python scripts/gmail_auth.py sales      # log in as sales@...
    python scripts/gmail_auth.py support    # log in as support@...
    python scripts/gmail_auth.py accounts   # log in as accounts@...
    python scripts/gmail_auth.py personal   # log in as your own address

Each run opens a browser — sign in with THAT account and approve read-only
access. Tokens are written to data/gmail_<name>.json. Then set in .env:

    GMAIL_TOKEN_FILES=./data/gmail_sales.json,./data/gmail_support.json,./data/gmail_accounts.json,./data/gmail_personal.json
"""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

HERE = Path(__file__).resolve().parent
CREDENTIALS = HERE / "credentials.json"


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "token"
    out = HERE.parent / "data" / f"gmail_{name}.json"
    if not CREDENTIALS.exists():
        raise SystemExit(
            f"Put your OAuth client file at {CREDENTIALS} first "
            "(Google Cloud Console -> Credentials -> Desktop app)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(creds.to_json())
    print(f"Token saved to {out}.")
    print("Copy it to the server and add its path to GMAIL_TOKEN_FILES in .env.")


if __name__ == "__main__":
    main()
