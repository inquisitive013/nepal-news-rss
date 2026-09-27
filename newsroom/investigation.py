"""Stage 3b: the investigator digs for what the coverage misses before the writer starts."""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings
from .llm import BaseLLM
from .models import Candidate, RankingVerdict, Story, StoryDebate
from .writing import _debate_view, _judge_reasons

log = logging.getLogger(__name__)

ANGLE_KINDS = ["record", "numbers", "who_benefits", "missing", "pattern"]

INVESTIGATION_SCHEMA = {
    "type": "object",
    "properties": {
        "angles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "kind": {"type": "string", "enum": ANGLE_KINDS},
                    "why_it_matters": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}, "source": {"type": "string"}, "fact": {"type": "string"}},
                        },
                    },
                    "confidence": {"type": "integer"},
                },
            },
        },
        "unanswered": {
            "type": "array",
            "items": {"type": "object", "properties": {"question": {"type": "string"}, "who_could_answer": {"type": "string"}}},
        },
        "summary": {"type": "string"},
    },
}


def empty(summary: str = "") -> dict[str, Any]:
    return {"angles": [], "unanswered": [], "summary": summary}


def usable_angles(investigation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Angles that carry at least one piece of evidence with a URL. The rest are noise."""
    if not investigation:
        return []
    out = []
    for angle in investigation.get("angles") or []:
        evidence = [e for e in (angle.get("evidence") or []) if (e.get("url") or "").strip()]
        if evidence and (angle.get("claim") or "").strip():
            out.append({**angle, "evidence": evidence})
    return out


def investigate(
    llm: BaseLLM,
    settings: Settings,
    story: Story,
    candidates: list[Candidate],
    debate: StoryDebate | None,
    verdicts: list[RankingVerdict],
    run_date: str,
) -> dict[str, Any]:
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
    }
    data = llm.structured(
        "investigator",
        "Find what the coverage of this story misses. Every angle needs evidence.",
        payload,
        INVESTIGATION_SCHEMA,
        web_search_uses=settings.web_search_uses("investigator"),
    )
    kept = usable_angles(data)
    dropped = len(data.get("angles") or []) - len(kept)
    if dropped:
        log.info("investigator: dropped %d angle(s) without evidence for %s", dropped, story.id)
    return {"angles": kept, "unanswered": list(data.get("unanswered") or []), "summary": (data.get("summary") or "").strip()}
