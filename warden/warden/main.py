"""Warden entrypoint — build the app, wire services, serve the SPA + JSON API.

Run:  python -m warden        (or uvicorn warden.main:app)
The lifespan builds every service and stitches them together via AppContext, which
is exposed to routers as request.app.state.ctx.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import COOKIE, MAX_AGE, Auth, login_page, make_auth_middleware
from .config import Settings, load_config
from .context import AppContext
from .db import DB
from .models import utcnow
from .rules.schema import Rule
from .rules.vocab import build_vocabulary
from .signals.bus import SignalBus

log = logging.getLogger("warden")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent                       # warden/
WEB_DIR = REPO / "web"
CONFIG_PATH = REPO / "config" / "warden.yaml"


async def _seed_rules(ctx: AppContext) -> None:
    """On first boot, compile the plain-English rules listed in warden.yaml."""
    if ctx.db.list_rules():
        return
    for i, text in enumerate(ctx.config.rules):
        rid = f"seed-{i+1}"
        rule = Rule(id=rid, text=text, source="seed", created_at=utcnow().isoformat())
        try:
            # curated seeds parse deterministically — keep first boot fast + offline
            # (no LLM/CLI call); only reach for the backend if the parser can't cope.
            try:
                rule.compiled = ctx.compiler.compile_fallback(text)
            except ValueError:
                rule.compiled = await ctx.compiler.compile(text)
        except Exception as e:  # keep the rule; surface the error in the UI
            rule.compile_error = str(e)
        ctx.db.upsert_rule(rule)
    log.info("seeded %d rules", len(ctx.config.rules))


def build_app() -> FastAPI:
    load_dotenv(REPO / ".env")
    settings = Settings.from_env()
    config = load_config(CONFIG_PATH)
    db = DB(settings.db_path)
    bus = SignalBus(db)
    ctx = AppContext(settings, config, db, bus)

    # heavy services — imported here so a failure in one is easy to localise
    from .adguard.service import build_adguard
    from .rules.compiler import RuleCompiler
    from .rules.engine import RuleEngine
    from .rules.evaluator import RuleEvaluator
    from .adapters.registry import SourceRegistry
    from .documents import DocumentReader
    from .hosts import HostControl
    from .overrides import OverrideKeeper
    from .reminders import ReminderBuilder

    ctx.adguard = build_adguard(settings, config)
    # devices under a live override are exempt from group writes; the service asks
    # the DB on every write so an override taken seconds ago is already in force.
    ctx.adguard.active_overrides = lambda: db.active_override_names(utcnow().isoformat())
    ctx.overrides = OverrideKeeper(ctx)
    ctx.hosts = HostControl(ctx)
    ctx.compiler = RuleCompiler(settings, build_vocabulary(config))
    # Every prompt Warden sends and every reply it gets is recorded and shown in the
    # Activity feed; without this line the compiler makes the calls but nothing keeps
    # them, and "why did it decide that?" has no answer after the fact.
    ctx.compiler.journal = ctx.record_llm
    ctx.evaluator = RuleEvaluator(ctx)
    ctx.engine = RuleEngine(ctx)
    ctx.reader = DocumentReader(ctx)
    ctx.reminders = ReminderBuilder(ctx)
    ctx.registry = SourceRegistry(ctx)
    ctx.auth = Auth(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.adguard.connect()
        await _seed_rules(ctx)
        await ctx.engine.start()
        await ctx.registry.start()
        await ctx.overrides.start()
        log.info("Warden ready — AdGuard mode=%s", ctx.adguard.mode)
        yield
        await ctx.overrides.stop()
        await ctx.registry.stop()
        await ctx.engine.stop()
        await ctx.adguard.close()

    app = FastAPI(title="Warden", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx

    # login gate (no-op when no WARDEN_PASSWORD is set) — must wrap everything
    app.middleware("http")(make_auth_middleware(ctx.auth))

    # routers
    from .api import (
        routes_state, routes_rules, routes_sources, routes_actions, routes_devices,
        routes_reminders, ws,
    )
    app.include_router(routes_state.router, prefix="/api")
    app.include_router(routes_rules.router, prefix="/api")
    app.include_router(routes_sources.router, prefix="/api")
    app.include_router(routes_actions.router, prefix="/api")
    app.include_router(routes_devices.router, prefix="/api")
    app.include_router(routes_reminders.router, prefix="/api")
    app.include_router(ws.router)   # /ws (no prefix)

    # static SPA
    if (WEB_DIR / "static").is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """The SPA shell, with its asset URLs stamped with their own mtimes.

        Nothing sets Cache-Control on /static, so a browser is free to guess how long
        app.js stays fresh — and it guesses days for a file that hasn't changed in a
        while. After a deploy that showed up as new API behaviour driving an old UI:
        the server had the feature, the page had no button for it, and the only fix was
        a hard refresh nobody thinks to do. Stamping the URLs makes a changed file a
        different URL, so an upgrade lands the moment the container restarts.
        """
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("/static/css/app.css", "/static/js/app.js"):
            f = WEB_DIR / asset.lstrip("/")
            stamp = int(f.stat().st_mtime) if f.exists() else 0
            html = html.replace(asset, f"{asset}?v={stamp}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    # ── auth routes (active only when a password is configured) ───────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request) -> HTMLResponse:
        if ctx.auth.check_cookies(request.cookies):        # already signed in
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_page())

    @app.post("/login")
    async def login_post(request: Request):
        form = parse_qs((await request.body()).decode("utf-8"))
        user = (form.get("username") or [""])[0]
        pw = (form.get("password") or [""])[0]
        if ctx.auth.check_credentials(user, pw):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, ctx.auth.make_token(), max_age=MAX_AGE,
                            httponly=True, samesite="lax", path="/")
            return resp
        return HTMLResponse(login_page(error=True), status_code=401)

    @app.get("/logout")
    async def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "adguard_mode": ctx.adguard.mode}

    return app


app = build_app()


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = app.state.ctx.settings
    uvicorn.run("warden.main:app", host=s.host, port=s.port, ws="wsproto", reload=False)


if __name__ == "__main__":
    main()
