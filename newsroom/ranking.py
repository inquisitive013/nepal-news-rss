"""Stage 2: turn candidates into stories, debate them, and let two judges rank them."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import Settings
from .llm import BaseLLM, BudgetExceeded, LLMError
from .models import Candidate, DebateCase, RankingVerdict, Story, StoryDebate

log = logging.getLogger(__name__)

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "topic": {"type": "string"},
                    "candidate_ids": {"type": "array", "items": {"type": "string"}},
                    "languages": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "notes": {"type": "string"},
    },
}

CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "argument": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {"type": "object", "properties": {"url": {"type": "string"}, "note": {"type": "string"}}},
        },
        "virality_factors": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "story_id": {"type": "string"},
                    "rank": {"type": "integer"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
            },
        },
        "rejected": {
            "type": "array",
            "items": {"type": "object", "properties": {"story_id": {"type": "string"}, "reason": {"type": "string"}}},
        },
        "notes": {"type": "string"},
    },
}


def _cand_view(c: Candidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "summary": c.summary,
        "source": c.source,
        "url": c.url,
        "published": c.published,
        "language": c.language,
    }


def cluster_stories(
    llm: BaseLLM, settings: Settings, candidates: list[Candidate], run_date: str, recent: list[dict[str, Any]] | None = None
) -> tuple[list[Story], str]:
    max_stories = int(settings.get("pipeline.max_debate_stories", 8))
    payload = {
        "run_date": run_date,
        "max_stories": max_stories,
        "recently_published": recent or [],
        "exclude_topics": settings.get("editorial.exclude_topics", []),
        "candidates": [_cand_view(c) for c in candidates],
    }
    data = llm.structured("story_clusterer", "Cluster today's candidate headlines into distinct stories.", payload, STORY_SCHEMA)
    known = {c.id for c in candidates}
    stories: list[Story] = []
    seen_ids: set[str] = set()
    for i, s in enumerate(data.get("stories", [])):
        ids = [cid for cid in s.get("candidate_ids", []) if cid in known]
        if not ids:
            continue
        sid = s.get("id") or f"s_{i}"
        if sid in seen_ids:
            sid = f"{sid}_{i}"
        seen_ids.add(sid)
        stories.append(
            Story(
                id=sid,
                headline=s.get("headline", "").strip() or candidates[0].title,
                summary=s.get("summary", "").strip(),
                topic=s.get("topic", "other").strip() or "other",
                candidate_ids=ids,
                languages=s.get("languages", []),
            )
        )
    return stories[:max_stories], data.get("notes", "")


def _case(role: str, data: dict[str, Any]) -> DebateCase:
    return DebateCase(
        role=role,
        score=int(max(0, min(100, data.get("score", 0)))),
        argument=data.get("argument", ""),
        evidence=[{"url": e.get("url", ""), "note": e.get("note", "")} for e in data.get("evidence", [])],
        virality_factors=list(data.get("virality_factors", [])),
        risks=list(data.get("risks", [])),
    )


def debate_story(llm: BaseLLM, settings: Settings, story: Story, candidates: list[Candidate], run_date: str) -> StoryDebate:
    debate = StoryDebate(story_id=story.id)
    story_view = {"id": story.id, "headline": story.headline, "summary": story.summary, "topic": story.topic}
    cand_views = [_cand_view(c) for c in candidates if c.id in story.candidate_ids]
    guidance = settings.get("editorial.guidance", {})
    try:
        adv = llm.structured(
            "advocate",
            "Research this story and make the case for ranking it high today.",
            {"run_date": run_date, "story": story_view, "candidates": cand_views, "guidance": guidance},
            CASE_SCHEMA,
            web_search_uses=settings.web_search_uses("advocate"),
        )
        debate.advocate = _case("advocate", adv)
        sk = llm.structured(
            "skeptic",
            "Test the advocate's case and argue against ranking this story high.",
            {"run_date": run_date, "story": story_view, "candidates": cand_views, "advocate": adv, "guidance": guidance},
            CASE_SCHEMA,
            web_search_uses=settings.web_search_uses("skeptic"),
        )
        debate.skeptic = _case("skeptic", sk)
    except BudgetExceeded:
        raise
    except LLMError as exc:
        debate.error = str(exc)[:300]
        log.warning("debate for %s failed: %s", story.id, exc)
    return debate


def debate_all(llm: BaseLLM, settings: Settings, stories: list[Story], candidates: list[Candidate], run_date: str) -> list[StoryDebate]:
    workers = max(1, int(settings.get("pipeline.concurrency", 3)))
    results: dict[str, StoryDebate] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(debate_story, llm, settings, s, candidates, run_date): s for s in stories}
        for fut in as_completed(futures):
            story = futures[fut]
            try:
                results[story.id] = fut.result()
            except BudgetExceeded as exc:
                results[story.id] = StoryDebate(story_id=story.id, error=str(exc))
    return [results[s.id] for s in stories if s.id in results]


def _verdict(judge: str, data: dict[str, Any], valid_ids: set[str]) -> RankingVerdict:
    ranked = [dict(r) for r in data.get("ranked", []) if r.get("story_id") in valid_ids]
    ranked.sort(key=lambda r: (int(r.get("rank", 999)), -int(r.get("score", 0))))
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
        r["score"] = int(max(0, min(100, r.get("score", 0))))
    rejected = [dict(r) for r in data.get("rejected", []) if r.get("story_id") in valid_ids]
    ranked_ids = {r["story_id"] for r in ranked}
    rejected = [r for r in rejected if r["story_id"] not in ranked_ids]
    covered = ranked_ids | {r["story_id"] for r in rejected}
    for sid in valid_ids - covered:
        rejected.append({"story_id": sid, "reason": "not ranked by the judge"})
    return RankingVerdict(judge=judge, ranked=ranked, rejected=rejected, notes=data.get("notes", ""))


def judge_ranking(
    llm: BaseLLM,
    settings: Settings,
    stories: list[Story],
    debates: list[StoryDebate],
    run_date: str,
    position: int,
    previous: RankingVerdict | None = None,
    recent: list[dict[str, Any]] | None = None,
) -> RankingVerdict:
    debated = [d for d in debates if d.advocate and d.skeptic and not d.error]
    valid_ids = {d.story_id for d in debated}
    payload = {
        "run_date": run_date,
        "judge_position": position,
        "articles_per_day": int(settings.get("pipeline.articles_per_day", 3)),
        "guidance": settings.get("editorial.guidance", {}),
        "recently_published": recent or [],
        "stories": [
            {"id": s.id, "headline": s.headline, "summary": s.summary, "topic": s.topic, "languages": s.languages}
            for s in stories
            if s.id in valid_ids
        ],
        "debates": [
            {
                "story_id": d.story_id,
                "advocate": {"score": d.advocate.score, "argument": d.advocate.argument, "evidence": d.advocate.evidence, "virality_factors": d.advocate.virality_factors, "risks": d.advocate.risks},
                "skeptic": {"score": d.skeptic.score, "argument": d.skeptic.argument, "evidence": d.skeptic.evidence, "virality_factors": d.skeptic.virality_factors, "risks": d.skeptic.risks},
            }
            for d in debated
        ],
        "previous_verdict": ({"ranked": previous.ranked, "rejected": previous.rejected, "notes": previous.notes} if previous else None),
    }
    task = "Rank today's stories." if position == 1 else "Review judge 1's ranking and issue the final ranking."
    data = llm.structured("ranking_judge", task, payload, VERDICT_SCHEMA)
    return _verdict(f"ranking_judge_{position}", data, valid_ids)


def rank_stories(
    llm: BaseLLM, settings: Settings, stories: list[Story], candidates: list[Candidate], run_date: str, recent: list[dict[str, Any]] | None = None
):
    """Run debates and both judges. Returns (debates, [verdict1, verdict2], selected_story_ids)."""
    debates = debate_all(llm, settings, stories, candidates, run_date)
    usable = [d for d in debates if d.advocate and d.skeptic and not d.error]
    if not usable:
        return debates, [], []
    v1 = judge_ranking(llm, settings, stories, debates, run_date, 1, recent=recent)
    v2 = judge_ranking(llm, settings, stories, debates, run_date, 2, previous=v1, recent=recent)
    n = int(settings.get("pipeline.articles_per_day", 3))
    selected = [r["story_id"] for r in v2.ranked[:n]]
    return debates, [v1, v2], selected
