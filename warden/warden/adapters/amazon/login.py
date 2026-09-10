"""Amazon Kids Parent Dashboard — session capture + endpoint recon.

Recon tool, not an adapter. It exists to answer four questions before anyone writes
one, because Amazon publishes no API for parents.amazon.co.uk and everything below the
UI is undocumented and free to change:

  1. Can a session captured by hand be reused later, headlessly, from the container —
     the "capture once, runs forever" bargain the Atom adapter relies on? Amazon's
     sessions are shorter-lived and better defended than a school portal's.
  2. Is the dashboard backed by clean JSON, or does it render server-side?
  3. How is a call authorised — cookie alone, a bearer token, an anti-CSRF token
     minted per page load? That decides whether httpx can replay it at all.
  4. Which call does "pause this child's device" actually make, and what does it need?
     That is the one control worth having: DNS filtering cannot touch a downloaded
     film, and a paused Fire tablet stops regardless of the network.

Three jobs:

  capture (default)   Opens a REAL browser window at the Parent Dashboard. YOU sign in —
                      password, OTP, captcha, whatever Amazon asks. Warden never sees or
                      stores the credentials; when the dashboard loads it saves the
                      session (cookies + localStorage) to data/amazon_state.json. This is
                      the Atom pattern: automating an Amazon sign-in is both fragile and
                      a good way to get an account flagged, so a human does it once.

  --watch             Same window, then it RECORDS while you drive. Open a child, look at
                      screen-time settings, and — if you want Warden to be able to do it —
                      press Pause and then Resume yourself. Every XHR the app makes is
                      written to data/amazon_recon.json with its method, payload and
                      response shape. Nothing is clicked for you: a script that went
                      hunting for the pause button would be pausing a real child's tablet
                      to see what happened.

  --check             The feasibility test. Loads the SAVED session headlessly, exactly as
                      the container would days later, and reports whether it is still
                      signed in. Run it now, tomorrow, and next week: if it fails after a
                      day, an adapter is not worth building on this.

Read-only by construction: this navigates, waits, and records. It never fills a form,
never clicks a control, and never posts anything of its own.

    python -m warden.adapters.amazon.login                 # sign in once, save session
    python -m warden.adapters.amazon.login --watch         # record while you click around
    python -m warden.adapters.amazon.login --check         # does the saved session still work?

The state and recon files hold real session cookies and your children's names and usage.
Both live under data/, which is gitignored — keep it that way.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent            # …/warden/warden/adapters/amazon
_REPO_ROOT = _HERE.parents[2]                      # …/warden  (matches atom/login.py)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# UK household → .co.uk. The dashboard is region-specific; a US account is parents.amazon.com.
BASE = "https://parents.amazon.co.uk"

_BODY_PREVIEW = 40000        # response bodies are truncated in the dump at this size
# Anything under these paths means "not signed in" (Amazon's shared auth portal).
_SIGNIN_MARKERS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge")


def _signed_in(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not any(m in path for m in _SIGNIN_MARKERS)


def _install_recorder(page, captured: list[dict[str, Any]]) -> None:
    """Record every XHR/fetch the dashboard makes, with enough detail to replay it.

    The auth fields are the point of the exercise: a call that rides on cookies alone can
    be replayed by httpx from the container, while one carrying a token minted by the page
    needs a browser (or a token endpoint) every time — which changes what an adapter can
    promise. POST bodies are kept because a mutation is only reproducible with its payload.
    """
    async def on_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            headers = await req.all_headers()
            ctype = (resp.headers or {}).get("content-type", "")
            entry: dict[str, Any] = {
                "method": req.method,
                "url": resp.url,
                "path": urlparse(resp.url).path,
                "status": resp.status,
                "content_type": ctype,
                "sent_cookie": "cookie" in headers,
                "has_auth_header": "authorization" in headers,
                "auth_scheme": (headers.get("authorization", "").split(" ")[0] or None),
                # Amazon mints anti-CSRF tokens per page; if the mutations carry one,
                # an adapter has to fetch a page to get it before it can act.
                "csrf_headers": sorted(k for k in headers
                                       if "csrf" in k or "anti-csrf" in k or k == "x-amz-csrf"),
            }
            if req.method != "GET":
                entry["post_data"] = (req.post_data or "")[:4000]
                entry["post_content_type"] = headers.get("content-type", "")
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
            pass                      # recon must never break the browsing it observes

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))


async def _wait_for_signin(page, base: str, timeout_s: int) -> bool:
    """Wait for the human to finish signing in. Polls rather than racing a selector,
    because the journey varies: password, OTP, captcha, 'keep me signed in', or nothing
    at all if the browser profile is already authenticated."""
    for _ in range(timeout_s):
        await page.wait_for_timeout(1000)
        if page.url.startswith(base) and _signed_in(page.url):
            return True
    return False


async def _prompt(text: str) -> None:
    """Block on the console without freezing the event loop (the browser stays alive)."""
    await asyncio.to_thread(input, text)


async def _run(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    base = args.base.rstrip("/")
    state_file = Path(args.state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    recon_file = Path(args.recon_file)

    async with async_playwright() as pw:
        # ── --check: the saved session, headless, as the container would use it ──
        if args.check:
            if not state_file.exists():
                print(f"no saved session at {state_file} — run without --check first")
                return 2
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(storage_state=str(state_file), user_agent=_UA,
                                                viewport={"width": 1400, "height": 1000})
                page = await ctx.new_page()
                captured: list[dict[str, Any]] = []
                _install_recorder(page, captured)
                await page.goto(base, wait_until="networkidle", timeout=args.timeout * 1000)
                await page.wait_for_timeout(3000)
                ok = _signed_in(page.url)
                age = ""
                try:
                    import datetime
                    ts = datetime.datetime.fromtimestamp(state_file.stat().st_mtime)
                    age = f" (captured {ts:%d %b %H:%M})"
                except Exception:
                    pass
                print(f"session{age}: {'ALIVE — still signed in' if ok else 'DEAD — bounced to sign-in'}")
                print(f"landed on: {page.url}")
                print(f"api calls seen: {len(captured)}")
                if ok:
                    # a live check is free recon: keep what it saw
                    _dump(recon_file.with_name(recon_file.stem + "_check.json"), captured, base)
                return 0 if ok else 1
            finally:
                await browser.close()

        # ── capture (and optionally watch): a real window, driven by a human ─────
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        try:
            ctx = await browser.new_context(
                storage_state=str(state_file) if (args.reuse and state_file.exists()) else None,
                user_agent=_UA, viewport=None)
            page = await ctx.new_page()
            captured = []
            _install_recorder(page, captured)

            print(f"\nOpening {base} — sign in with YOUR Amazon account.")
            print("Warden never sees the password; it only keeps the session afterwards.\n")
            await page.goto(base, wait_until="domcontentloaded", timeout=args.timeout * 1000)

            if not await _wait_for_signin(page, base, args.signin_timeout):
                print(f"still not on the dashboard after {args.signin_timeout}s — "
                      f"currently at {page.url}\nnothing saved; run it again when you're ready.")
                return 2

            await ctx.storage_state(path=str(state_file))
            print(f"signed in — session saved to {state_file}")

            if args.watch:
                print("\nRECORDING. Now drive the dashboard yourself:")
                print("  • open each child's profile")
                print("  • open Screen Time / Daily Goals & Time Limits")
                print("  • if you want Warden to be able to do it: press Pause, then Resume")
                print("  • anything else you'd want automated")
                print("\nNothing is clicked for you — a script hunting for the pause button")
                print("would be pausing a real tablet to find out what it does.\n")
                await _prompt("Press Enter here when you're done… ")
                # the session may have been refreshed while browsing; keep the newest
                await ctx.storage_state(path=str(state_file))
                _dump(recon_file, captured, base)
            else:
                print("(add --watch to record what the dashboard calls while you click around)")
            return 0
        finally:
            await browser.close()


def _dump(path: Path, captured: list[dict[str, Any]], base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hosts: dict[str, int] = {}
    for e in captured:
        host = urlparse(e["url"]).netloc
        hosts[host] = hosts.get(host, 0) + 1
    payload = {
        "base": base,
        "calls": captured,
        "summary": {
            "total": len(captured),
            "by_host": hosts,
            "mutations": [f"{e['method']} {e['path']}" for e in captured if e["method"] != "GET"],
            "json_paths": sorted({e["path"] for e in captured
                                  if "json" in (e.get("content_type") or "").lower()}),
            "any_auth_header": any(e.get("has_auth_header") for e in captured),
            "any_csrf_header": any(e.get("csrf_headers") for e in captured),
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    s = payload["summary"]
    print(f"\nrecorded {s['total']} api call(s) → {path}")
    print(f"  hosts        : {', '.join(f'{h} ×{n}' for h, n in sorted(hosts.items())) or '—'}")
    print(f"  json paths   : {len(s['json_paths'])}")
    print(f"  mutations    : {', '.join(s['mutations']) or 'none recorded'}")
    print(f"  bearer tokens: {'yes' if s['any_auth_header'] else 'no (cookie auth)'}"
          f" | csrf headers: {'yes' if s['any_csrf_header'] else 'no'}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Amazon Kids Parent Dashboard — session capture + endpoint recon")
    ap.add_argument("--base", default=BASE, help=f"dashboard base URL (default {BASE})")
    ap.add_argument("--state-file", default=str(_REPO_ROOT / "data" / "amazon_state.json"))
    ap.add_argument("--recon-file", default=str(_REPO_ROOT / "data" / "amazon_recon.json"))
    ap.add_argument("--watch", action="store_true",
                    help="after sign-in, record every API call while YOU drive the dashboard")
    ap.add_argument("--check", action="store_true",
                    help="headlessly test the SAVED session — the feasibility question")
    ap.add_argument("--reuse", action="store_true",
                    help="start from the saved session (skips sign-in if it is still good)")
    ap.add_argument("--timeout", type=int, default=60, help="per-navigation timeout (seconds)")
    ap.add_argument("--signin-timeout", type=int, default=300,
                    help="how long to wait for you to finish signing in (seconds)")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
