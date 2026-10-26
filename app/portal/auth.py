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
FAILURE_WINDOW_S = 15 * 60
# Wrong passwords: 5 per email from one address, 20 per address in total. Never per email alone, so a stranger
# guessing from elsewhere can't lock the real owner out. (Behind a proxy, run uvicorn with --proxy-headers.)
MAX_FAILURES_PAIR = 5
MAX_FAILURES_IP = 20
MAX_SIGNUPS_PER_IP = 5  # new businesses per address per hour
SIGNUP_WINDOW_S = 60 * 60


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


def _recent_failures(key: str, window_s: int = FAILURE_WINDOW_S) -> int:
    cutoff = time.monotonic() - window_s
    q = _failures.get(key)
    while q and q[0] < cutoff:
        q.popleft()
    if q is not None and not q:
        del _failures[key]  # keep memory bounded however many emails are tried
    return len(q or ())


def _too_many(*limits: tuple[str, int], window_s: int = FAILURE_WINDOW_S) -> bool:
    with _fail_lock:
        return any(_recent_failures(k, window_s) >= n for k, n in limits)


def _record_failure(*keys: str) -> None:
    with _fail_lock:
        for k in keys:
            _failures[k].append(time.monotonic())


def reset_failures() -> None:
    with _fail_lock:
        _failures.clear()


# ---- accounts ----

def register(business_name: str, email: str, password: str, confirm: str, now: datetime, ip: str = "?") -> int:
    """Create a business + its login. Returns the account id. Raises AuthError with a friendly message."""
    name, email = business_name.strip(), email.strip().lower()
    if _too_many((f"signup:{ip}", MAX_SIGNUPS_PER_IP), window_s=SIGNUP_WINDOW_S):
        raise AuthError("Too many new accounts from your network. Please try again in an hour.")
    if not 2 <= len(name) <= 120:
        raise AuthError("Please enter your business name.")
    if len(email) > 160 or not EMAIL_RE.match(email):
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
        account_id = account.id
    _record_failure(f"signup:{ip}")  # counts sign-ups, not failures
    return account_id


def login(email: str, password: str, ip: str, now: datetime) -> int:
    email = email.strip().lower()
    pair, addr = f"pair:{ip}:{email[:160]}", f"ip:{ip}"
    if _too_many((pair, MAX_FAILURES_PAIR), (addr, MAX_FAILURES_IP)):
        raise AuthError("Too many attempts. Please wait a few minutes and try again.")
    with session_scope() as s:
        account = s.scalar(select(Account).where(Account.email == email))
        if account is None or not verify_password(password, account.password_hash):
            _record_failure(pair, addr)
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
        account.session_version += 1  # sign out every existing session


# ---- sessions: the cookie holds the account id + its session version ----

def start_session(request: Request, account_id: int) -> None:
    with session_scope() as s:
        version = s.get(Account, account_id).session_version
    request.session.clear()
    request.session.update({"account_id": account_id, "sv": version})


def end_session(request: Request) -> None:
    """Log out: bump the version so this cookie (and any copy of it) no longer works."""
    account_id = request.session.get("account_id")
    if account_id:
        with session_scope() as s:
            account = s.get(Account, account_id)
            if account is not None and account.session_version == request.session.get("sv"):
                account.session_version += 1
    request.session.clear()


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


def session_business(request: Request) -> Business | None:
    """The business of a still-valid session cookie (clears a stale one)."""
    account_id = request.session.get("account_id")
    if account_id:
        with session_scope() as s:
            account = s.get(Account, account_id)
            if account is not None and account.session_version == request.session.get("sv"):
                tenant = s.get(Tenant, account.tenant_id)
                cfg = get_tenants().get(tenant.slug) if tenant and tenant.status == "active" else None
                if cfg is not None:
                    return Business(account.id, account.email, cfg, tenant.id)
    request.session.clear()
    return None


def current_business(request: Request) -> Business:
    portal_available()
    same_origin(request)
    biz = session_business(request)
    if biz is not None:
        return biz
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": f"/app/login?next={quote(target)}"})
