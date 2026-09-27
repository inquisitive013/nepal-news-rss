"""Stage 6b: every approved story is rendered in everyday Nepali, and a judge checks it against the original."""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings
from .llm import BaseLLM
from .models import Article

log = logging.getLogger(__name__)

TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "take": {"type": "string"},
        "body_markdown": {"type": "string"},
        "image_headline": {"type": "string"},
        "social_hook": {"type": "string"},
        "caption": {
            "type": "object",
            "properties": {"hook": {"type": "string"}, "body": {"type": "string"}, "trigger": {"type": "string"}},
        },
        "notes": {"type": "string"},
    },
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "revise"]},
        "problems": {
            "type": "array",
            "items": {"type": "object", "properties": {"passage": {"type": "string"}, "problem": {"type": "string"}, "fix": {"type": "string"}}},
        },
        "reason": {"type": "string"},
    },
}

FIELDS = ("headline", "dek", "take", "body_markdown", "image_headline", "social_hook")


def _english(article: Article) -> dict[str, Any]:
    return {
        "headline": article.headline,
        "dek": article.dek,
        "take": article.take,
        "body_markdown": article.body_markdown,
        "image_headline": article.image_headline,
        "social_hook": article.social_hook,
        "caption": article.caption or {},
        "key_facts": article.key_facts,
        "sources": [{"name": s.get("name", ""), "url": s.get("url", "")} for s in article.sources],
    }


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: str(data.get(k, "") or "").strip() for k in FIELDS}
    cap = data.get("caption") or {}
    out["caption"] = {k: str(cap.get(k, "") or "").strip() for k in ("hook", "body", "trigger")}
    out["notes"] = str(data.get("notes", "") or "").strip()
    return out


def usable(nepali: dict[str, Any] | None) -> bool:
    return bool(nepali and nepali.get("headline") and nepali.get("body_markdown"))


def wanting(settings: Settings, *, only: list[str] | None = None, everything: bool = False, limit: int = 0) -> list[Article]:
    """The stored articles to translate: the ones without a usable Nepali version unless told otherwise."""
    from . import publish  # publish imports this module, so the import stays local

    articles = publish.load_articles(settings)
    if only:
        ids = set(only)
        wanted = [a for a in articles if a.id in ids]
    elif everything:
        wanted = list(articles)
    else:
        wanted = [a for a in articles if not usable(a.nepali)]
    return wanted[:limit] if limit and limit > 0 else wanted


def backfill(settings: Settings, llm: BaseLLM, articles: list[Article]) -> list[tuple[str, str, str]]:
    """Translate and store each article in turn. Returns (article id, status, detail) per attempt.

    A spent call budget ends the loop; a refusal or model error skips the article. Everything
    translated before that point is already saved.
    """
    from . import publish
    from .llm import BudgetExceeded, LLMError, LLMRefusal

    out: list[tuple[str, str, str]] = []
    for article in articles:
        try:
            article.nepali = nepali_for(llm, settings, article)
        except BudgetExceeded as exc:
            log.warning("stopping before %s: %s", article.id, exc)
            break
        except (LLMRefusal, LLMError) as exc:
            log.warning("translation failed for %s: %s", article.id, exc)
            out.append((article.id, "failed", str(exc)[:200]))
            continue
        publish.save_article(settings, article)
        out.append((article.id, "translated", article.nepali.get("headline", "")))
    return out


def nepali_for(llm: BaseLLM, settings: Settings, article: Article) -> dict[str, Any]:
    """Translate, check once, fix once. Returns the Nepali fields plus the judge's verdict."""
    english = _english(article)
    data = llm.structured("translator", "Render this story in everyday Nepali, faithful to every fact.", {"run_date": article.run_date, "article": english}, TRANSLATION_SCHEMA)
    nepali = _clean(data)
    check = llm.structured(
        "translation_judge",
        "Check the Nepali version against the English original.",
        {"english": english, "nepali": {k: nepali[k] for k in (*FIELDS, "caption")}},
        CHECK_SCHEMA,
    )
    decision = check.get("decision", "approve")
    problems = [p for p in (check.get("problems") or []) if (p.get("problem") or "").strip()]
    if decision == "revise" and problems:
        fixed = llm.structured(
            "translator",
            "Apply these fixes to the Nepali version and return it complete.",
            {"run_date": article.run_date, "article": english, "nepali": {k: nepali[k] for k in (*FIELDS, "caption")}, "fixes": problems},
            TRANSLATION_SCHEMA,
        )
        nepali = _clean(fixed)
    nepali["checked"] = decision == "approve" or bool(problems)
    nepali["judge"] = str(check.get("reason", "") or "").strip()
    nepali["problems_fixed"] = len(problems) if decision == "revise" else 0
    return nepali
