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
    AdGuardConfig, Baseline, ClientConfig, GroupConfig, ServiceConfig,
    SignalDef, SourceConfig, SubjectConfig, WardenConfig,
)


class Settings(BaseModel):
    adguard_url: str = "http://10.7.11.29"
    adguard_user: str = ""
    adguard_pass: str = ""
    adguard_mode: str = "auto"                 # auto | live | fake
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"
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
            host=os.getenv("WARDEN_HOST", "0.0.0.0"),
            port=int(os.getenv("WARDEN_PORT", "8080")),
            db_path=os.getenv("WARDEN_DB", "warden.db"),
            tz=os.getenv("WARDEN_TZ", "Europe/London"),
            eval_interval_min=int(os.getenv("WARDEN_EVAL_INTERVAL_MIN", "10")),
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

    return WardenConfig(
        adguard=AdGuardConfig(**(raw.get("adguard") or {})),
        groups=groups, clients=clients, services=services,
        subjects=subjects, sources=sources, signals=signals,
        rules=raw.get("rules") or [],
    )


def resolve_secrets(source: SourceConfig) -> dict[str, str]:
    """Map the source's logical secret names to their env-var values (may be empty)."""
    return {logical: os.getenv(env_var, "") for logical, env_var in source.secrets.items()}
