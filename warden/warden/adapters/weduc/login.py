"""Weduc session capture + endpoint recon.

Two jobs, both driven by Playwright:

  1. **capture** (default) — log in with WEDUC_USERNAME/WEDUC_PASSWORD and save the
     session to ``data/weduc_state.json`` (Playwright storage_state: cookies AND
     localStorage). The adapter reuses it headlessly — same capture-once pattern as
     Atom, so it works in the container. Unlike Atom this needs no human: the creds
     are enough, so the adapter can re-capture by itself when the session expires.

  2. **recon** (``--recon``) — after logging in, walk the portal and record every
     XHR/fetch the app makes, into ``data/weduc_recon.json``, so the collectors can
     hit JSON APIs instead of scraping a DOM.

PORTAL NOTE (observed 26 Jul 2026): the portal is now a **Blazor WebAssembly** app
(`WeducPortal.styles.css`, `blazor.webassembly.js`), NOT the server-rendered SPA that
docs/weduclukedigestHANDOVER (2).md §3 describes. Consequences:
  * The login inputs carry **no name/id** — only placeholders ("Login or E-mail",
    "Password"), so selectors must go by placeholder.
  * A hidden "Authentication Code" input exists — the portal supports 2FA even where
    an account doesn't use it. We detect it becoming visible and say so plainly.
  * WASM boots asynchronously, so we wait for the field to exist rather than for a
    load event.
  * Auth may be a bearer token in localStorage rather than a cookie, so recon records
    Authorization headers and localStorage keys, and we persist storage_state (which
    covers both).

Read-only: this navigates and reads. It never submits a portal form or posts anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent            # …/warden/warden/adapters/weduc
_REPO_ROOT = _HERE.parents[2]                      # …/warden  (matches atom/adapter.py)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE = "https://ui.app.weduc.co.uk"

# Response bodies bigger than this are truncated in the recon dump (keep it readable).
_BODY_PREVIEW = 40000
# Cap the recon crawl so a nav loop can't run away.
_MAX_PAGES = 25
# NEVER navigate to these during recon: following /logout ends the session mid-crawl
# and the storage_state we then save is a signed-out one.
_NEVER_VISIT = ("/logout", "/login", "/rest/login/logout")

# Blazor renders its nav lazily, so link-harvesting alone finds very little. Seed the
# crawl with the feature routes from docs/weduclukedigestHANDOVER (2).md §3 — even
# where the route 404s, the XHR it fires on the way is what we're after.
SEED_ROUTES: list[str] = [
    "/dashboard/newsfeed/list/user/{user}",
    "/message/message/index",
    "/calendar/event/index",
    "/forms/index/forms/user/{child}",
    "/assessment/parent/index/id/{child}",
    "/user/profile/view/",
]


# ── login ────────────────────────────────────────────────────────────────────
async def _find_login_fields(page, timeout_ms: int):
    """Locate the username + password inputs. Blazor renders them late and gives them
    no name/id, so we wait for the password box then go by placeholder."""
    await page.wait_for_selector("input[type=password]", timeout=timeout_ms, state="visible")

    pw = page.locator("input[type=password]").first
    # the visible text input whose placeholder mentions login/e-mail
    for sel in ('input[placeholder*="Login" i]', 'input[placeholder*="mail" i]',
                'input[placeholder*="user" i]', "input[type=text]"):
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc, pw
        except Exception:
            continue
    return None, pw


async def _auth_code_visible(page) -> bool:
    """Is the 2FA 'Authentication Code' box showing?"""
    for sel in ('input[placeholder*="Authentication" i]', 'input[placeholder*="code" i]'):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            continue
    return False


async def _fill_login(page, username: str, password: str, timeout_ms: int) -> None:
    user, pw = await _find_login_fields(page, timeout_ms)
    if user is None:
        raise RuntimeError(
            "found the password box but not the username box — login markup changed; "
            "re-run with --headed to look"
        )
    await user.fill(username)
    await pw.fill(password)

    for sel in ('button[type=submit]', 'button:has-text("Login")', 'input[type=submit]'):
        btn = page.locator(sel).first
        try:
            if await btn.count() and await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue
    await pw.press("Enter")


async def capture_session(state_file: Path, username: str, password: str,
                          ui_base: str = BASE, timeout: int = 60) -> tuple[bool, str]:
    """Log in headlessly and write a fresh storage_state. Returns (ok, detail).

    This is the adapter's self-refresh hook: Weduc authenticates with a plain
    username+password, so a dead session can be replaced without a human — unlike
    Atom, whose Google SSO always needs one.
    """
    from playwright.async_api import async_playwright

    if not (username and password):
        return False, "no credentials"

    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmo = timeout * 1000

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=_UA,
                                                viewport={"width": 1400, "height": 1000})
                page = await ctx.new_page()
                await page.goto(f"{ui_base.rstrip('/')}/login",
                                wait_until="domcontentloaded", timeout=tmo)
                await _fill_login(page, username, password, tmo)
                try:
                    await page.wait_for_load_state("networkidle", timeout=tmo)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)

                if await _auth_code_visible(page):
                    return False, "portal is asking for a 2FA code; cannot refresh unattended"
                if "/login" in urlparse(page.url).path.lower():
                    return False, "credentials rejected"

                await ctx.storage_state(path=str(state_file))
                return True, f"session captured to {state_file.name}"
            finally:
                await browser.close()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── recon plumbing ───────────────────────────────────────────────────────────
def _install_recorder(page, captured: list[dict[str, Any]]) -> None:
    async def on_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            ctype = (resp.headers or {}).get("content-type", "")
            req_headers = await req.all_headers()
            entry: dict[str, Any] = {
                "method": req.method,
                "url": resp.url,
                "path": urlparse(resp.url).path,
                "status": resp.status,
                "content_type": ctype,
                # how is this call authorised? bearer token vs cookie is the key question
                # for reusing the session headlessly from httpx.
                "has_auth_header": "authorization" in req_headers,
                "auth_scheme": (req_headers.get("authorization", "").split(" ")[0] or None),
                "sent_cookie": "cookie" in req_headers,
            }
            # POST bodies matter: several list endpoints (newsfeed river, messages)
            # return the SPA shell instead of JSON unless you send the params they
            # expect, so replaying them headlessly needs the exact payload.
            if req.method != "GET":
                try:
                    entry["post_data"] = (req.post_data or "")[:2000]
                    entry["post_content_type"] = req_headers.get("content-type", "")
                except Exception:
                    pass
            if "json" in ctype.lower():
                try:
                    body = await resp.text()
                    entry["body_bytes"] = len(body)
                    entry["body_preview"] = body[:_BODY_PREVIEW]
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        entry["top_level_keys"] = list(parsed.keys())[:40]
                    elif isinstance(parsed, list):
                        entry["list_len"] = len(parsed)
                        if parsed and isinstance(parsed[0], dict):
                            entry["first_item_keys"] = list(parsed[0].keys())[:40]
                except Exception:
                    pass
            captured.append(entry)
        except Exception:
            pass

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))


async def _discover_links(page, base: str) -> list[str]:
    """Blazor routes are client-side; harvest them from the rendered nav."""
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))")
    except Exception:
        return []
    out: list[str] = []
    for h in hrefs or []:
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = h if h.startswith("http") else base + "/" + h.lstrip("/")
        path = urlparse(url).path.lower().rstrip("/")
        if any(path.startswith(bad) for bad in _NEVER_VISIT):
            continue
        if url.startswith(base) and url not in out:
            out.append(url)
    return out


# ── main flow ────────────────────────────────────────────────────────────────
async def _run(args) -> int:
    from playwright.async_api import async_playwright

    username = args.username or os.getenv("WEDUC_USERNAME", "")
    password = args.password or os.getenv("WEDUC_PASSWORD", "")
    if not (username and password):
        print("Set WEDUC_USERNAME and WEDUC_PASSWORD in .env (or pass --username/--password).",
              file=sys.stderr)
        return 2

    out = Path(args.state_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    tmo = args.timeout * 1000
    captured: list[dict[str, Any]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(user_agent=_UA, viewport={"width": 1400, "height": 1000})
        page = await ctx.new_page()
        if args.recon:
            _install_recorder(page, captured)

        print(f"-> opening {base}/login  (Blazor WASM — waiting for it to boot)")
        await page.goto(f"{base}/login", wait_until="domcontentloaded", timeout=tmo)
        await _fill_login(page, username, password, tmo)

        # let the login round-trip settle
        try:
            await page.wait_for_load_state("networkidle", timeout=tmo)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        if await _auth_code_visible(page):
            print("[x] Weduc is asking for a 2FA 'Authentication Code'. This helper cannot "
                  "complete that unattended — re-run with --headed and type it in.",
                  file=sys.stderr)
            await browser.close()
            return 3

        if "/login" in urlparse(page.url).path.lower():
            print(f"[x] still on the login page ({page.url}) — credentials rejected, or the "
                  "portal wants something else. Re-run with --headed to watch.", file=sys.stderr)
            await browser.close()
            return 1

        cookies = await ctx.cookies()
        weduc_cookies = [c for c in cookies if "weduc" in (c.get("domain") or "")]
        print(f"[OK] signed in — landed on {page.url}")
        print(f"     {len(weduc_cookies)} weduc cookies")

        try:
            ls_keys = await page.evaluate("Object.keys(window.localStorage)")
            print(f"     localStorage keys: {ls_keys}")
        except Exception:
            ls_keys = []

        # ── recon crawl ──────────────────────────────────────────────────────
        if args.recon:
            seeded = [base + r.format(user=args.user_id, child=args.child_id)
                      for r in SEED_ROUTES]
            discovered = await _discover_links(page, base)
            links = seeded + [u for u in discovered if u not in seeded]
            print(f"\n-> {len(seeded)} seeded + {len(discovered)} discovered routes; "
                  f"visiting up to {_MAX_PAGES} (never /logout)")
            visited: set[str] = set()
            for url in links[:_MAX_PAGES]:
                if url in visited:
                    continue
                visited.add(url)
                mark = len(captured)
                name = urlparse(url).path or "/"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=tmo)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"   {name:45s} nav failed: {type(e).__name__}")
                    continue
                for entry in captured[mark:]:
                    entry.setdefault("seen_on", name)
                print(f"   {name:45s} +{len(captured) - mark} XHR")

        # Only persist a session we've just confirmed is still signed in — a crawl that
        # wandered somewhere session-ending would otherwise overwrite a good state file
        # with a dead one, and the failure would only surface on the next collect.
        await page.goto(f"{base}/", wait_until="domcontentloaded", timeout=tmo)
        await page.wait_for_timeout(2000)
        if "/login" in urlparse(page.url).path.lower():
            print(f"[x] session is no longer valid after the crawl ({page.url}) — NOT "
                  f"overwriting {out}. Re-run without --recon to capture a clean session.",
                  file=sys.stderr)
            await browser.close()
            return 1

        await ctx.storage_state(path=str(out))
        await browser.close()

    print(f"\n[OK] session saved to {out} (verified still signed in)")

    if args.recon:
        recon_path = out.parent / "weduc_recon.json"
        json_calls = [c for c in captured if "json" in (c.get("content_type") or "").lower()]
        recon_path.write_text(json.dumps({
            "base_url": base,
            "localStorage_keys": ls_keys,
            "total_xhr": len(captured),
            "json_endpoints": len(json_calls),
            "calls": captured,
        }, indent=2), encoding="utf-8")
        print(f"[OK] recon written to {recon_path}")
        print(f"     {len(captured)} XHR/fetch calls, {len(json_calls)} returning JSON")

        bearer = [c for c in captured if c.get("has_auth_header")]
        print(f"\n  auth style: {len(bearer)}/{len(captured)} calls sent an Authorization header"
              f"{' (' + str(bearer[0].get('auth_scheme')) + ')' if bearer else ''}")

        seen: set[str] = set()
        print("\n  JSON endpoints discovered:")
        for c in json_calls:
            sig = f"{c['method']} {c['path']}"
            if sig in seen:
                continue
            seen.add(sig)
            keys = c.get("top_level_keys") or c.get("first_item_keys") or []
            print(f"    {sig:65s} {c['status']}  keys={keys[:8]}")
        if not json_calls:
            print("    (none captured)")
    return 0


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")

    ap = argparse.ArgumentParser(description="Weduc session capture + endpoint recon")
    ap.add_argument("--state-file", default=str(_REPO_ROOT / "data" / "weduc_state.json"))
    ap.add_argument("--base-url", default=BASE)
    ap.add_argument("--username", default=None, help="defaults to $WEDUC_USERNAME")
    ap.add_argument("--password", default=None, help="defaults to $WEDUC_PASSWORD")
    ap.add_argument("--user-id", default="281474978315305", help="account holder id (from recon)")
    ap.add_argument("--child-id", default="281474978315151", help="child id (Luke, from recon)")
    ap.add_argument("--recon", action="store_true", help="crawl the portal and dump the XHR map")
    ap.add_argument("--headed", action="store_true", help="show the browser (debugging / 2FA)")
    ap.add_argument("--timeout", type=int, default=60, help="per-navigation timeout (seconds)")
    args = ap.parse_args()

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
