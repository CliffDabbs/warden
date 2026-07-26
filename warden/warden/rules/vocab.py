"""Vocabulary — the controlled terms the plain-English compiler maps free text onto.

Built from WardenConfig so it always reflects the user's real groups/services/people/
signals. The compiler (LLM or fallback) resolves phrases like "the kids", "youtube",
"luke's atom learning" to these canonical ids. Aliases capture common phrasings.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import WardenConfig


# common phrasings → canonical, so both the fallback parser and the LLM prompt agree
GROUP_ALIASES = {
    "kids": ["kids", "children", "child", "kid", "the kids", "kids devices", "kids' devices"],
    "tv": ["tv", "tvs", "televisions", "telly", "the tvs"],
}
SERVICE_ALIASES = {
    "youtube": ["youtube", "you tube", "yt"],
    "netflix": ["netflix"],
    "tiktok": ["tiktok", "tik tok"],
    "roblox": ["roblox"],
    "discord": ["discord"],
}
# words that mean "allow" vs "block"
ALLOW_WORDS = ["enable", "allow", "unblock", "turn on", "switch on", "let", "permit", "open"]
BLOCK_WORDS = ["disable", "block", "turn off", "switch off", "stop", "deny", "cut off", "lock"]


class VocabService(BaseModel):
    id: str
    name: str
    groups: list[str]
    aliases: list[str] = Field(default_factory=list)


class VocabSubject(BaseModel):
    id: str
    name: str


class VocabSignal(BaseModel):
    key: str
    type: str
    describe: str = ""
    subject: str | None = None
    source: str | None = None


class Vocabulary(BaseModel):
    groups: list[str]
    group_aliases: dict[str, list[str]]
    services: list[VocabService]
    subjects: list[VocabSubject]
    signals: list[VocabSignal]
    allow_words: list[str] = Field(default_factory=lambda: ALLOW_WORDS)
    block_words: list[str] = Field(default_factory=lambda: BLOCK_WORDS)

    def prompt_reference(self) -> str:
        """A compact human/LLM-readable description of the vocabulary."""
        lines = ["GROUPS: " + ", ".join(self.groups)]
        lines.append("SERVICES: " + ", ".join(f"{s.id} (groups: {','.join(s.groups)})" for s in self.services))
        lines.append("SUBJECTS: " + ", ".join(f"{s.id}={s.name}" for s in self.subjects))
        if self.signals:
            lines.append("SIGNALS:")
            for sig in self.signals:
                lines.append(f"  - {sig.key} [{sig.type}] — {sig.describe}")
        return "\n".join(lines)


def build_vocabulary(config: WardenConfig) -> Vocabulary:
    services = [
        VocabService(
            id=s.id, name=s.name, groups=s.groups,
            aliases=SERVICE_ALIASES.get(s.id, [s.id, s.name.lower()]),
        )
        for s in config.services
    ]
    group_aliases = {
        g.name: GROUP_ALIASES.get(g.name, [g.name])
        for g in config.groups
    }
    return Vocabulary(
        groups=[g.name for g in config.groups],
        group_aliases=group_aliases,
        services=services,
        subjects=[VocabSubject(id=s.id, name=s.name) for s in config.subjects],
        signals=[
            VocabSignal(key=s.key, type=s.type.value, describe=s.describe,
                        subject=s.subject, source=s.source)
            for s in config.signals
        ],
    )
