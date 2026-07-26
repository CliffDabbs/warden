"""Basic auth — a static username/password login gate with a long-lived cookie.

Deliberately minimal and dependency-free (stdlib hmac/hashlib):
  * one configured user (WARDEN_USERNAME / WARDEN_PASSWORD).
  * on login we set a signed session cookie. The signature uses a secret that is
    stable across restarts (WARDEN_SECRET, or derived from the credentials), so the
    cookie survives reboots and lasts ~10 years — "practically forever". Changing the
    password rotates the derived secret and invalidates old cookies.
  * the cookie carries no secret, only `username.hmac(username)`, compared in
    constant time. Fine for a personal LAN tool.

Auth is OFF unless a password is set, so Warden still runs open out of the box.
The HTTP middleware gates pages (redirect to /login) and the API/WS (401/close);
static assets and /healthz stay public.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .config import Settings

COOKIE = "warden_session"
MAX_AGE = 10 * 365 * 24 * 3600           # ~10 years
_PUBLIC_EXACT = {"/login", "/logout", "/healthz", "/favicon.ico"}
_PUBLIC_PREFIX = ("/static/",)


class Auth:
    def __init__(self, settings: Settings) -> None:
        self.username = settings.auth_username or "admin"
        self.password = settings.auth_password or ""
        self.enabled = bool(self.password)
        base = settings.auth_secret or f"{self.username}:{self.password}"
        self._secret = hashlib.sha256(base.encode("utf-8")).digest()

    # ── token ────────────────────────────────────────────────────────────────
    def _sig(self, username: str) -> str:
        return hmac.new(self._secret, ("warden|" + username).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def make_token(self) -> str:
        return f"{self.username}.{self._sig(self.username)}"

    def _token_valid(self, token: str) -> bool:
        try:
            user, sig = token.rsplit(".", 1)
        except ValueError:
            return False
        return user == self.username and hmac.compare_digest(sig, self._sig(user))

    # ── checks ───────────────────────────────────────────────────────────────
    def check_credentials(self, username: str, password: str) -> bool:
        return (
            self.enabled
            and hmac.compare_digest(username or "", self.username)
            and hmac.compare_digest(password or "", self.password)
        )

    def check_cookies(self, cookies: Mapping[str, str]) -> bool:
        if not self.enabled:
            return True
        tok = cookies.get(COOKIE)
        return bool(tok) and self._token_valid(tok)


def make_auth_middleware(auth: Auth):
    """ASGI HTTP middleware dispatch that enforces `auth` (no-op if disabled)."""

    async def dispatch(request: Request, call_next):
        if not auth.enabled:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX):
            return await call_next(request)
        if auth.check_cookies(request.cookies):
            return await call_next(request)
        # unauthenticated
        if path.startswith("/api/") or path == "/ws":
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    return dispatch


# ── login page (dark, matches the app) ───────────────────────────────────────
def login_page(error: bool = False) -> str:
    msg = ('<p class="err">Wrong username or password.</p>' if error else "")
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Warden - sign in</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(1200px 600px at 50% -10%, #1b2440 0%, #0b0f1a 60%); color:#e7ecf5; }}
  .card {{ width:min(92vw,380px); background:#131826; border:1px solid #232a3d;
    border-radius:16px; padding:32px 28px; box-shadow:0 20px 60px rgba(0,0,0,.45); }}
  .brand {{ display:flex; align-items:center; gap:12px; margin-bottom:22px; }}
  .brand svg {{ width:36px; height:36px; }}
  .brand h1 {{ font-size:20px; margin:0; font-weight:700; }}
  .brand span {{ display:block; font-size:12px; color:#8b93a7; font-weight:400; }}
  label {{ display:block; font-size:12px; color:#9aa3b8; margin:14px 0 6px; }}
  input {{ width:100%; padding:11px 13px; border-radius:10px; border:1px solid #2b3350;
    background:#0d1120; color:#e7ecf5; font-size:15px; }}
  input:focus {{ outline:none; border-color:#5b7cfa; box-shadow:0 0 0 3px rgba(91,124,250,.25); }}
  button {{ width:100%; margin-top:20px; padding:12px; border:0; border-radius:10px;
    background:linear-gradient(180deg,#6b8afd,#5b7cfa); color:#fff; font-size:15px;
    font-weight:600; cursor:pointer; }}
  button:hover {{ filter:brightness(1.06); }}
  .err {{ color:#ff8a8a; font-size:13px; margin:14px 0 0; }}
</style></head><body>
  <form class="card" method="post" action="/login">
    <div class="brand">
      <svg viewBox="0 0 24 24" fill="none"><path d="M12 2l7 3v6c0 4.5-3 8-7 9-4-1-7-4.5-7-9V5l7-3z"
        fill="#5b7cfa"/><path d="M9 12l2 2 4-4" stroke="#fff" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"/></svg>
      <div><h1>Warden</h1><span>parental control hub</span></div>
    </div>
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    {msg}
    <button type="submit">Sign in</button>
  </form>
</body></html>"""
