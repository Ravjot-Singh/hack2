"""Create (or reset the password of) the portal login for an existing business, e.g. the ShopKart demo.

    python scripts/create_account.py --tenant shopkart --email demo@shopkart.example

The password is typed in (not shown, not passed on the command line). Self-service businesses sign up at /app/register.
"""

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.data.generator import IST  # noqa: E402
from app.db.models import Account, Tenant  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.portal.auth import EMAIL_RE, MIN_PASSWORD  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.tenants.registry import sync_tenants  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--email", required=True)
    args = ap.parse_args()
    email = args.email.strip().lower()
    if not EMAIL_RE.match(email):
        sys.exit("That doesn't look like an email address.")
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD or password != getpass.getpass("Repeat password: "):
        sys.exit(f"Passwords must match and be at least {MIN_PASSWORD} characters.")

    with session_scope() as s:
        sync_tenants(s)
        s.flush()
        tenant = s.scalar(select(Tenant).where(Tenant.slug == args.tenant))
        if tenant is None:
            sys.exit(f"No business '{args.tenant}'.")
        account = s.scalar(select(Account).where(Account.tenant_id == tenant.id))
        if account is None:
            if s.scalar(select(Account).where(Account.email == email)):
                sys.exit("That email already belongs to another business.")
            s.add(Account(tenant_id=tenant.id, email=email, password_hash=hash_password(password),
                          created_at=datetime.now(IST)))
            print(f"Created the login for {tenant.name}.")
        else:
            account.email, account.password_hash = email, hash_password(password)
            print(f"Updated the login for {tenant.name}.")


if __name__ == "__main__":
    main()
