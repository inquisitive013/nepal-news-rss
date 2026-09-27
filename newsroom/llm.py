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
import os
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

from .config import Settings
from .models import UsageRecord
from .prompts_loader import system_for

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PAUSE_CONTINUATIONS = 5
DEFAULT_OIDC_AUDIENCE = "https://api.anthropic.com"
CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_IDENTITY_TOKEN",
)


class LLMError(Exception):
    """Something went wrong talking to the model or reading its answer."""


class LLMRefusal(LLMError):
    """The model declined the request. Callers treat this as a stop for that item."""


class BudgetExceeded(LLMError):
    """The run has used its allowance of model calls."""


class CreditExhausted(BudgetExceeded):
    """The Anthropic account has no credit left. Nothing else in this run can succeed."""


MAX_OUTPUT_TOKENS = 128000


def is_credit_error(text: str) -> bool:
    lowered = text.lower()
    return "credit balance" in lowered or "purchase credits" in lowered


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


# --------------------------------------------------------------------------- credentials

def scrub_empty_credentials(environ: dict[str, str] | None = None) -> list[str]:
    """Remove credential variables that are set to an empty string.

    CI systems export every configured secret, present or not. An empty
    ANTHROPIC_API_KEY would otherwise win the SDK's precedence chain and shadow
    identity federation. Returns the names that were removed.
    """
    env = os.environ if environ is None else environ
    removed = []
    for name in CREDENTIAL_ENV_VARS:
        if name in env and env[name].strip() == "":
            del env[name]
            removed.append(name)
    return removed


def oidc_request_url(base_url: str, audience: str) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}audience={quote(audience, safe='')}"


def github_oidc_token_provider(environ: dict[str, str] | None = None) -> Callable[[], str] | None:
    """A provider that mints a fresh GitHub Actions identity token on every call.

    GitHub tokens expire after about five minutes and Anthropic accepts each one
    once, so the token must be minted at exchange time, not once per job.
    """
    env = os.environ if environ is None else environ
    url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        return None
    audience = env.get("ANTHROPIC_OIDC_AUDIENCE") or DEFAULT_OIDC_AUDIENCE
    full_url = oidc_request_url(url, audience)

    def mint() -> str:
        import httpx

        with httpx.Client(timeout=30.0) as client:
            resp = client.get(full_url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
            resp.raise_for_status()
            token = resp.json().get("value", "")
        if not token:
            raise LLMError("GitHub returned no identity token")
        return token

    return mint


def auth_mode(environ: dict[str, str] | None = None) -> str:
    """Which credential the client will use: api_key, auth_token, federation or sdk_default."""
    env = os.environ if environ is None else environ
    if env.get("ANTHROPIC_API_KEY"):
        return "api_key"
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        return "auth_token"
    if env.get("ANTHROPIC_FEDERATION_RULE_ID") and env.get("ANTHROPIC_ORGANIZATION_ID"):
        return "federation"
    return "sdk_default"


def make_credentials(environ: dict[str, str] | None = None):
    """Build explicit federation credentials when the environment asks for them.

    Returns None when an API key or auth token is present (they take precedence,
    matching the SDK), or when federation is not configured, so the SDK's own
    resolution runs. When running inside GitHub Actions the identity token is
    minted on demand; otherwise the SDK's file or literal token providers are used.
    """
    env = os.environ if environ is None else environ
    if auth_mode(env) != "federation":
        return None
    from anthropic.lib.credentials import IdentityTokenFile, WorkloadIdentityCredentials

    provider = github_oidc_token_provider(env)
    if provider is None:
        if env.get("ANTHROPIC_IDENTITY_TOKEN_FILE"):
            provider = IdentityTokenFile(env["ANTHROPIC_IDENTITY_TOKEN_FILE"])
        elif env.get("ANTHROPIC_IDENTITY_TOKEN"):
            literal = env["ANTHROPIC_IDENTITY_TOKEN"]
            provider = lambda: literal  # noqa: E731
        else:
            raise LLMError(
                "Identity federation is configured but no identity token source is available. "
                "Run inside GitHub Actions with id-token: write, or set ANTHROPIC_IDENTITY_TOKEN_FILE."
            )
    return WorkloadIdentityCredentials(
        identity_token_provider=provider,
        federation_rule_id=env["ANTHROPIC_FEDERATION_RULE_ID"],
        organization_id=env["ANTHROPIC_ORGANIZATION_ID"],
        service_account_id=env.get("ANTHROPIC_SERVICE_ACCOUNT_ID") or None,
        workspace_id=env.get("ANTHROPIC_WORKSPACE_ID") or None,
    )


def client_kwargs(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Extra client arguments derived from the environment.

    An API key that is not scoped to one workspace must name the workspace on
    every request. ANTHROPIC_WORKSPACE_ID supplies it through a default header.
    Federation tokens are workspace scoped at exchange time, so they never need it.
    """
    env = os.environ if environ is None else environ
    kwargs: dict[str, Any] = {}
    workspace = env.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace and auth_mode(env) in ("api_key", "auth_token"):
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    return kwargs


def build_client(timeout: float = 600.0, max_retries: int = 3):
    """Construct the Anthropic client with whichever credential the environment provides."""
    import anthropic

    scrub_empty_credentials()
    creds = make_credentials()
    extra = client_kwargs()
    if creds is not None:
        return anthropic.Anthropic(credentials=creds, timeout=timeout, max_retries=max_retries, **extra)
    return anthropic.Anthropic(timeout=timeout, max_retries=max_retries, **extra)


# --------------------------------------------------------------------------- live client

class ClaudeLLM(BaseLLM):
    name = "claude"

    def __init__(self, settings: Settings, meter: UsageMeter | None = None) -> None:
        super().__init__(settings, meter)
        import anthropic  # imported lazily so mock mode never needs the package

        self._anthropic = anthropic
        self.client = build_client()
        self.auth_mode = auth_mode()
        log.info("model access via %s", self.auth_mode)
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
            "max_tokens": max_tokens or int(self.settings.get("llm.max_tokens", 64000)),
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
        cap = max_tokens or int(self.settings.get("llm.max_tokens", 64000))
        while True:
            attempts += 1
            try:
                return self._structured_once(role, user_text + nudge, payload, schema, images, web_search_uses, cap)
            except (LLMRefusal, BudgetExceeded):
                raise
            except LLMError as exc:
                if attempts >= 2:
                    raise
                if "max_tokens" in str(exc):
                    # Thinking counts against the cap. Give the retry real headroom.
                    cap = min(cap * 2, MAX_OUTPUT_TOKENS)
                    log.warning("%s: output hit the cap, retrying with max_tokens=%d", role, cap)
                else:
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
                if is_credit_error(text):
                    raise CreditExhausted("the Anthropic account has run out of credit. Add credit in the Console under Billing.") from exc
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

    def __init__(
        self,
        settings: Settings,
        meter: UsageMeter | None = None,
        *,
        reject_story_ids: set[str] | None = None,
        send_back_story_ids: set[str] | None = None,
        revise_rounds: int = 1,
    ) -> None:
        super().__init__(settings, meter)
        self.reject_story_ids = set(reject_story_ids or ())
        # Stories judge 2 sends back for one more edit before approving.
        self.send_back_story_ids = set(send_back_story_ids or ())
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

    # -- investigation -----------------------------------------------------
    def _investigator(self, p, _images):
        cands = p["candidates"]
        lead = cands[0] if cands else {"source": "Newsroom", "url": ""}
        return {
            "angles": [
                {
                    "claim": "The district office promised embankment repairs on this stretch in its 2024 budget speech and none were built.",
                    "kind": "record",
                    "why_it_matters": "The households moved overnight live behind the wall that was never repaired.",
                    "evidence": [{"url": lead.get("url", "") or "https://example.org/budget-2024", "source": lead["source"], "fact": "Budget speech line item for embankment repair, unspent."}],
                    "confidence": 72,
                },
                {
                    "claim": "Nobody checked the number.",
                    "kind": "numbers",
                    "why_it_matters": "",
                    "evidence": [],
                    "confidence": 30,
                },
            ],
            "unanswered": [{"question": "Where did the embankment money go?", "who_could_answer": "The district development office"}],
            "summary": "The coverage repeats the police toll. The record shows a repair promise that was not kept.",
        }

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
        angles = [a for a in (p.get("investigation") or {}).get("angles", []) if a.get("evidence")]
        if angles:
            body += "\n\n## What the coverage missed\n\n" + "\n\n".join(
                f"{a['claim']} That is in the record kept by {a['evidence'][0].get('source', 'the source')}." for a in angles
            )
            questions = (p.get("investigation") or {}).get("unanswered", [])
            if questions:
                body += "\n\n## What we still do not know\n\n" + "\n".join(f"{q['question']} {q['who_could_answer']} could answer." for q in questions)
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
            "take": "One number is confirmed: 140 households moved to schools overnight. Officials promise a damage assessment on Monday. Until it lands, nobody can say what this night cost.",
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
            "take": art.get("take", ""),
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
        sid = p["article"].get("story_id")
        if sid in self.reject_story_ids:
            return {"decision": "reject", "rulings": rulings, "required_edits": [], "reason": "judge 2: not defensible enough to publish", "scores": scores}
        if sid in self.send_back_story_ids and int(p["revisions_left"]) > 0:
            return {"decision": "revise", "rulings": rulings, "required_edits": ["Put the confirmed number in the headline."], "reason": "judge 2: the headline buries the number", "scores": scores}
        return {"decision": "approve", "rulings": rulings, "required_edits": [], "reason": "judge 2 concurs", "scores": scores}


def make_llm(settings: Settings, meter: UsageMeter | None = None) -> BaseLLM:
    if settings.mock:
        return MockLLM(settings, meter)
    return ClaudeLLM(settings, meter)
