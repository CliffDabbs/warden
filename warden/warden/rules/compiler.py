"""Plain-English → CompiledRule compiler (MODULE B).

Two paths, one public surface:

  * ``compile(text)``          — async. Uses the Anthropic LLM when an API key is
                                 configured; on ANY error it degrades to the
                                 deterministic parser. Works fine with no key.
  * ``compile_fallback(text)`` — sync, deterministic, no network. This is the
                                 critical path: it must correctly compile the
                                 three seed rules (and close variants) into the
                                 exact AST shapes in ``schema.py``.

The fallback resolves free text against the controlled :class:`Vocabulary`
(groups / services / subjects / signals) built from ``config/warden.yaml``. It
parses clock times, day tokens, allow/block verbs, and "<subject> … atom
learning" signal phrases. Emitted cron expressions are validated with croniter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from typing import Any, Optional

from croniter import croniter

from ..config import Settings
from ..models import LlmCall, ServiceState
from .schema import (
    Action,
    CompiledRule,
    ManualTrigger,
    ScheduleTrigger,
    SetGroupAction,
    SetServiceAction,
    SignalTrigger,
    Trigger,
)
from .vocab import Vocabulary, VocabService

log = logging.getLogger("warden.compiler")

# day-of-week names → cron numeric (cron: 0=Sun … 6=Sat)
_DOW_NUM: list[tuple[str, int]] = [
    ("sunday", 0), ("monday", 1), ("tuesday", 2), ("wednesday", 3),
    ("thursday", 4), ("friday", 5), ("saturday", 6),
]
_DOW_LABEL = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
              4: "Thursday", 5: "Friday", 6: "Saturday"}

# completion words that flip a boolean "…daily_complete" signal true
_DONE_RE = re.compile(r"(?<!\w)(complete[ds]?|completes|finish(?:e[ds]|es)?|done)(?!\w)")

# words implying a rule depends on live source data (=> evaluated on-the-fly by the LLM)
_DATA_WORDS = ("min", "mins", "minute", "minutes", "score", "scores", "percent", "passed",
               "pass", "done", "complete", "completed", "finish", "finished", "practice",
               "learning", "atom", "weduc", "form", "forms", "homework", "island", "islands",
               "topic", "topics", "today", "progress", "hour", "hours", "accuracy", "mark",
               "marks", "test", "quiz", "session", "sessions")


def _word(pat: str) -> str:
    """Word-boundary match that also treats apostrophes/spaces as edges."""
    return r"(?<!\w)" + re.escape(pat) + r"(?!\w)"


CLI_TIMEOUT = 45          # seconds to wait for a `claude -p` compile
# Output budget for a JSON completion. Generous because a reasoning model (the evaluator
# runs on WARDEN_EVAL_MODEL, typically a thinking one) spends this on thinking before it
# writes a single character of the answer — at the old 1500 the whole budget could go to
# thinking and return no answer at all. Only tokens actually produced are billed, so the
# headroom is free for the non-thinking models.
DEFAULT_MAX_TOKENS = 8000


class RuleCompiler:
    def __init__(self, settings: Settings, vocab: Vocabulary) -> None:
        self.settings = settings
        self.vocab = vocab
        self._client = None            # lazily-built AsyncAnthropic
        self._cli_ok: Optional[bool] = None
        # Set by main.py to ctx.record_llm. Every exchange this class has with a model is
        # handed to it verbatim, so the Activity feed can show exactly what was asked and
        # exactly what came back. Left None (tests, scripts) nothing is journalled.
        self.journal: Optional[Any] = None

    async def _journal(self, purpose: str, backend: str, model: str, system: str,
                       prompt: str, response: str, started: float,
                       usage: Optional[dict] = None, error: str = "") -> None:
        """Hand one exchange to the recorder. Never fatal to the call it describes."""
        if self.journal is None:
            return
        try:
            await self.journal(LlmCall(
                purpose=purpose, backend=backend, model=model, system=system,
                prompt=prompt, response=response, ok=not error, error=error,
                ms=int((time.perf_counter() - started) * 1000),
                input_tokens=(usage or {}).get("input_tokens"),
                output_tokens=(usage or {}).get("output_tokens")))
        except Exception:
            log.exception("could not journal the %s LLM call", purpose)

    # ── public API ───────────────────────────────────────────────────────────
    async def compile(self, text: str) -> CompiledRule:
        """Compile via the chosen backend; degrade to the parser on ANY error.

        Backend order (see Settings.llm_backend): api (key) → cli (claude -p) → parser.
        """
        backend = self._backend()
        rule: Optional[CompiledRule] = None
        if backend == "api":
            try: rule = await self._compile_llm(text)
            except Exception: rule = None
        elif backend == "cli":
            try: rule = await self._compile_cli(text)
            except Exception: rule = None
        if rule is None:
            rule = self.compile_fallback(text)                 # sets .dynamic itself
        else:
            rule.dynamic = rule.dynamic or self._is_dynamic(text)
        return rule

    def has_llm(self) -> bool:
        """True if a real LLM backend is available (needed for dynamic rules)."""
        return self._backend() != "none"

    def _is_dynamic(self, text: str) -> bool:
        """A rule is 'dynamic' (evaluated live by the LLM) if it depends on source data
        — a person's activity, minutes, scores, completion, forms, etc."""
        t = self._normalize(text)
        if any(self._mentions_subject(t, s.id) for s in self.vocab.subjects):
            return True
        if "%" in t:
            return True
        return any(re.search(_word(w), t) for w in _DATA_WORDS)

    async def complete_json(self, system: str, user: str,
                            model: Optional[str] = None,
                            max_tokens: int = DEFAULT_MAX_TOKENS,
                            purpose: str = "llm") -> dict:
        """Generic LLM call returning parsed JSON, retried once if the JSON is malformed.

        Models occasionally emit not-quite-valid JSON (an unescaped quote, a stray
        delimiter). One observed case blew up a rule evaluation mid-run, so a single
        clean retry happens here rather than letting a formatting slip surface as a
        server error.

        `model` overrides the configured one — the evaluator uses it so its harder
        reasoning can run on a stronger model than the compiler needs. `purpose` is what
        the exchange is filed under in the activity feed (rule_eval:<id>, reminders, …).

        Both attempts are journalled, so a retry shows up AS a retry rather than hiding
        behind whichever answer finally parsed.
        """
        try:
            return await self._complete_json_once(system, user, model, max_tokens, purpose)
        except json.JSONDecodeError as e:
            log.warning("LLM returned malformed JSON (%s); retrying once", e)
            return await self._complete_json_once(
                system + "\n\nIMPORTANT: your previous reply was not valid JSON. Reply "
                         "with a single valid JSON object and nothing else. Keep string "
                         "values short and escape any quotes inside them.",
                user, model, max_tokens, purpose + " (retry)")

    async def _complete_json_once(self, system: str, user: str,
                                  model: Optional[str] = None,
                                  max_tokens: int = DEFAULT_MAX_TOKENS,
                                  purpose: str = "llm") -> dict:
        """One request, one reply — and a verbatim record of both either way.

        The record is written in a `finally` so a call that fails (a timeout, a reply
        that was all thinking and no answer, malformed JSON) is journalled with whatever
        did come back and the error that ended it. Those are exactly the calls worth
        being able to read afterwards.
        """
        backend = self._backend()
        if backend == "none":
            raise RuntimeError("no LLM backend (set ANTHROPIC_API_KEY or install the claude CLI)")
        use_model = model or self.settings.llm_model
        started = time.perf_counter()
        # what actually goes on the wire: the API takes a system prompt and a user
        # message, the CLI takes one concatenated prompt on stdin. Journal it the way
        # it was sent, not the way it was assembled.
        sent_system, sent, text, usage, failure = system, user, "", None, ""
        try:
            if backend == "api":
                from anthropic import AsyncAnthropic
                if self._client is None:
                    self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
                resp = await self._client.messages.create(
                    model=use_model, max_tokens=max_tokens,
                    system=system, messages=[{"role": "user", "content": user}])
                text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
                usage = {"input_tokens": getattr(resp.usage, "input_tokens", None),
                         "output_tokens": getattr(resp.usage, "output_tokens", None)}
                # A reasoning model spends the budget on thinking FIRST, so too small a cap
                # returns a reply that is all thinking and no answer — zero text blocks and
                # stop_reason "max_tokens". That surfaced as an unreadable
                # "Expecting value: line 1 column 1" and lost a seed-3 evaluation on
                # 2026-08-27. Name it instead, so the cause is in the audit line.
                if not text.strip():
                    raise ValueError(
                        f"{use_model} returned no text (stop_reason={resp.stop_reason}; "
                        f"{resp.usage.output_tokens} output tokens). If this is a thinking "
                        f"model, max_tokens={max_tokens} left no room for the answer.")
            else:  # cli
                prompt = system + "\n\n" + user
                sent_system, sent = "", prompt
                args = [self.settings.claude_cli, "-p", "--output-format", "json"]
                m = self._cli_model(use_model)
                if m:
                    args += ["--model", m]
                proc = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(
                        proc.communicate(prompt.encode("utf-8")), timeout=CLI_TIMEOUT)
                except asyncio.TimeoutError:
                    proc.kill(); raise ValueError("claude CLI timed out")
                if proc.returncode != 0:
                    raise ValueError(f"claude CLI exit {proc.returncode}: {err.decode('utf-8','ignore')[:200]}")
                raw = out.decode("utf-8")
                text = raw                       # journal the raw envelope if it won't parse
                text = (json.loads(raw) or {}).get("result", "")
            return json.loads(self._extract_json(text))
        except Exception as e:
            failure = f"{type(e).__name__}: {e}"
            raise
        finally:
            await self._journal(purpose, self.backend_name(), use_model,
                                sent_system, sent,
                                text, started, usage, failure)

    def backend_name(self) -> str:
        """Which compile path is active — surfaced in the UI/About."""
        b = self._backend()
        return {"api": "anthropic-api", "cli": "claude-cli", "none": "builtin-parser"}[b]

    def _backend(self) -> str:
        mode = (self.settings.llm_backend or "auto").lower()
        has_key = bool(self.settings.anthropic_api_key)
        if mode == "off":
            return "none"
        if mode == "api":
            return "api" if has_key else "none"
        if mode == "cli":
            return "cli" if self._cli_available() else "none"
        # auto: prefer a key, then the CLI, then the parser
        if has_key:
            return "api"
        if self._cli_available():
            return "cli"
        return "none"

    def _cli_available(self) -> bool:
        if self._cli_ok is None:
            self._cli_ok = shutil.which(self.settings.claude_cli) is not None
        return self._cli_ok

    def compile_fallback(self, text: str) -> CompiledRule:
        """Deterministic parser — the no-API-key critical path."""
        raw = text.strip()
        t = self._normalize(raw)
        warnings: list[str] = []
        confidence = 1.0

        action, awarn, aconf = self._parse_action(t)
        trigger, twarn, tconf = self._parse_trigger(t)
        warnings += awarn + twarn
        confidence = min(confidence, aconf, tconf)

        dynamic = self._is_dynamic(raw)
        if action is None:
            if dynamic:
                # dynamic rules are evaluated live; a statically-parsed action is optional
                return CompiledRule(
                    trigger=ManualTrigger(describe="evaluated live"),
                    actions=[], summary=raw, confidence=0.6, dynamic=True,
                    warnings=["evaluated on-the-fly by the LLM against live source data"],
                )
            raise ValueError(f"could not parse: no allow/block action found in {raw!r}")

        actions = [action]
        summary = self._summarize(trigger, actions)
        return CompiledRule(
            trigger=trigger, conditions=[], actions=actions, summary=summary,
            confidence=round(confidence, 2), warnings=warnings, dynamic=dynamic,
        )

    # ── trigger parsing ──────────────────────────────────────────────────────
    def _parse_trigger(self, t: str) -> tuple[Trigger, list[str], float]:
        sig = self._parse_signal(t)
        if sig is not None:
            return sig, [], 1.0
        sched, swarn = self._parse_schedule(t)
        if sched is not None:
            return sched, swarn, (0.8 if swarn else 1.0)
        return (
            ManualTrigger(),
            ["no schedule or signal trigger recognised; rule will only run manually"],
            0.5,
        )

    def _parse_schedule(self, t: str) -> tuple[Optional[ScheduleTrigger], list[str]]:
        tm = self._parse_time(t)
        dow, dow_explicit = self._parse_dow(t)
        if tm is None and not dow_explicit:      # neither a clock time nor a day token
            return None, []
        warnings: list[str] = []
        if tm is None:                            # a day token with no time ⇒ assume midnight
            hour, minute = 0, 0
            warnings.append("no time given; assuming 00:00")
        else:
            hour, minute = tm
        cron = f"{minute} {hour} * * {dow}"
        if not croniter.is_valid(cron):
            return None, []
        return ScheduleTrigger(cron=cron, describe=self._describe_schedule(hour, minute, dow)), warnings

    def _parse_signal(self, t: str) -> Optional[SignalTrigger]:
        """Score each vocab signal against the text; return the best match."""
        best = None
        best_score = 0
        for sig in self.vocab.signals:
            score = 0
            if sig.subject and self._mentions_subject(t, sig.subject):
                score += 2
            if sig.source and re.search(_word(sig.source.lower()), t):
                score += 2
            tail = sig.key.rsplit(".", 1)[-1]          # e.g. daily_complete
            for kw in tail.split("_"):
                if len(kw) >= 3 and kw in t:
                    score += 1
            if tail.endswith("complete") and _DONE_RE.search(t):
                score += 2
            if score > best_score:
                best_score, best = score, sig

        if best is None or best_score < 3:
            return None

        describe = f"when {best.describe}" if best.describe else f"when {best.key} becomes true"
        if best.type == "bool":
            return SignalTrigger(
                signal=best.key, edge="becomes_true", comparator="is_true", describe=describe
            )
        # numeric/string signals: parse an optional threshold, else react on change
        thr = self._parse_threshold(t)
        if thr is not None:
            cmp, val = thr
            return SignalTrigger(
                signal=best.key, edge="on_value", comparator=cmp, value=val, describe=describe
            )
        return SignalTrigger(signal=best.key, edge="changes", comparator="!=", describe=describe)

    # ── action parsing ───────────────────────────────────────────────────────
    def _parse_action(self, t: str) -> tuple[Optional[Action], list[str], float]:
        state = self._parse_state(t)
        if state is None:
            return None, [], 1.0

        service = self._find_service(t)
        group = self._find_group(t)

        # A named service ("block youtube") ⇒ set_service; otherwise a group-wide
        # toggle ("disable all kids devices") ⇒ set_group.
        if service is not None:
            warnings: list[str] = []
            conf = 1.0
            if group is None:
                if service.groups:
                    group = service.groups[0]
                    warnings.append(f"no group named; defaulting to '{group}'")
                    conf = 0.7
                else:
                    return None, [f"service '{service.id}' belongs to no group"], 1.0
            return SetServiceAction(group=group, service=service.id, state=state), warnings, conf

        if group is not None:
            return SetGroupAction(group=group, state=state), [], 1.0

        return None, [], 1.0

    def _parse_state(self, t: str) -> Optional[ServiceState]:
        block_at = self._first_index(t, self.vocab.block_words)
        allow_at = self._first_index(t, self.vocab.allow_words)
        if block_at is None and allow_at is None:
            return None
        if block_at is None:
            return ServiceState.allowed
        if allow_at is None:
            return ServiceState.blocked
        return ServiceState.blocked if block_at < allow_at else ServiceState.allowed

    def _find_service(self, t: str) -> Optional[VocabService]:
        best: Optional[VocabService] = None
        best_len = 0
        for svc in self.vocab.services:
            for alias in svc.aliases:
                al = alias.lower()
                if re.search(_word(al), t) and len(al) > best_len:
                    best, best_len = svc, len(al)
        return best

    def _find_group(self, t: str) -> Optional[str]:
        best: Optional[str] = None
        best_len = 0
        for name, aliases in self.vocab.group_aliases.items():
            for alias in aliases:
                al = alias.lower()
                if re.search(_word(al), t) and len(al) > best_len:
                    best, best_len = name, len(al)
        return best

    # ── low-level text helpers ───────────────────────────────────────────────
    @staticmethod
    def _normalize(s: str) -> str:
        s = s.lower()
        s = (s.replace("’", "'").replace("‘", "'")
              .replace("“", '"').replace("”", '"'))
        return re.sub(r"\s+", " ", s).strip()

    @staticmethod
    def _first_index(t: str, words: list[str]) -> Optional[int]:
        best: Optional[int] = None
        for w in words:
            m = re.search(_word(w.lower()), t)
            if m and (best is None or m.start() < best):
                best = m.start()
        return best

    @staticmethod
    def _parse_time(t: str) -> Optional[tuple[int, int]]:
        # HH:MM, optional am/pm  (20:00, 8:30am)
        m = re.search(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?", t)
        if m:
            return RuleCompiler._apply_ampm(int(m.group(1)), m.group(3)), int(m.group(2))
        # H am/pm  (8pm, 8 am)
        m = re.search(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\b", t)
        if m:
            return RuleCompiler._apply_ampm(int(m.group(1)), m.group(2)), 0
        # "at H" bare number  (at 7)
        m = re.search(r"\bat\s+(\d{1,2})\b", t)
        if m:
            return int(m.group(1)) % 24, 0
        return None

    @staticmethod
    def _apply_ampm(hour: int, ap: Optional[str]) -> int:
        if not ap:
            return hour % 24
        ap = ap.replace(".", "")
        if ap == "pm" and hour != 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        return hour % 24

    @staticmethod
    def _parse_dow(t: str) -> tuple[str, bool]:
        """Return (cron day-of-week, was an explicit day token present?)."""
        if re.search(r"\bweekend", t):
            return "0,6", True
        if re.search(r"\bweekday", t):
            return "1-5", True
        days = sorted({num for name, num in _DOW_NUM if re.search(r"\b" + name + r"s?\b", t)})
        if days:
            return ",".join(str(d) for d in days), True
        return "*", False

    @staticmethod
    def _parse_threshold(t: str) -> Optional[tuple[str, float]]:
        m = re.search(r"(at least|more than|greater than|over|>=|>)\s*(\d+(?:\.\d+)?)", t)
        if m:
            cmp = ">=" if m.group(1) in ("at least", ">=") else ">"
            return cmp, float(m.group(2))
        m = re.search(r"(at most|less than|under|below|<=|<)\s*(\d+(?:\.\d+)?)", t)
        if m:
            cmp = "<=" if m.group(1) in ("at most", "<=") else "<"
            return cmp, float(m.group(2))
        return None

    def _mentions_subject(self, t: str, subject_id: str) -> bool:
        subj = next((s for s in self.vocab.subjects if s.id == subject_id), None)
        tokens = [subject_id] + (subj.name.lower().split() if subj else [])
        return any(len(tok) >= 2 and re.search(_word(tok.lower()), t) for tok in tokens)

    # ── human-readable echoes ────────────────────────────────────────────────
    @staticmethod
    def _describe_schedule(hour: int, minute: int, dow: str) -> str:
        ts = f"{hour:02d}:{minute:02d}"
        if dow == "*":
            when = "every day"
        elif dow == "1-5":
            when = "on weekdays"
        elif dow == "0,6":
            when = "on weekends"
        else:
            labels = [_DOW_LABEL.get(int(p), p) for p in dow.split(",") if p.isdigit()]
            when = "on " + ", ".join(f"{lbl}s" for lbl in labels)
        return f"{when} at {ts}"

    def _summarize(self, trigger: Trigger, actions: list[Action]) -> str:
        tp = self._describe_trigger(trigger)
        aps = " and ".join(self._describe_action(a) for a in actions)
        body = f"{tp}, {aps}"
        return body[:1].upper() + body[1:] if body else body

    @staticmethod
    def _describe_trigger(trigger: Trigger) -> str:
        if isinstance(trigger, ScheduleTrigger):
            return trigger.describe or "on a schedule"
        if isinstance(trigger, SignalTrigger):
            return trigger.describe or f"when {trigger.signal} becomes true"
        return "manually"

    @staticmethod
    def _describe_action(a: Action) -> str:
        if isinstance(a, SetServiceAction):
            verb = "allow" if a.state == ServiceState.allowed else "block"
            return f"{verb} {a.service} for {a.group}"
        if isinstance(a, SetGroupAction):
            return (f"enable all devices for {a.group}" if a.state == ServiceState.allowed
                    else f"disable all devices for {a.group}")
        return "run action"

    # ── LLM path ─────────────────────────────────────────────────────────────
    async def _compile_llm(self, text: str) -> CompiledRule:
        from anthropic import AsyncAnthropic

        if self._client is None:
            self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)

        system = self._system_prompt()
        started = time.perf_counter()
        payload, usage, failure = "", None, ""
        try:
            resp = await self._client.messages.create(
                model=self.settings.llm_model,          # default: claude-haiku-4-5-20251001
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": text}],
            )
            payload = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            usage = {"input_tokens": getattr(resp.usage, "input_tokens", None),
                     "output_tokens": getattr(resp.usage, "output_tokens", None)}
            data = json.loads(self._extract_json(payload))
            rule = CompiledRule.model_validate(data)

            # sanity-check any cron the model produced; a bad cron falls back
            if isinstance(rule.trigger, ScheduleTrigger) and not croniter.is_valid(rule.trigger.cron):
                raise ValueError(f"LLM produced invalid cron: {rule.trigger.cron!r}")
            return rule
        except Exception as e:
            failure = f"{type(e).__name__}: {e}"
            raise
        finally:
            # compile() swallows every failure and quietly uses the parser instead, so
            # without this line a rule that the model got wrong leaves no trace at all.
            await self._journal("rule_compile", "anthropic-api", self.settings.llm_model,
                                system, text, payload, started, usage, failure)

    # ── CLI path (Claude Code, no API key) ───────────────────────────────────
    async def _compile_cli(self, text: str) -> CompiledRule:
        """Compile by shelling out to `claude -p` — uses the CLI's own login.

        The prompt is piped on stdin (avoids arg-length/quoting issues). We ask for
        JSON, take the envelope's `result`, and validate it exactly like the SDK path.
        """
        prompt = (
            self._system_prompt()
            + "\n\nINSTRUCTION (compile this one rule):\n"
            + text.strip()
            + "\n\nOutput ONLY the JSON object."
        )
        args = [self.settings.claude_cli, "-p", "--output-format", "json"]
        model = self._cli_model()
        if model:
            args += ["--model", model]

        started = time.perf_counter()
        result_text, failure = "", ""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=CLI_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise ValueError("claude CLI timed out")
            if proc.returncode != 0:
                raise ValueError(f"claude CLI exit {proc.returncode}: {err.decode('utf-8', 'ignore')[:200]}")

            raw = out.decode("utf-8")
            result_text = raw                    # journal the raw envelope if it won't parse
            envelope = json.loads(raw)
            result_text = envelope.get("result") if isinstance(envelope, dict) else None
            if not result_text:
                result_text = raw
                raise ValueError("claude CLI returned no result")
            rule = CompiledRule.model_validate(json.loads(self._extract_json(result_text)))
            if isinstance(rule.trigger, ScheduleTrigger) and not croniter.is_valid(rule.trigger.cron):
                raise ValueError(f"CLI produced invalid cron: {rule.trigger.cron!r}")
            return rule
        except Exception as e:
            failure = f"{type(e).__name__}: {e}"
            raise
        finally:
            await self._journal("rule_compile", "claude-cli", model or "cli default",
                                "", prompt, result_text or "", started, None, failure)

    def _cli_model(self, model: Optional[str] = None) -> Optional[str]:
        """Map a model id to a CLI alias (cheap by default)."""
        m = (model or self.settings.llm_model or "").lower()
        if "haiku" in m:
            return "haiku"
        if "sonnet" in m:
            return "sonnet"
        if "opus" in m:
            return "opus"
        return None            # let the CLI use its own default model

    @staticmethod
    def _extract_json(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```$", "", s).strip()
        i, j = s.find("{"), s.rfind("}")
        return s[i:j + 1] if i != -1 and j != -1 else s

    def _system_prompt(self) -> str:
        return (
            'You are the rule compiler for "Warden", a parental-control hub. Convert ONE '
            "plain-English instruction into a single JSON object describing a compiled rule. "
            "Respond with JSON ONLY — no prose, no markdown fences.\n\n"
            "VOCABULARY (use these exact ids):\n"
            f"{self.vocab.prompt_reference()}\n\n"
            "OUTPUT SHAPE (CompiledRule):\n"
            "{\n"
            '  "trigger": one of\n'
            '     {"kind":"schedule","cron":"<min hour dom mon dow>","describe":"..."}\n'
            '     {"kind":"signal","signal":"<signal key>","edge":"becomes_true|becomes_false|changes|on_value",'
            '"comparator":"is_true|is_false|==|!=|>|>=|<|<=","value":<bool|number|string|null>,"describe":"..."}\n'
            '     {"kind":"manual","describe":"..."},\n'
            '  "conditions": [],\n'
            '  "actions": [ one or more of\n'
            '     {"kind":"set_service","group":"<group>","service":"<service id>","state":"allowed|blocked"}\n'
            '     {"kind":"set_group","group":"<group>","state":"allowed|blocked"}\n'
            '     {"kind":"set_client","client":"<name>","state":"allowed|blocked"}\n'
            '     {"kind":"set_protection","enabled":<bool>}\n'
            '     {"kind":"add_rule","rule":"<adguard filter>"}\n'
            '     {"kind":"remove_rule","rule":"<adguard filter>"}\n'
            '     {"kind":"set_host_service","service":"<host service id>","running":<bool>} ],\n'
            '  "summary": "<one-line human echo>",\n'
            '  "confidence": <0..1>,\n'
            '  "warnings": []\n'
            "}\n\n"
            "RULES:\n"
            '- "block"/"disable"/"turn off" => state "blocked"; "allow"/"enable"/"turn on" => "allowed".\n'
            '- A named service ("block youtube") => set_service; "all <group> devices" / '
            '"disable all the kids\' devices" => set_group.\n'
            "- Cron is: minute hour day-of-month month day-of-week (dow 0=Sun..6=Sat). "
            '"every day" => "*", weekdays => "1-5", weekends => "0,6". 8pm => hour 20. '
            "Emit a valid 5-field cron.\n"
            '- "when <subject> has completed/finished their atom learning" => a signal trigger '
            "on the atom.<subject>.daily_complete signal, edge becomes_true, comparator is_true.\n"
            "- Only reference groups/services/signals that exist in the VOCABULARY.\n"
            '- set_host_service stops or starts a REAL service for the whole house (it is '
            "not per-group). Use it only when the instruction plainly means the service "
            'itself — "stop the Plex server", or an "everything off" that names it. A '
            "rule about what the kids can watch is a set_service, not this.\n"
            "Return JSON only."
        )


# ── self-check: compile the three seed rules and print their ASTs ────────────
if __name__ == "__main__":
    from pathlib import Path

    from ..config import Settings as _Settings, load_config
    from .vocab import build_vocabulary

    cfg_path = Path(__file__).resolve().parents[2] / "config" / "warden.yaml"
    config = load_config(cfg_path)
    compiler = RuleCompiler(_Settings.from_env(), build_vocabulary(config))

    seeds = [
        "At 8pm every day, disable all kids devices",
        "At 8pm every day, block YouTube for the kids",
        "When Luke has completed his Atom Learning, allow YouTube for the kids",
    ]
    for rule_text in seeds:
        compiled = compiler.compile_fallback(rule_text)
        print("=" * 68)
        print("RULE:", rule_text)
        print(compiled.model_dump_json(indent=2))
