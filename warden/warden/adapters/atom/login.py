"""Capture an Atom Learning session once (Google SSO), for the adapter to reuse.

Atom signs in with Google, and Google blocks browsers it can tell are automated
("This browser or app may not be secure"). So we DON'T let Playwright launch the
browser. Instead we launch your **real** Chrome/Edge normally — just with a debug
port — you sign in to Atom with Google by hand (Google sees an ordinary browser and
is happy), and Playwright then reads the resulting `.atomlearning.com` cookies over
the debug port and saves them. The adapter reuses that cookie jar headlessly.

Usage:
    cd warden
    .venv/Scripts/python.exe -m warden.adapters.atom.login

A browser window opens at Atom → sign in with Google → it auto-detects success and
writes data/atom_state.json. Re-run when the session expires.

Options:
    --state-file PATH   where to save        (default data/atom_state.json)
    --browser PATH      chrome/edge exe       (default: auto-detect)
    --app-base URL      default https://app.atomlearning.com
    --api-base URL      default https://api.atomlearning.com
    --port N            debug port            (default 9222)
    --timeout SECONDS   wait for you to log in (default 600)
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]      # …/warden
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0")


def _find_browser() -> str | None:
    for var, tail in [
        ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
        ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
        ("LocalAppData", r"Google\Chrome\Application\chrome.exe"),
        ("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
        ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
    ]:
        base = os.environ.get(var)
        if base and Path(base, tail).exists():
            return str(Path(base, tail))
    return None


def _wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture an Atom Learning (Google SSO) session")
    ap.add_argument("--state-file", default=str(_REPO_ROOT / "data" / "atom_state.json"))
    ap.add_argument("--browser", default=None)
    ap.add_argument("--app-base", default="https://app.atomlearning.com")
    ap.add_argument("--api-base", default="https://api.atomlearning.com")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    exe = args.browser or _find_browser()
    if not exe:
        print("No Chrome or Edge found. Install one, or pass --browser <path to chrome.exe/msedge.exe>",
              file=sys.stderr)
        return 2

    out = Path(args.state_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = _REPO_ROOT / "data" / "atom_login_profile"      # dedicated, avoids locking your main profile
    profile.mkdir(parents=True, exist_ok=True)
    probe = f"{args.api_base.rstrip('/')}/ms_accounts/user"

    print(f"Launching {Path(exe).name} ... a window will open at Atom.")
    print("-> Sign in with your Google account. I'll detect it automatically and save the session.")
    proc = subprocess.Popen([
        exe,
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--new-window",
        f"{args.app_base.rstrip('/')}/",
    ])

    if not _wait_port(args.port):
        print("Could not open the browser's debug port. Close any other window using it and retry.",
              file=sys.stderr)
        proc.terminate()
        return 2

    try:
        import httpx
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"Missing dependency: {e}", file=sys.stderr)
        return 2

    rc = 1
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        deadline = time.time() + args.timeout
        authed = False
        while time.time() < deadline:
            atom_cookies = [c for c in ctx.cookies() if "atomlearning.com" in c.get("domain", "")]
            if atom_cookies:
                jar = httpx.Cookies()
                for c in atom_cookies:
                    jar.set(c["name"], c.get("value", ""),
                            domain=c["domain"].lstrip("."), path=c.get("path", "/"))
                try:
                    r = httpx.get(probe, cookies=jar, headers={"User-Agent": _UA,
                                  "Accept": "application/json"}, timeout=8, follow_redirects=True)
                    if r.status_code == 200:
                        authed = True
                        break
                except Exception:
                    pass
            time.sleep(2)

        if authed:
            ctx.storage_state(path=str(out))
            n = len([c for c in ctx.cookies() if "atomlearning.com" in c.get("domain", "")])
            print(f"\n[OK] Signed in. Saved {n} atomlearning.com cookies to {out}")
            rc = 0
        else:
            print("\n[x] Timed out waiting for sign-in. Re-run and finish the Google login, "
                  "or pass a larger --timeout.", file=sys.stderr)
        # detach Playwright without killing your browser, then close the login window
        try:
            browser.close()
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
