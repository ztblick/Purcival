"""
Google OAuth2 authentication — shared by all Google API tools.

Handles the full OAuth2 lifecycle:
    - First-time authorization via browser (interactive, run once)
    - Credential storage per persona
    - Automatic token refresh
    - Scope management (start read-only, upgrade later)

Credentials are stored at data/<persona>/google_credentials.json.
The client secret file (google_client_secret.json) lives in the
project root and is shared across all personas.

Usage:
    # First-time auth (run interactively from terminal):
    python -c "from google_auth import run_auth_flow; run_auth_flow('jo')"

    # In code (tools, agent, etc.):
    from google_auth import get_credentials
    creds = get_credentials('jo')
    # creds is a google.oauth2.credentials.Credentials object
"""

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# --- Paths ---

PROJECT_ROOT = Path(__file__).parent
CLIENT_SECRET_PATH = PROJECT_ROOT / "google_client_secret.json"
DATA_DIR = PROJECT_ROOT / "data"


def _credentials_path(persona_name: str) -> Path:
    """Path to a persona's stored Google credentials."""
    return DATA_DIR / persona_name / "google_credentials.json"


# --- Scopes ---

# Start with read-only. When write access is needed, add the write
# scopes here and re-run the auth flow — the user will be prompted
# to grant the additional permissions.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
]


# --- Public API ---

def get_credentials(persona_name: str) -> Credentials | None:
    """
    Load and return valid Google credentials for a persona.

    Handles automatic token refresh. Returns None if no credentials
    exist (auth flow hasn't been run yet). Raises an exception if
    credentials exist but can't be refreshed (user needs to re-auth).

    Args:
        persona_name: The persona whose credentials to load.

    Returns:
        A valid Credentials object, or None if not yet authorized.
    """
    creds_path = _credentials_path(persona_name)

    if not creds_path.exists():
        logger.info(
            f"No Google credentials for '{persona_name}'. "
            f"Run the auth flow to set up calendar access."
        )
        return None

    creds = Credentials.from_authorized_user_file(str(creds_path), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save the refreshed credentials
            creds_path.write_text(creds.to_json())
            logger.info(f"Google credentials refreshed for '{persona_name}'")
            return creds
        except Exception as e:
            raise RuntimeError(
                f"Failed to refresh Google credentials for '{persona_name}': {e}. "
                f"Re-run the auth flow: "
                f'python -c "from google_auth import run_auth_flow; '
                f"run_auth_flow('{persona_name}')\""
            )

    raise RuntimeError(
        f"Google credentials for '{persona_name}' are invalid and cannot "
        f"be refreshed. Re-run the auth flow."
    )


def run_auth_flow(persona_name: str):
    """
    Run the interactive OAuth2 authorization flow.

    Opens a browser window for the user to sign in and grant
    calendar access. Saves the resulting credentials (including
    the refresh token) to the persona's data directory.

    This is meant to be run once from the terminal, not from
    the bot or agent loop.

    Args:
        persona_name: The persona to authorize.
    """
    if not CLIENT_SECRET_PATH.exists():
        print(
            f"\nError: {CLIENT_SECRET_PATH} not found.\n\n"
            f"Download it from the Google Cloud Console:\n"
            f"  1. Go to APIs & Services → Credentials\n"
            f"  2. Click on your OAuth client → Download JSON\n"
            f"  3. Save it as: {CLIENT_SECRET_PATH}\n"
        )
        return

    print(f"\nStarting Google Calendar authorization for '{persona_name}'...")
    print("A browser window will open. Sign in and grant access.\n")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
    )

    # Run the local server flow — opens browser, handles the callback
    creds = flow.run_local_server(port=0)

    # Save credentials
    creds_path = _credentials_path(persona_name)
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(creds.to_json())

    print(f"\nCredentials saved to: {creds_path}")

    # Quick verification: list calendars
    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds)
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get("items", [])

        print(f"\nSuccess! Found {len(calendars)} calendar(s):\n")
        for cal in calendars:
            primary = " (primary)" if cal.get("primary") else ""
            role = cal.get("accessRole", "unknown")
            print(f"  • {cal['summary']}{primary} [{role}]")
        print()
    except Exception as e:
        print(f"\nCredentials saved, but verification failed: {e}")
        print("The auth flow completed — this error may resolve itself.\n")


def has_credentials(persona_name: str) -> bool:
    """Check if a persona has Google credentials on disk."""
    return _credentials_path(persona_name).exists()
