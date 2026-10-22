"""Admin access: HTTP Basic auth + a same-origin check on form posts (basic-auth credentials are sent
automatically by browsers, so a cross-site form could otherwise act as the admin)."""

import secrets
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings

_basic = HTTPBasic(realm="Offer engine admin")


def require_admin(request: Request, creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    st = get_settings()
    if st.public_base_url and st.admin_password == "change-me":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Set ADMIN_PASSWORD before using the dashboard on a public server.")
    ok_user = secrets.compare_digest(creds.username.encode(), st.admin_user.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), st.admin_password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password",
                            headers={"WWW-Authenticate": 'Basic realm="Offer engine admin"'})
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if urlparse(origin).netloc != request.url.netloc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Form posted from another site.")
    return creds.username
