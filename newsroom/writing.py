"""Stage 3: write the article, and revise it when a judge asks."""

from __future__ import annotations

import re
from typing import Any

from .config import Settings
from .llm import BaseLLM
from .models import Article, Candidate, RankingVerdict, Story, StoryDebate, short_hash

ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "body_markdown": {"type": "string"},
        "key_facts": {
            "type": "array",
            "items": {"type": "object", "properties": {"fact": {"type": "string"}, "source_url": {"type": "string"}}},
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "url": {"type": "string"}, "used_for": {"type": "string"}},
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "social_hook": {"type": "string"},
        "take": {"type": "string"},
        "image_brief": {
            "type": "object",
            "properties": {
                "search_queries": {"type": "array", "items": {"type": "string"}},
                "generation_prompt": {"type": "string"},
                "alt_text": {"type": "string"},
            },
        },
    },
}


def clean_slug(slug: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (slug or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:60].strip("-")
    return slug or fallback


def _debate_view(debate: StoryDebate | None) -> dict[str, Any]:
    if not debate or not debate.advocate or not debate.skeptic:
        return {}
    return {
        "advocate": {"score": debate.advocate.score, "argument": debate.advocate.argument, "evidence": debate.advocate.evidence, "risks": debate.advocate.risks},
        "skeptic": {"score": debate.skeptic.score, "argument": debate.skeptic.argument, "evidence": debate.skeptic.evidence, "risks": debate.skeptic.risks},
    }


def _judge_reasons(story_id: str, verdicts: list[RankingVerdict]) -> list[dict[str, Any]]:
    out = []
    for v in verdicts:
        for r in v.ranked:
            if r.get("story_id") == story_id:
                out.append({"judge": v.judge, "rank": r.get("rank"), "score": r.get("score"), "reason": r.get("reason", "")})
    return out


def article_from_output(data: dict[str, Any], story: Story, run_date: str, language: str, previous: Article | None = None) -> Article:
    fallback_slug = "story-" + short_hash(story.id, run_date, length=8)
    slug = clean_slug(data.get("slug", ""), fallback_slug)
    if previous is not None:
        slug = previous.slug
    art = Article(
        id=previous.id if previous else f"{run_date}-{slug}",
        slug=slug,
        story_id=story.id,
        headline=data.get("headline", "").strip()[:140] or story.headline,
        dek=data.get("dek", "").strip(),
        body_markdown=data.get("body_markdown", "").strip(),
        language=language,
        key_facts=[{"fact": k.get("fact", ""), "source_url": k.get("source_url", "")} for k in data.get("key_facts", [])],
        sources=[{"name": s.get("name", ""), "url": s.get("url", ""), "used_for": s.get("used_for", "")} for s in data.get("sources", [])],
        tags=[t.strip().lower() for t in data.get("tags", []) if t.strip()][:8],
        social_hook=data.get("social_hook", "").strip(),
        take=data.get("take", "").strip(),
        image_brief=data.get("image_brief") or (previous.image_brief if previous else {}),
        run_date=run_date,
        version=(previous.version + 1) if previous else 1,
    )
    if previous is not None:
        art.image = previous.image
        art.review = previous.review
        art.investigation = previous.investigation
        if not art.image_brief:
            art.image_brief = previous.image_brief
    return art


def write_article(
    llm: BaseLLM,
    settings: Settings,
    story: Story,
    candidates: list[Candidate],
    debate: StoryDebate | None,
    verdicts: list[RankingVerdict],
    run_date: str,
    investigation: dict[str, Any] | None = None,
) -> Article:
    cands = [c for c in candidates if c.id in story.candidate_ids]
    payload = {
        "run_date": run_date,
        "language": settings.language,
        "story": {"id": story.id, "headline": story.headline, "summary": story.summary, "topic": story.topic},
        "candidates": [
            {"title": c.title, "summary": c.summary, "source": c.source, "url": c.url, "published": c.published, "language": c.language}
            for c in cands
        ],
        "debate": _debate_view(debate),
        "judge_reasons": _judge_reasons(story.id, verdicts),
        "investigation": investigation or {"angles": [], "unanswered": [], "summary": ""},
    }
    data = llm.structured(
        "writer",
        "Write today's article for this story.",
        payload,
        ARTICLE_SCHEMA,
        web_search_uses=settings.web_search_uses("writer"),
    )
    article = article_from_output(data, story, run_date, settings.language)
    article.investigation = investigation or {}
    return article


def revise_article(
    llm: BaseLLM,
    settings: Settings,
    article: Article,
    story: Story,
    required_edits: list[str],
    findings: list[dict[str, Any]],
    defense: dict[str, Any],
) -> Article:
    payload = {
        "run_date": article.run_date,
        "language": settings.language,
        "article": {
            "slug": article.slug,
            "headline": article.headline,
            "dek": article.dek,
            "body_markdown": article.body_markdown,
            "key_facts": article.key_facts,
            "sources": article.sources,
            "tags": article.tags,
            "social_hook": article.social_hook,
            "take": article.take,
            "image_brief": article.image_brief,
        },
        "required_edits": required_edits,
        "findings": findings,
        "defense": defense,
    }
    data = llm.structured("reviser", "Apply the required edits and return the revised article.", payload, ARTICLE_SCHEMA, web_search_uses=settings.web_search_uses("reviser"))
    return article_from_output(data, story, article.run_date, settings.language, previous=article)
