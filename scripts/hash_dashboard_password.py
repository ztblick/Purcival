"""Generate a PBKDF2 dashboard password hash for .env."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.auth import hash_dashboard_password


def main() -> None:
    password = getpass.getpass("Dashboard password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords did not match.")
    print(hash_dashboard_password(password))


if __name__ == "__main__":
    main()
