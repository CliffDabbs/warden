"""Configuration — load config/warden.yaml into typed models, plus env settings.

The YAML uses maps keyed by name for groups/sources (nice to edit); we flatten them
into the list[...] models. Secrets are resolved from environment variables named in
the source's `secrets:` map, so nothing sensitive lives in the YAML.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

from .models import (
    AdGuardConfig, Baseline, ClientConfig, GroupConfig, HostConfig,
    ManagedServiceConfig, QuickActionConfig, ServiceConfig, SignalDef,
    SourceConfig, SubjectConfig, WardenConfig,
)


class Settings(BaseModel):
    adguard_url: str = "http://10.7.11.29"
    adguard_user: str = ""
    adguard_pass: str = ""
    adguard_mode: str = "auto"                 # auto | live | fake
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"
    # The dynamic evaluator does the hardest reasoning in the system — judging a rule
    # AND working out its own next wake-up from clock/term/window arithmetic. That is
    # where a cheap model shows its limits, so it can be pointed at a stronger one
    # independently of the compiler. Empty = use llm_model.
    eval_model: str = ""
    # LLM backend for the rule compiler:
    #   auto = API key if set, else the Claude Code CLI if installed, else the
    #          deterministic parser. api|cli force a path; off = parser only.
    llm_backend: str = "auto"           # auto | api | cli | off
    claude_cli: str = "claude"          # command/path of the Claude Code CLI
    host: str = "0.0.0.0"
    port: int = 8080
    db_path: str = "warden.db"
    tz: str = "Europe/London"
    eval_interval_min: int = 10         # periodic re-evaluation cadence for dynamic rules
    # How long a rule evaluation will wait for a source collection that is running (or
    # due) before deciding without it. Data first, decisions second — but a hung scrape
    # must delay a decision, never strand it.
    collect_wait_sec: int = 180
    # basic auth — a static login gate. Enabled iff auth_password is non-empty.
    auth_username: str = "admin"
    auth_password: str = ""
    auth_secret: str = ""               # cookie-signing key; derived from creds if unset

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            adguard_url=os.getenv("ADGUARD_URL", "http://10.7.11.29"),
            adguard_user=os.getenv("ADGUARD_USER", ""),
            adguard_pass=os.getenv("ADGUARD_PASS", ""),
            adguard_mode=os.getenv("ADGUARD_MODE", "auto"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            llm_model=os.getenv("WARDEN_LLM_MODEL", "claude-haiku-4-5-20251001"),
            eval_model=os.getenv("WARDEN_EVAL_MODEL", ""),
            host=os.getenv("WARDEN_HOST", "0.0.0.0"),
            port=int(os.getenv("WARDEN_PORT", "8080")),
            db_path=os.getenv("WARDEN_DB", "warden.db"),
            tz=os.getenv("WARDEN_TZ", "Europe/London"),
            eval_interval_min=int(os.getenv("WARDEN_EVAL_INTERVAL_MIN", "10")),
            collect_wait_sec=int(os.getenv("WARDEN_COLLECT_WAIT_SEC", "180")),
            llm_backend=os.getenv("WARDEN_LLM", "auto"),
            claude_cli=os.getenv("WARDEN_CLAUDE_CLI", "claude"),
            auth_username=os.getenv("WARDEN_USERNAME", "admin"),
            auth_password=os.getenv("WARDEN_PASSWORD", ""),
            auth_secret=os.getenv("WARDEN_SECRET", ""),
        )


def load_config(path: str | Path) -> WardenConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    groups = [
        GroupConfig(
            name=name,
            tag=g.get("tag"),
            match_tags=g.get("match_tags") or [],
            baseline=Baseline(**(g.get("baseline") or {})),
            default_blocked=g.get("default_blocked") or [],
        )
        for name, g in (raw.get("groups") or {}).items()
    ]
    clients = [ClientConfig(**c) for c in (raw.get("clients") or [])]
    services = [ServiceConfig(**s) for s in (raw.get("services") or [])]
    subjects = [SubjectConfig(**s) for s in (raw.get("subjects") or [])]
    sources = [
        SourceConfig(key=key, **{k: v for k, v in (s or {}).items()})
        for key, s in (raw.get("sources") or {}).items()
    ]
    signals = [SignalDef(**s) for s in (raw.get("signals") or [])]
    quick_actions = [QuickActionConfig(**q) for q in (raw.get("quick_actions") or [])]
    hosts = [
        HostConfig(name=name, **{k: v for k, v in (h or {}).items()})
        for name, h in (raw.get("hosts") or {}).items()
    ]
    managed = [ManagedServiceConfig(**m) for m in (raw.get("managed_services") or [])]

    return WardenConfig(
        adguard=AdGuardConfig(**(raw.get("adguard") or {})),
        groups=groups, clients=clients, services=services,
        subjects=subjects, sources=sources, signals=signals,
        rules=raw.get("rules") or [],
        quick_actions=quick_actions, hosts=hosts, managed_services=managed,
    )


def resolve_secrets(source: SourceConfig) -> dict[str, str]:
    """Map the source's logical secret names to their env-var values (may be empty)."""
    return {logical: os.getenv(env_var, "") for logical, env_var in source.secrets.items()}
