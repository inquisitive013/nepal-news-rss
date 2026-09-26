"""Model access for every role.

`ClaudeLLM` talks to the Claude API through the official SDK. Each call asks for
one JSON object that matches a schema, may use the server side web search tool,
and streams so long research turns do not hit HTTP timeouts.

`MockLLM` implements the same interface with deterministic outputs so the whole
pipeline can run offline in tests and CI.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

from .config import Settings
from .models import UsageRecord
from .prompts_loader import system_for

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PAUSE_CONTINUATIONS = 5


class LLMError(Exception):
    """Something went wrong talking to the model or reading its answer."""


class LLMRefusal(LLMError):
    """The model declined the request. Callers treat this as a stop for that item."""


class BudgetExceeded(LLMError):
    """The run has used its allowance of model calls."""


# --------------------------------------------------------------------------- schema helpers

def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy where every object forbids extra keys and requires every property.

    Structured outputs demand `additionalProperties: false` on each object. Making
    every property required keeps the shape predictable for the rest of the code.
    """
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)

    walk(out)
    return out


def validate_against_schema(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Light structural validation. Returns a list of problems (empty means fine)."""
    problems: list[str] = []
    stype = schema.get("type")
    if "enum" in schema and data not in schema["enum"]:
        problems.append(f"{path}: {data!r} not in {schema['enum']}")
    if stype == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object"]
        for key in schema.get("required", []):
            if key not in data:
                problems.append(f"{path}.{key}: missing")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                problems.extend(validate_against_schema(data[key], sub, f"{path}.{key}"))
    elif stype == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array"]
        items = schema.get("items")
        if items:
            for i, val in enumerate(data):
                problems.extend(validate_against_schema(val, items, f"{path}[{i}]"))
    elif stype == "string" and not isinstance(data, str):
        problems.append(f"{path}: expected string")
    elif stype == "integer" and not (isinstance(data, int) and not isinstance(data, bool)):
        problems.append(f"{path}: expected integer")
    elif stype == "number" and not isinstance(data, (int, float)):
        problems.append(f"{path}: expected number")
    elif stype == "boolean" and not isinstance(data, bool):
        problems.append(f"{path}: expected boolean")
    return problems


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of model text, tolerating code fences and prose around it."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        val = json.loads(cleaned)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError("no JSON object found in model output")
    val = json.loads(cleaned[start : end + 1])
    if not isinstance(val, dict):
        raise LLMError("model output JSON is not an object")
    return val


# --------------------------------------------------------------------------- usage meter

class UsageMeter:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.records: list[UsageRecord] = []
        self._lock = threading.Lock()
        self.calls = 0

    def reserve(self) -> None:
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"reached the limit of {self.max_calls} model calls for this run")
            self.calls += 1

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self.records.append(rec)


# --------------------------------------------------------------------------- base

def _payload_block(user_text: str, payload: dict[str, Any]) -> str:
    return (
        f"{user_text.strip()}\n\n<input>\n"
        + json.dumps(payload, ensure_ascii=False, indent=1, default=str)
        + "\n</input>\n\nAnswer with one JSON object that matches the required schema. No prose outside the JSON."
    )


class BaseLLM:
    name = "base"

    def __init__(self, settings: Settings, meter: UsageMeter | None = None) -> None:
        self.settings = settings
        self.meter = meter or UsageMeter(int(settings.get("pipeline.max_llm_calls", 90)))

    def structured(
        self,
        role: str,
        user_text: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        *,
        images: list[tuple[bytes, str]] | None = None,
        web_search_uses: int = 0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


# --------------------------------------------------------------------------- live client

class ClaudeLLM(BaseLLM):
    name = "claude"

    def __init__(self, settings: Settings, meter: UsageMeter | None = None) -> None:
        super().__init__(settings, meter)
        import anthropic  # imported lazily so mock mode never needs the package

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=600.0, max_retries=3)
        self.use_fallback = bool(settings.get("llm.refusal_fallback", True))
        self.use_format = True
        self._lock = threading.Lock()

    # -- request building -------------------------------------------------
    def _messages(self, user_text: str, payload: dict[str, Any], images, schema_hint: str | None) -> list[dict]:
        content: list[dict[str, Any]] = []
        for data, media_type in images or []:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode("ascii")},
                }
            )
        text = _payload_block(user_text, payload)
        if schema_hint:
            text += "\n\nRequired JSON schema:\n" + schema_hint
        content.append({"type": "text", "text": text})
        return [{"role": "user", "content": content}]

    def _request_kwargs(self, role: str, messages: list[dict], schema: dict, web_search_uses: int, max_tokens: int | None) -> dict:
        model = self.settings.role_model(role)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or int(self.settings.get("llm.max_tokens", 16000)),
            "system": [{"type": "text", "text": system_for(role, self.settings), "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        output_config: dict[str, Any] = {}
        if "haiku" not in model:
            output_config["effort"] = self.settings.role_effort(role)
        if self.use_format:
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        if output_config:
            kwargs["output_config"] = output_config
        if web_search_uses > 0:
            kwargs["tools"] = [{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": int(web_search_uses)}]
        return kwargs

    def _create(self, kwargs: dict):
        if self.use_fallback:
            with self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as stream:
                return stream.get_final_message()
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    # -- main entry point ---------------------------------------------------
    def structured(self, role, user_text, payload, schema, *, images=None, web_search_uses=0, max_tokens=None):
        attempts = 0
        nudge = ""
        while True:
            attempts += 1
            try:
                return self._structured_once(role, user_text + nudge, payload, schema, images, web_search_uses, max_tokens)
            except LLMRefusal:
                raise
            except LLMError as exc:
                if attempts >= 2:
                    raise
                log.warning("%s: retrying after %s", role, exc)
                nudge = "\n\nYour previous answer was not valid JSON for the schema. Return only the JSON object."

    def _structured_once(self, role, user_text, payload, schema, images, web_search_uses, max_tokens) -> dict[str, Any]:
        self.meter.reserve()
        anthropic = self._anthropic
        schema_hint = None if self.use_format else json.dumps(strict_schema(schema))
        messages = self._messages(user_text, payload, images, schema_hint)
        kwargs = self._request_kwargs(role, messages, schema, web_search_uses, max_tokens)
        started = time.time()
        continuations = 0
        while True:
            try:
                msg = self._create(kwargs)
            except anthropic.BadRequestError as exc:
                text = str(exc)
                if self.use_fallback and ("fallback" in text.lower()):
                    log.warning("server side fallback rejected by the API, continuing without it: %s", text[:200])
                    with self._lock:
                        self.use_fallback = False
                    continue
                if self.use_format and any(k in text.lower() for k in ("output_config", "json_schema", "format")):
                    log.warning("structured output format rejected, falling back to prompt guided JSON: %s", text[:200])
                    with self._lock:
                        self.use_format = False
                    messages = self._messages(user_text, payload, images, json.dumps(strict_schema(schema)))
                    kwargs = self._request_kwargs(role, messages, schema, web_search_uses, max_tokens)
                    continue
                raise LLMError(f"bad request for {role}: {text[:300]}") from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(f"API error for {role}: {exc.status_code} {str(exc)[:200]}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError(f"connection error for {role}: {exc}") from exc

            self._record_usage(role, kwargs["model"], msg, time.time() - started)
            if msg.stop_reason == "pause_turn":
                continuations += 1
                if continuations > MAX_PAUSE_CONTINUATIONS:
                    raise LLMError(f"{role}: research turn paused too many times")
                kwargs["messages"] = [messages[0], {"role": "assistant", "content": msg.content}]
                continue
            break

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            why = getattr(details, "explanation", "") if details else ""
            raise LLMRefusal(f"{role}: the model declined this request. {why}".strip())
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"{role}: output hit max_tokens, raise llm.max_tokens")

        texts = [b.text for b in msg.content if getattr(b, "type", "") == "text" and getattr(b, "text", "").strip()]
        if not texts:
            raise LLMError(f"{role}: no text block in response")
        data = extract_json(texts[-1])
        problems = validate_against_schema(data, strict_schema(schema))
        if problems:
            raise LLMError(f"{role}: output failed schema check: {problems[:5]}")
        return data

    def _record_usage(self, role: str, model: str, msg, seconds: float) -> None:
        usage = getattr(msg, "usage", None)
        rec = UsageRecord(role=role, model=getattr(msg, "model", model) or model, seconds=round(seconds, 1))
        if usage is not None:
            rec.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            rec.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            rec.cache_read_input_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            rec.cache_creation_input_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            stu = getattr(usage, "server_tool_use", None)
            if stu is not None:
                rec.web_search_requests = int(getattr(stu, "web_search_requests", 0) or 0)
        self.meter.record(rec)


# --------------------------------------------------------------------------- mock client

def _h(*parts: str) -> int:
    return int(hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8], 16)


def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-") or fallback


class MockLLM(BaseLLM):
    """Deterministic stand in. Shapes match the schemas the stages ask for."""

    name = "mock"

    def __init__(self, settings: Settings, meter: UsageMeter | None = None, *, reject_story_ids: set[str] | None = None, revise_rounds: int = 1) -> None:
        super().__init__(settings, meter)
        self.reject_story_ids = set(reject_story_ids or ())
        self.revise_rounds = revise_rounds
        self.calls: list[str] = []

    def structured(self, role, user_text, payload, schema, *, images=None, web_search_uses=0, max_tokens=None):
        self.meter.reserve()
        self.calls.append(role)
        handler = getattr(self, f"_{role}", None)
        if handler is None:
            raise LLMError(f"MockLLM has no handler for role {role}")
        data = handler(payload, images or [])
        problems = validate_against_schema(data, strict_schema(schema))
        if problems:
            raise LLMError(f"mock output for {role} failed schema: {problems[:5]}")
        self.meter.record(UsageRecord(role=role, model="mock", input_tokens=1000, output_tokens=400))
        return data

    # -- ranking -----------------------------------------------------------
    def _story_clusterer(self, p, _images):
        from .discovery import jaccard, title_tokens

        groups: list[list[dict]] = []
        for cand in p["candidates"]:
            toks = title_tokens(cand["title"])
            for group in groups:
                if any(jaccard(toks, title_tokens(g["title"])) >= 0.5 for g in group):
                    group.append(cand)
                    break
            else:
                groups.append([cand])
        stories = []
        preferred = self.settings.language
        for group in groups[: int(p["max_stories"])]:
            lead = next((c for c in group if c.get("language") == preferred), group[0])
            ids = [c["id"] for c in group]
            stories.append(
                {
                    "id": "s_" + hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:10],
                    "headline": lead["title"],
                    "summary": lead.get("summary") or lead["title"],
                    "topic": "general",
                    "candidate_ids": ids,
                    "languages": sorted({c.get("language", "unknown") for c in group}),
                }
            )
        return {"stories": stories, "notes": "mock clustering by headline overlap"}

    def _advocate(self, p, _images):
        story = p["story"]
        score = 55 + _h(story["id"], "adv") % 40
        return {
            "score": score,
            "argument": f"{story['headline']} affects many households and has fresh, checkable numbers.",
            "evidence": [{"url": c.get("url", ""), "note": c["source"]} for c in p["candidates"][:2]],
            "virality_factors": ["broad impact", "clear number in the headline"],
            "risks": ["thin sourcing if only one outlet carries it"],
        }

    def _skeptic(self, p, _images):
        story = p["story"]
        score = max(5, p["advocate"]["score"] - 10 - _h(story["id"], "sk") % 15)
        return {
            "score": score,
            "argument": "Coverage so far leans on a single press statement. Independent confirmation is thin.",
            "evidence": [{"url": c.get("url", ""), "note": "same claim repeated"} for c in p["candidates"][:1]],
            "virality_factors": [],
            "risks": ["single source", "could be routine"],
        }

    def _ranking_judge(self, p, _images):
        if p["judge_position"] == 1:
            scored = []
            for d in p["debates"]:
                adv = d["advocate"]["score"]
                sk = d["skeptic"]["score"]
                scored.append((round((adv + sk) / 2), d["story_id"]))
            scored.sort(reverse=True)
            ranked = [
                {"story_id": sid, "rank": i + 1, "score": sc, "reason": "advocate case stronger than the skeptic's objections"}
                for i, (sc, sid) in enumerate(scored)
                if sc >= 40
            ]
            rejected = [{"story_id": sid, "reason": "too weak on both impact and sourcing"} for sc, sid in scored if sc < 40]
            return {"ranked": ranked, "rejected": rejected, "notes": "mock judge 1"}
        prev = p.get("previous_verdict") or {"ranked": [], "rejected": []}
        ranked, rejected = [], list(prev.get("rejected", []))
        for item in prev.get("ranked", []):
            if item["story_id"] in self.reject_story_ids:
                rejected.append({"story_id": item["story_id"], "reason": "judge 2: sourcing does not support the rank"})
            else:
                ranked.append(item)
        for i, item in enumerate(ranked):
            item["rank"] = i + 1
        return {"ranked": ranked, "rejected": rejected, "notes": "mock judge 2 confirms judge 1 with noted exceptions"}

    # -- writing -----------------------------------------------------------
    def _writer(self, p, _images):
        story = p["story"]
        cands = p["candidates"]
        lead = cands[0] if cands else {"source": "Newsroom", "url": "", "summary": ""}
        headline = story["headline"][:88]
        body = (
            f"{story['summary']}\n\n"
            f"{lead['source']} reported the development first. Two other outlets carried the same figures within hours.\n\n"
            "The number that matters is 140. That is how many households moved overnight, according to police quoted by the outlet.\n\n"
            "## Why it matters\n\n"
            "Riverside settlements flood most monsoons. This year the water rose faster than the warning system.\n\n"
            "## What happens next\n\n"
            "Officials said a damage assessment starts on Monday."
        )
        sources = [{"name": c["source"], "url": c.get("url", ""), "used_for": "primary report"} for c in cands[:3]]
        return {
            "slug": _slugify(headline, "story-" + story["id"][2:]),
            "headline": headline,
            "dek": "The first verified numbers, and what officials say comes next.",
            "body_markdown": body,
            "key_facts": [{"fact": "140 households moved to schools", "source_url": lead.get("url", "")}],
            "sources": sources,
            "tags": ["nepal", story.get("topic", "general")],
            "social_hook": f"{headline}. Here is what is confirmed.",
            "image_brief": {
                "search_queries": [story["headline"][:50], "Kathmandu Nepal"],
                "generation_prompt": "Editorial illustration of a Kathmandu street scene during monsoon rain, no text, no identifiable faces",
                "alt_text": f"Illustration for: {headline}",
            },
        }

    def _reviser(self, p, _images):
        art = dict(p["article"])
        body = art["body_markdown"].replace("The number that matters is 140.", "Police put the number of households moved at 140, a figure the outlet attributed to the district office.")
        art["body_markdown"] = body
        art["dek"] = art.get("dek", "")
        art.setdefault("image_brief", {"search_queries": [], "generation_prompt": "", "alt_text": ""})
        return {
            "slug": art["slug"],
            "headline": art["headline"],
            "dek": art["dek"],
            "body_markdown": art["body_markdown"],
            "key_facts": art.get("key_facts", []),
            "sources": art.get("sources", []),
            "tags": art.get("tags", []),
            "social_hook": art.get("social_hook", ""),
            "image_brief": art["image_brief"],
        }

    # -- images ------------------------------------------------------------
    def _image_picker(self, p, images):
        if p["candidates"]:
            return {"chosen_index": 0, "reason": "first candidate shows the place named in the headline", "alt_text": p.get("alt_hint") or p["headline"]}
        return {"chosen_index": -1, "reason": "no candidate matched the story", "alt_text": p.get("alt_hint") or p["headline"]}

    # -- validation --------------------------------------------------------
    def _red_team(self, p, _images):
        rnd = int(p["round"])
        if rnd <= self.revise_rounds:
            findings = [
                {
                    "id": f"f{rnd}-1",
                    "dimension": "accuracy",
                    "severity": "high",
                    "passage": "The number that matters is 140.",
                    "problem": "The figure is asserted without attribution in the body.",
                    "evidence_url": "",
                    "suggested_fix": "Attribute the figure to the police via the outlet that reported it.",
                }
            ]
            scores = {"accuracy": 70, "relevance": 85, "defensibility": 72, "virality": 68}
        else:
            findings = [
                {
                    "id": f"f{rnd}-1",
                    "dimension": "virality",
                    "severity": "low",
                    "passage": "headline",
                    "problem": "Headline could carry the number.",
                    "evidence_url": "",
                    "suggested_fix": "Optional.",
                }
            ]
            scores = {"accuracy": 92, "relevance": 88, "defensibility": 90, "virality": 70}
        return {"findings": findings, "scores": scores, "summary": "mock red team round %d" % rnd}

    def _defense(self, p, _images):
        return {
            "responses": [
                {"finding_id": f["id"], "stance": "accept", "response": "Fair. Will attribute.", "proposed_edit": f.get("suggested_fix", "")}
                for f in p["findings"]
            ],
            "summary": "accepts the findings",
        }

    def _validation_judge(self, p, _images):
        findings = p["red_team"].get("findings", [])
        highs = [f for f in findings if f["severity"] == "high"]
        rulings = [{"finding_id": f["id"], "ruling": "sustained", "note": "well founded"} for f in findings]
        scores = p["red_team"].get("scores", {"accuracy": 80, "relevance": 80, "defensibility": 80, "virality": 60})
        if p["judge_position"] == 1:
            if highs and int(p["revisions_left"]) > 0:
                return {"decision": "revise", "rulings": rulings, "required_edits": [f["suggested_fix"] for f in highs], "reason": "attribution gap", "scores": scores}
            if highs:
                return {"decision": "reject", "rulings": rulings, "required_edits": [], "reason": "unresolved accuracy problem", "scores": scores}
            return {"decision": "approve", "rulings": rulings, "required_edits": [], "reason": "claims trace to sources", "scores": scores}
        if p["article"].get("story_id") in self.reject_story_ids:
            return {"decision": "reject", "rulings": rulings, "required_edits": [], "reason": "judge 2: not defensible enough to publish", "scores": scores}
        return {"decision": "approve", "rulings": rulings, "required_edits": [], "reason": "judge 2 concurs", "scores": scores}


def make_llm(settings: Settings, meter: UsageMeter | None = None) -> BaseLLM:
    if settings.mock:
        return MockLLM(settings, meter)
    return ClaudeLLM(settings, meter)
