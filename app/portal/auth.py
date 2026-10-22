"""Business accounts: register, log in, session lookup, and the guards every portal request goes through."""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Account, Tenant
from app.db.session import session_scope
from app.rules.policy import TenantPolicy, current_policy
from app.security import hash_password, insecure_defaults, verify_password
from app.tenants.registry import TenantConfig, create_tenant, get_tenants

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60


@dataclass
class Business:
    account_id: int
    email: str
    tenant: TenantConfig
    tenant_id: int

    @property
    def policy(self) -> TenantPolicy:
        return current_policy(self.tenant.slug, self.tenant.path)


class AuthError(ValueError):
    pass


# ---- brute-force protection (per process; enough for one web instance) ----
_fail_lock = threading.Lock()
_failures: dict[str, deque] = defaultdict(deque)


def _recent_failures(key: str) -> int:
    cutoff = time.monotonic() - FAILURE_WINDOW_S
    q = _failures[key]
    while q and q[0] < cutoff:
        q.popleft()
    return len(q)


def _too_many(*keys: str) -> bool:
    with _fail_lock:
        return any(_recent_failures(k) >= MAX_FAILURES for k in keys)


def _record_failure(*keys: str) -> None:
    with _fail_lock:
        for k in keys:
            _failures[k].append(time.monotonic())


def reset_failures() -> None:
    with _fail_lock:
        _failures.clear()


# ---- accounts ----

def register(business_name: str, email: str, password: str, confirm: str, now: datetime) -> int:
    """Create a business + its login. Returns the account id. Raises AuthError with a friendly message."""
    name, email = business_name.strip(), email.strip().lower()
    if not 2 <= len(name) <= 120:
        raise AuthError("Please enter your business name.")
    if not EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters.")
    if password != confirm:
        raise AuthError("The two passwords don't match.")
    with session_scope() as s:
        if s.scalar(select(func.count()).select_from(Account).where(Account.email == email)):
            raise AuthError("An account with this email already exists. Try logging in.")
        tenant = create_tenant(s, name)
        account = Account(tenant_id=tenant.id, email=email, password_hash=hash_password(password), created_at=now)
        s.add(account)
        s.flush()
        return account.id


def login(email: str, password: str, ip: str, now: datetime) -> int:
    email = email.strip().lower()
    keys = (f"ip:{ip}", f"email:{email}")
    if _too_many(*keys):
        raise AuthError("Too many attempts. Please wait a few minutes and try again.")
    with session_scope() as s:
        account = s.scalar(select(Account).where(Account.email == email))
        if account is None or not verify_password(password, account.password_hash):
            _record_failure(*keys)
            raise AuthError("Wrong email or password.")
        tenant = s.get(Tenant, account.tenant_id)
        if tenant is None or tenant.status != "active":
            raise AuthError("This business account is not active.")
        account.last_login_at = now
        return account.id


def set_password(email: str, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters.")
    with session_scope() as s:
        account = s.scalar(select(Account).where(Account.email == email.strip().lower()))
        if account is None:
            raise AuthError("No account with that email.")
        account.password_hash = hash_password(password)


# ---- request guards ----

def portal_available() -> None:
    st = get_settings()
    bad = insecure_defaults()
    if st.public_base_url and bad:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"The portal is not configured yet: set {', '.join(b.upper() for b in bad)}.")


def same_origin(request: Request) -> None:
    """Refuse form posts from other sites (session cookies are sent automatically by browsers)."""
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if urlparse(origin).netloc != request.url.netloc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Form posted from another site.")


def current_business(request: Request) -> Business:
    portal_available()
    same_origin(request)
    account_id = request.session.get("account_id")
    if account_id:
        with session_scope() as s:
            account = s.get(Account, account_id)
            if account is not None:
                tenant = s.get(Tenant, account.tenant_id)
                cfg = get_tenants().get(tenant.slug) if tenant and tenant.status == "active" else None
                if cfg is not None:
                    return Business(account.id, account.email, cfg, tenant.id)
        request.session.clear()
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": f"/app/login?next={quote(target)}"})
