"""DocumentReader — pull the content out of attached documents (the school newsletter).

Why this exists: the weekly newsletter PDF carries per-class and per-child information
that appears **nowhere else** in the portal — a class's sports result, a "Merit Book"
naming individual children. Skipping it means missing the most personal content there is.

Why it can't just be text extraction: these newsletters are image-rich. On a real sample
the text layer yielded 17k characters and did contain "Swordfish" and "Canada", but
"Merit" and "bell" appeared **zero** times — those sections are pictures. Text-only
extraction fails silently, which is the worst way to fail.

So each page is rendered to a JPEG and sent alongside the extracted text. That also keeps
the payload sane: the sample PDF is 27.5 MB (too big to send whole — base64 would exceed
the request limit), while six pages at 140 dpi come to ~2.2 MB.

Where this sits in the contract: adapters stay **mechanical** — the Weduc adapter only
downloads and caches the file, recording it under ``item.raw["_files"]``. Reading and
interpreting is the hub's job (docs/sourceadapterCONTRACT.md §2), which is here.

Results are cached in the kv store keyed by the file's content hash, so a newsletter is
read once no matter how often we poll. Needs the Anthropic API backend (the CLI path has
no image support); with anything else it no-ops with a clear note.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("warden.documents")

_SYSTEM = (
    "You extract facts from a school newsletter for a parent's dashboard. You are given "
    "the PDF's extracted text AND an image of every page (the text layer misses "
    "picture-only sections such as merit lists, so the images are authoritative where "
    "they disagree).\n"
    "Report ONLY what the document actually says — never infer, never invent a name or a "
    "date. If something is unclear or unreadable, leave it out.\n"
    'Reply with ONLY a JSON object:\n'
    '{"summary": "<3-4 sentences covering the whole newsletter>",\n'
    ' "child_mentioned": <true only if the child is named in this document>,\n'
    ' "about_child": [{"page": <n>, "quote": "<short verbatim quote NAMING the child>", '
    '"why": "<what it says about them>"}],\n'
    ' "about_class": [{"page": <n>, "class": "<class name>", "detail": "<what it says>"}],\n'
    ' "key_dates": [{"date": "<YYYY-MM-DD or as printed>", "what": "<event>"}],\n'
    ' "actions_for_parents": [{"what": "<action>", "due": "<when, or empty>"}],\n'
    ' "whole_school": ["<notable item>"]}\n'
    "about_child is ONLY for places the child is actually named. If they are not named, "
    'set child_mentioned false and leave about_child empty — do NOT use it to record an '
    "absence. Anything about their class (a merit list they are not on, a class result) "
    "belongs in about_class instead. Reporting that they are not mentioned is a good "
    "answer; inventing a mention is not."
)


class DocumentReader:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    # ── availability ─────────────────────────────────────────────────────────
    def available(self) -> tuple[bool, str]:
        """Reading needs vision, which needs the Anthropic API path."""
        if not self.ctx.settings.anthropic_api_key:
            return False, "no ANTHROPIC_API_KEY (document reading needs the API, not the CLI)"
        if (self.ctx.settings.llm_backend or "auto").lower() == "off":
            return False, "WARDEN_LLM=off"
        try:
            import fitz  # noqa: F401
        except Exception:
            return False, "pymupdf not installed"
        return True, ""

    # ── cache ────────────────────────────────────────────────────────────────
    @staticmethod
    def _kv_key(key: str) -> str:
        return f"doc:{key}"

    def get_cached(self, key: str) -> Optional[dict[str, Any]]:
        raw = self.ctx.db.get_kv(self._kv_key(key))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    # ── the read ─────────────────────────────────────────────────────────────
    def _render(self, path: Path, dpi: int, max_pages: int) -> tuple[str, list[bytes]]:
        """Extracted text + one JPEG per page."""
        import fitz

        text_parts: list[str] = []
        images: list[bytes] = []
        with fitz.open(str(path)) as doc:
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                try:
                    text_parts.append(f"--- page {i + 1} ---\n{page.get_text() or ''}")
                except Exception:
                    pass
                try:
                    pix = page.get_pixmap(dpi=dpi)
                    images.append(pix.tobytes("jpeg", jpg_quality=75))
                except Exception:
                    pass
        return "\n".join(text_parts), images

    async def read(self, path: Path, name: str, subject_name: str,
                   class_hint: str = "") -> dict[str, Any]:
        """Send one document to the model and return the structured digest."""
        from anthropic import AsyncAnthropic

        opts = self.ctx.config.source("weduc")
        o = opts.options if opts else {}
        dpi = int(o.get("pdf_dpi", 140))
        max_pages = int(o.get("pdf_max_pages", 12))

        text, images = self._render(path, dpi, max_pages)
        if not images and not text.strip():
            raise RuntimeError("nothing readable in the document")

        who = f"The child of interest is {subject_name}"
        if class_hint:
            who += f", in {class_hint}"
        blocks: list[dict[str, Any]] = [{
            "type": "text",
            "text": (f"Document: {name}\n{who}.\n\n"
                     f"EXTRACTED TEXT (may be missing image-only sections):\n{text[:60000]}\n\n"
                     f"Page images follow, in order."),
        }]
        for img in images:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.b64encode(img).decode("ascii")},
            })

        client = AsyncAnthropic(api_key=self.ctx.settings.anthropic_api_key)
        resp = await client.messages.create(
            model=self.ctx.settings.llm_model, max_tokens=3000,
            system=_SYSTEM, messages=[{"role": "user", "content": blocks}])
        payload = "".join(b.text for b in resp.content
                          if getattr(b, "type", None) == "text")
        digest = json.loads(self._extract_json(payload))
        self._verify_child_mentions(digest, subject_name)
        digest["_meta"] = {"document": name, "pages_read": len(images),
                           "dpi": dpi, "text_chars": len(text)}
        return digest

    @staticmethod
    def _verify_child_mentions(digest: dict[str, Any], subject_name: str) -> None:
        """Drop any "about the child" claim whose own quote doesn't name the child.

        Reading names off a graphic (a merit list, an awards panel) is where a vision
        model is least reliable, and it shows: on a real newsletter it reported
        ``quote: "HETE-ROSE"`` as Luke receiving an award, and separately filed his
        class's merit list — which does not include him — under about_child. A false
        "he won an award" is worse than no data, especially if a rule acts on it.

        So the model proposes and this verifies: an entry survives only if its own quote
        contains one of the child's name tokens. Rejected entries are kept under
        `unverified_child_mentions` for transparency rather than silently binned, and
        `child_mentioned` is recomputed from what actually survived.
        """
        tokens = [t.lower() for t in str(subject_name).split() if len(t) >= 3]
        entries = digest.get("about_child")
        if not isinstance(entries, list):
            entries = []

        kept, rejected = [], []
        for e in entries:
            if not isinstance(e, dict):
                continue
            quote = str(e.get("quote") or "")
            if tokens and any(t in quote.lower() for t in tokens):
                kept.append(e)
            else:
                e = dict(e)
                e["_rejected"] = ("quote does not contain the child's name — not treated "
                                  "as a mention")
                rejected.append(e)

        digest["about_child"] = kept
        if rejected:
            digest["unverified_child_mentions"] = rejected
        digest["child_mentioned"] = bool(kept)
        digest["_verification"] = (
            f"about_child entries are quote-verified against '{subject_name}'; "
            f"{len(kept)} kept, {len(rejected)} rejected as unverified.")

    @staticmethod
    def _extract_json(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            import re
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```$", "", s).strip()
        i, j = s.find("{"), s.rfind("}")
        return s[i:j + 1] if i != -1 and j != -1 else s

    # ── driven by the registry after a source run ────────────────────────────
    async def process_items(self, items: list, subject_name: str,
                            class_hint: str = "") -> list[dict[str, Any]]:
        """Read any cached document we haven't already read. Returns the digests."""
        ok, why = self.available()
        candidates: list[tuple[str, Path, str]] = []
        for it in items:
            for f in (getattr(it, "raw", {}) or {}).get("_files", []) or []:
                if str(f.get("ext", "")).lower() != "pdf":
                    continue
                p = Path(f.get("path", ""))
                if p.exists():
                    candidates.append((str(f.get("key") or p.stem), p,
                                       str(f.get("name") or p.name)))
        if not candidates:
            return []
        if not ok:
            log.info("skipping %d document(s): %s", len(candidates), why)
            return []

        out: list[dict[str, Any]] = []
        for key, path, name in candidates:
            cached = self.get_cached(key)
            if cached:
                out.append(cached)
                continue
            try:
                digest = await self.read(path, name, subject_name, class_hint)
            except Exception as e:
                log.warning("reading %s failed: %s: %s", name, type(e).__name__, e)
                await self.ctx.audit("weduc", "document_read", name,
                                     f"failed: {type(e).__name__}: {e}", ok=False)
                continue
            self.ctx.db.set_kv(self._kv_key(key), json.dumps(digest, default=str))
            n_child = len(digest.get("about_child") or [])
            await self.ctx.audit(
                "weduc", "document_read", name,
                f"read {digest.get('_meta', {}).get('pages_read', '?')} pages; "
                f"{n_child} mention(s) of {subject_name}")
            log.info("read document %s (%d child mentions)", name, n_child)
            out.append(digest)
        return out
