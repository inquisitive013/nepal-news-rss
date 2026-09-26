"""Stage 5: red team against defence, judge 1 rules, revisions, judge 2 confirms."""

from __future__ import annotations

import logging
from typing import Any, Callable

from .config import Settings
from .llm import BaseLLM, LLMError, LLMRefusal
from .models import Article, Candidate, ReviewRecord, Story, ValidationRound

log = logging.getLogger(__name__)

SCORES = {
    "type": "object",
    "properties": {
        "accuracy": {"type": "integer"},
        "relevance": {"type": "integer"},
        "defensibility": {"type": "integer"},
        "virality": {"type": "integer"},
    },
}

RED_TEAM_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "dimension": {"type": "string", "enum": ["accuracy", "relevance", "defensibility", "virality"]},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "passage": {"type": "string"},
                    "problem": {"type": "string"},
                    "evidence_url": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
        "scores": SCORES,
        "summary": {"type": "string"},
    },
}

DEFENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "stance": {"type": "string", "enum": ["accept", "contest"]},
                    "response": {"type": "string"},
                    "proposed_edit": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

RULING_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "rulings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "ruling": {"type": "string", "enum": ["sustained", "overruled"]},
                    "note": {"type": "string"},
                },
            },
        },
        "required_edits": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "scores": SCORES,
    },
}

Reviser = Callable[[Article, list[str], list[dict[str, Any]], dict[str, Any]], Article]


def article_view(article: Article) -> dict[str, Any]:
    image = None
    if article.image:
        image = {"kind": article.image.credit.kind, "credit": article.image.credit.line(), "license": article.image.credit.license, "alt": article.image.alt}
    return {
        "id": article.id,
        "story_id": article.story_id,
        "headline": article.headline,
        "dek": article.dek,
        "body_markdown": article.body_markdown,
        "key_facts": article.key_facts,
        "sources": article.sources,
        "tags": article.tags,
        "social_hook": article.social_hook,
        "image": image,
        "version": article.version,
    }


def _judge(llm: BaseLLM, settings: Settings, position: int, article: Article, red: dict, defense: dict, previous: dict | None, round_no: int, revisions_left: int) -> dict[str, Any]:
    payload = {
        "run_date": article.run_date,
        "judge_position": position,
        "round": round_no,
        "revisions_left": revisions_left,
        "guidance": settings.get("editorial.guidance", {}),
        "article": article_view(article),
        "red_team": red,
        "defense": defense,
        "previous_ruling": previous,
    }
    task = "Rule on the red team findings and decide whether this article publishes." if position == 1 else "Give the independent second ruling on this article."
    data = llm.structured("validation_judge", task, payload, RULING_SCHEMA)
    if data.get("decision") == "revise" and revisions_left <= 0:
        data["decision"] = "reject"
        data["reason"] = (data.get("reason", "") + " No revision rounds left.").strip()
    return data


def validate_article(
    llm: BaseLLM,
    settings: Settings,
    article: Article,
    story: Story,
    candidates: list[Candidate],
    reviser: Reviser,
) -> tuple[Article, ReviewRecord]:
    """Run the adversarial review. Returns the (possibly revised) article and its record."""
    record = article.review or ReviewRecord()
    max_revisions = int(settings.get("pipeline.max_revisions", 2))
    revisions_left = max_revisions
    round_no = 1
    cand_views = [
        {"title": c.title, "summary": c.summary, "source": c.source, "url": c.url, "published": c.published, "language": c.language}
        for c in candidates
        if c.id in story.candidate_ids
    ]
    story_view = {"id": story.id, "headline": story.headline, "summary": story.summary, "topic": story.topic}

    try:
        while True:
            red = llm.structured(
                "red_team",
                "Find every reason this article should not run as written.",
                {"run_date": article.run_date, "round": round_no, "article": article_view(article), "story": story_view, "candidates": cand_views},
                RED_TEAM_SCHEMA,
                web_search_uses=settings.web_search_uses("red_team"),
            )
            defense = llm.structured(
                "defense",
                "Respond to each red team finding.",
                {"run_date": article.run_date, "article": article_view(article), "findings": red.get("findings", [])},
                DEFENSE_SCHEMA,
                web_search_uses=settings.web_search_uses("defense"),
            )
            j1 = _judge(llm, settings, 1, article, red, defense, None, round_no, revisions_left)
            vround = ValidationRound(round=round_no, red_team=red, defense=defense, judge_1=j1)
            record.validation_rounds.append(vround)

            if j1["decision"] == "revise":
                article = reviser(article, list(j1.get("required_edits", [])), red.get("findings", []), defense)
                revisions_left -= 1
                round_no += 1
                continue
            if j1["decision"] == "reject":
                record.final_decision = "rejected"
                record.final_reason = f"Judge 1: {j1.get('reason', '')}".strip()
                break

            j2 = _judge(llm, settings, 2, article, red, defense, j1, round_no, revisions_left)
            vround.judge_2 = j2
            if j2["decision"] == "approve":
                record.final_decision = "approved"
                record.final_reason = f"Judge 2: {j2.get('reason', '')}".strip()
                break
            if j2["decision"] == "revise":
                article = reviser(article, list(j2.get("required_edits", [])), red.get("findings", []), defense)
                revisions_left -= 1
                round_no += 1
                continue
            record.final_decision = "rejected"
            record.final_reason = f"Judge 2: {j2.get('reason', '')}".strip()
            break
    except LLMRefusal as exc:
        record.final_decision = "rejected"
        record.final_reason = f"Model declined during validation: {exc}"
    except LLMError as exc:
        record.final_decision = "error"
        record.final_reason = f"Validation failed: {exc}"

    article.review = record
    return article, record
