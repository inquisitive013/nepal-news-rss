"""Stage 5: red team against defence, judge 1 rules, the reviser fixes, judge 2 has the final word."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .config import Settings
from .llm import BaseLLM, BudgetExceeded, LLMError, LLMRefusal
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


class RevisionPool:
    """Caps reviser calls across the whole run. Shared by the articles validated in parallel."""

    def __init__(self, limit: int) -> None:
        self._remaining = max(0, int(limit))
        self._lock = threading.Lock()

    def left(self) -> int:
        with self._lock:
            return self._remaining

    def take(self) -> bool:
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True


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


def _judge(
    llm: BaseLLM,
    settings: Settings,
    position: int,
    article: Article,
    red: dict,
    defense: dict,
    previous: dict | None,
    round_no: int,
    revisions_left: int,
    *,
    revision_log: list[dict[str, Any]] | None = None,
    revised_since_red_team: bool = False,
) -> dict[str, Any]:
    payload = {
        "run_date": article.run_date,
        "judge_position": position,
        "round": round_no,
        "revisions_left": revisions_left,
        "revised_since_red_team": revised_since_red_team,
        "revision_log": revision_log or [],
        "guidance": settings.get("editorial.guidance", {}),
        "article": article_view(article),
        "red_team": red,
        "defense": defense,
        "previous_ruling": previous,
    }
    if position == 1:
        task = "Rule on the red team findings and decide whether this article publishes."
    elif revised_since_red_team:
        task = "The article was revised after the red team read it. Check the revision and give the final ruling."
    else:
        task = "Give the independent second ruling on this article."
    # The final check of a revision is the only judge call that may search: nobody else reads that version.
    searches = settings.web_search_uses("validation_judge") if revised_since_red_team else 0
    data = llm.structured("validation_judge", task, payload, RULING_SCHEMA, web_search_uses=searches)
    if data.get("decision") == "revise" and revisions_left <= 0:
        data["decision"] = "reject"
        data["reason"] = (data.get("reason", "") + " No revisions left.").strip()
    return data


def validate_article(
    llm: BaseLLM,
    settings: Settings,
    article: Article,
    story: Story,
    candidates: list[Candidate],
    reviser: Reviser,
    pool: RevisionPool | None = None,
) -> tuple[Article, ReviewRecord]:
    """Run the adversarial review. Returns the (possibly revised) article and its record.

    A round is: red team, defence, judge 1. Judge 1 may send the piece to the reviser.
    While red team rounds remain, the revision gets a fresh round. Otherwise judge 2 reads
    the revision as the final check, with the findings, the defence, judge 1's ruling and
    the list of ordered edits in front of it. Judge 2 may send the piece back once more if
    revisions remain, then rules on that version with no further revision possible.
    Only a piece both judges approve is published.
    """
    record = article.review or ReviewRecord()
    max_revisions = int(settings.get("pipeline.max_revisions", 2))
    red_team_rounds = max(1, int(settings.get("pipeline.red_team_rounds", 1)))
    pool = pool or RevisionPool(10**9)
    revisions_used = 0
    cand_views = [
        {"title": c.title, "summary": c.summary, "source": c.source, "url": c.url, "published": c.published, "language": c.language}
        for c in candidates
        if c.id in story.candidate_ids
    ]
    story_view = {"id": story.id, "headline": story.headline, "summary": story.summary, "topic": story.topic}

    def revisions_left() -> int:
        return max(0, min(max_revisions - revisions_used, pool.left()))

    def revise(art: Article, ruling: dict[str, Any], red: dict, defense: dict, vround: ValidationRound, after: str) -> Article | None:
        nonlocal revisions_used
        if not pool.take():
            return None
        revisions_used += 1
        edits = list(ruling.get("required_edits", []))
        new = reviser(art, edits, red.get("findings", []), defense)
        vround.revisions.append({"after": after, "required_edits": edits, "version": new.version})
        return new

    def approve(judge: str, ruling: dict[str, Any]) -> None:
        record.final_decision = "approved"
        record.final_reason = f"{judge}: {ruling.get('reason', '')}".strip()

    def reject(judge: str, ruling: dict[str, Any], suffix: str = "") -> None:
        record.final_decision = "rejected"
        record.final_reason = f"{judge}: {ruling.get('reason', '')} {suffix}".strip()

    try:
        round_no = 0
        vround: ValidationRound | None = None
        red: dict[str, Any] = {}
        defense: dict[str, Any] = {}
        j1: dict[str, Any] = {}
        needs_red_team = True
        while True:
            if needs_red_team:
                round_no += 1
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
                j1 = _judge(llm, settings, 1, article, red, defense, None, round_no, revisions_left())
                vround = ValidationRound(round=round_no, red_team=red, defense=defense, judge_1=j1)
                record.validation_rounds.append(vround)
                needs_red_team = False
                if j1["decision"] == "reject":
                    reject("Judge 1", j1)
                    break
                if j1["decision"] == "revise":
                    new = revise(article, j1, red, defense, vround, "judge_1")
                    if new is None:
                        reject("Judge 1", j1, "No revision budget left today.")
                        break
                    article = new
                    if round_no < red_team_rounds:
                        needs_red_team = True
                        continue
                    # No red team rounds left: judge 2 reads the revision as the final check.

            assert vround is not None
            revised = bool(vround.revisions)
            j2 = _judge(llm, settings, 2, article, red, defense, j1, round_no, revisions_left(), revision_log=vround.revisions, revised_since_red_team=revised)
            vround.judge_2 = j2
            if j2["decision"] == "approve":
                approve("Judge 2", j2)
                break
            if j2["decision"] == "reject":
                reject("Judge 2", j2)
                break
            new = revise(article, j2, red, defense, vround, "judge_2")
            if new is None:
                reject("Judge 2", j2, "No revision budget left today.")
                break
            article = new
            if round_no < red_team_rounds:
                needs_red_team = True
                continue
            # One more look at the version judge 2 asked for. No further revision is possible.
            j2b = _judge(llm, settings, 2, article, red, defense, j2, round_no, 0, revision_log=vround.revisions, revised_since_red_team=True)
            vround.judge_2_recheck = j2b
            if j2b["decision"] == "approve":
                approve("Judge 2", j2b)
            else:
                reject("Judge 2", j2b)
            break
    except BudgetExceeded:
        raise
    except LLMRefusal as exc:
        record.final_decision = "rejected"
        record.final_reason = f"Model declined during validation: {exc}"
    except LLMError as exc:
        record.final_decision = "error"
        record.final_reason = f"Validation failed: {exc}"

    article.review = record
    return article, record
