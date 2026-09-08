"""ParentPay session capture — via Weduc's partner-login SSO. No ParentPay password.

The school's clubs are booked in ParentPay, not in Weduc. But you never type a ParentPay
password: the Weduc portal's Payments page mints a one-shot SSO token and bounces you to
``app.parentpay.com/.../#/partnerlogin?token=…``. So the credentials Warden already has
(WEDUC_USERNAME/WEDUC_PASSWORD) are enough to reach ParentPay too — there is no second
secret to configure, and nothing new for a human to do.

Two steps, and the second is why this needs a browser:

  1. **Mint** (httpx) — GET the Weduc handoff page with the stored Weduc cookies and
     scrape the ``partnerlogin?token=…`` URL out of it. The token is short-lived
     (~1 hour) and single-use, so it is minted fresh each time rather than stored.
  2. **Exchange** (Playwright) — the token rides in the URL **fragment**, which is never
     sent to a server: only the ParentPay SPA can read it and swap it for a session. A
     plain HTTP client therefore cannot complete this login, so we drive a real browser
     once and persist the resulting cookies for headless replay (same capture-once
     pattern as Atom and Weduc). The container image already ships Chromium.

If the Weduc session itself has expired, minting fails — so we refresh that first using
Weduc's own unattended login helper, then retry. That makes the whole chain self-healing.

Read-only: this signs in and saves cookies. It never books, cancels or pays for anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import httpx

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]                      # …/warden

WEDUC_API = "https://app.weduc.co.uk"
WEDUC_UI = "https://ui.app.weduc.co.uk"
PP_BASE = "https://app.parentpay.com"
# Where the SSO lands when it works. Anything else means the handoff failed.
PP_HOME_MARK = "/V3Payer4W3/"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_SSO_RE = re.compile(
    r"https://app\.parentpay\.com/[^\s'\"<>]*partnerlogin\?token=[^\s'\"<>]+")


def _weduc_cookies(state_file: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except Exception:
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return []
    return [c for c in cookies if "weduc" in (c.get("domain") or "")]


async def mint_sso_url(weduc_state: Path, entity_id: str, child_id: str) -> tuple[str, str]:
    """Ask Weduc for a fresh ParentPay partner-login URL. Returns (url, detail)."""
    cookies = _weduc_cookies(weduc_state)
    if not cookies:
        return "", f"no Weduc cookies in {Path(weduc_state).name}"
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c.get("value", ""), domain=(c.get("domain") or ""),
                path=c.get("path", "/"))
    try:
        async with httpx.AsyncClient(base_url=WEDUC_API, cookies=jar, timeout=30.0,
                                     headers={"User-Agent": _UA,
                                              "Referer": WEDUC_UI + "/"}) as client:
            r = await client.get(
                f"/payment/parentpay/index/entity/{entity_id}/user/{child_id}")
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"

    m = _SSO_RE.search(r.text)
    if not m:
        # The handoff page is tiny and always contains the link; its absence means the
        # Weduc session is dead (we got the login SPA) rather than that SSO is off.
        return "", ("no partner-login token in the handoff page — the Weduc session has "
                    "probably expired")
    return m.group(0), "minted"


async def _refresh_weduc(weduc_state: Path, username: str, password: str) -> tuple[bool, str]:
    """Re-capture the Weduc session so we can mint from it (needs no human)."""
    try:
        from ..weduc.login import capture_session
    except Exception as e:
        return False, f"weduc login helper unavailable: {type(e).__name__}"
    return await capture_session(state_file=weduc_state, username=username,
                                 password=password, ui_base=WEDUC_UI)


async def capture_session(state_file: Path, weduc_state: Path, entity_id: str,
                          child_id: str, weduc_username: str = "",
                          weduc_password: str = "", timeout: int = 90,
                          ) -> tuple[bool, str]:
    """Mint an SSO token from Weduc, exchange it in a browser, save the ParentPay session."""
    from playwright.async_api import async_playwright

    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    url, detail = await mint_sso_url(weduc_state, entity_id, child_id)
    if not url and weduc_username and weduc_password:
        ok, why = await _refresh_weduc(Path(weduc_state), weduc_username, weduc_password)
        if not ok:
            return False, f"could not refresh the Weduc session first ({why})"
        url, detail = await mint_sso_url(weduc_state, entity_id, child_id)
    if not url:
        return False, f"could not mint a ParentPay SSO token ({detail})"

    tmo = timeout * 1000
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=_UA,
                                                viewport={"width": 1400, "height": 1100})
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=tmo)
                # The SPA reads the fragment, calls its own login API and redirects. Wait
                # for the payer area rather than for a load event, which fires far too early.
                try:
                    await page.wait_for_url(f"**{PP_HOME_MARK}**", timeout=tmo)
                except Exception:
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=25000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)

                if PP_HOME_MARK not in page.url:
                    return False, (f"SSO did not land in ParentPay (stuck at "
                                   f"{page.url[:120]})")
                await ctx.storage_state(path=str(state_file))
                return True, f"ParentPay session captured to {state_file.name}"
            finally:
                await browser.close()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── CLI: capture a session by hand (mirrors the weduc/atom helpers) ──────────
def main() -> int:
    import argparse
    import asyncio
    import os

    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")

    ap = argparse.ArgumentParser(
        description="Capture a ParentPay session via Weduc's partner-login SSO")
    ap.add_argument("--state-file", default=str(_REPO_ROOT / "data" / "parentpay_state.json"))
    ap.add_argument("--weduc-state", default=str(_REPO_ROOT / "data" / "weduc_state.json"))
    ap.add_argument("--entity-id", default="281474976822310")
    ap.add_argument("--child-id", default="281474978315151")
    args = ap.parse_args()

    ok, detail = asyncio.run(capture_session(
        Path(args.state_file), Path(args.weduc_state), args.entity_id, args.child_id,
        os.getenv("WEDUC_USERNAME", ""), os.getenv("WEDUC_PASSWORD", "")))
    print(("[OK] " if ok else "[x] ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
