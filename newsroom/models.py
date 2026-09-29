"""Plain dataclasses shared by every stage. Everything here serialises to JSON."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def short_hash(*parts: str, length: int = 10) -> str:
    h = hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()
    return h[:length]


def to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to plain dicts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def dump_json(obj: Any, path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(to_dict(obj), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


@dataclass
class Candidate:
    """One headline found during discovery."""

    id: str
    title: str
    summary: str
    url: str
    source: str
    source_domain: str
    published: str | None  # ISO 8601 or None when the feed had no date
    language: str  # "ne" | "en" | "unknown"
    via: str  # "rss" | "google_news"

    @staticmethod
    def make_id(url: str, title: str) -> str:
        return "c_" + short_hash(url or "", title or "")


@dataclass
class FeedHealth:
    source: str
    url: str
    kind: str  # "rss" | "google_news" | "google_news_fallback"
    ok: bool
    entries: int = 0
    in_window: int = 0
    error: str = ""


@dataclass
class Story:
    id: str
    headline: str
    summary: str
    topic: str
    candidate_ids: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)


@dataclass
class DebateCase:
    role: str  # "advocate" | "skeptic"
    score: int
    argument: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    virality_factors: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass
class StoryDebate:
    story_id: str
    advocate: DebateCase | None = None
    skeptic: DebateCase | None = None
    error: str = ""


@dataclass
class RankingVerdict:
    judge: str  # "ranking_judge_1" | "ranking_judge_2"
    ranked: list[dict[str, Any]] = field(default_factory=list)  # {story_id, rank, score, reason}
    rejected: list[dict[str, Any]] = field(default_factory=list)  # {story_id, reason}
    notes: str = ""


@dataclass
class ImageCredit:
    kind: str  # "found" | "generated" | "cover_card"
    title: str = ""
    author: str = ""
    author_url: str = ""
    source: str = ""  # provider name, e.g. "Wikimedia Commons"
    source_url: str = ""  # page where the image lives
    license: str = ""
    license_url: str = ""
    model: str = ""  # for generated images
    note: str = ""

    def line(self) -> str:
        """Human readable credit line."""
        if self.kind == "generated":
            return f"Illustration: AI generated with {self.model or 'an image model'}. {self.note}".strip()
        if self.kind == "cover_card":
            return self.note or "Graphic: newsroom"
        bits = []
        if self.title:
            bits.append(f"“{self.title}”")
        if self.author:
            bits.append(f"by {self.author}")
        if self.source:
            bits.append(f"via {self.source}")
        if self.license:
            bits.append(f"({self.license})")
        # Found pictures come from archives such as Wikimedia Commons, so they never
        # show the day's events. Say so in the credit itself.
        return "File photo: " + " ".join(bits) if bits else "File photo: source unknown"


@dataclass
class ImageAsset:
    path: str  # repo relative, e.g. data/images/2026-09-26-slug.jpg
    alt: str
    width: int
    height: int
    credit: ImageCredit
    original_url: str = ""


@dataclass
class ValidationFinding:
    id: str
    dimension: str  # accuracy | relevance | defensibility | virality
    severity: str  # high | medium | low
    passage: str
    problem: str
    evidence_url: str = ""
    suggested_fix: str = ""


@dataclass
class ValidationRound:
    round: int
    red_team: dict[str, Any] = field(default_factory=dict)
    defense: dict[str, Any] = field(default_factory=dict)
    judge_1: dict[str, Any] = field(default_factory=dict)
    judge_2: dict[str, Any] = field(default_factory=dict)
    # Edits applied inside this round, in order: {after: judge_1 | judge_2, required_edits, version}
    revisions: list[dict[str, Any]] = field(default_factory=list)
    # Judge 2's ruling on the version it sent back for, when it did.
    judge_2_recheck: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewRecord:
    ranking: list[dict[str, Any]] = field(default_factory=list)  # judge verdict summaries
    validation_rounds: list[ValidationRound] = field(default_factory=list)
    final_decision: str = ""  # approved | rejected | error
    final_reason: str = ""
    # The picture desk's record: the subjects it looked for, what each library gave, how many
    # pictures the model saw, and why the story carries a photo, an illustration or the cover card.
    picture: dict[str, Any] = field(default_factory=dict)


@dataclass
class Article:
    id: str
    slug: str
    story_id: str
    headline: str
    dek: str
    body_markdown: str
    language: str
    key_facts: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)  # {name, url, used_for}
    tags: list[str] = field(default_factory=list)
    social_hook: str = ""
    # One paragraph, the desk's own critical read of the story. Opens the article page and Instagram.
    take: str = ""
    # The card and the Facebook caption, to the content engine in docs/content-engine.md.
    image_headline: str = ""  # two lines, survives alone as a screenshot, carries its own status signal
    theme: str = ""  # one of graphic.THEMES, the chip and header label
    country: str = ""  # the country the story is about, NEPAL unless it is not
    caption: dict[str, str] = field(default_factory=dict)  # {"hook", "body", "trigger"}
    # The Nepali edition of the story: headline, dek, take, body_markdown, image_headline, social_hook,
    # caption, plus the editor's verdict. Empty when the Nepali edition stage is off or failed.
    nepali: dict[str, Any] = field(default_factory=dict)
    image_brief: dict[str, Any] = field(default_factory=dict)
    image: ImageAsset | None = None
    # What the investigator found that the coverage missed: angles with evidence, open questions.
    investigation: dict[str, Any] = field(default_factory=dict)
    review: ReviewRecord = field(default_factory=ReviewRecord)
    # Dated notes shown under the headline when a story moves on or is corrected after it ran:
    # {"date": ISO timestamp, "kind": "update" | "correction", "text", "text_ne", "link": "articles/<slug>/"}.
    updates: list[dict[str, str]] = field(default_factory=list)
    run_date: str = ""  # YYYY-MM-DD in newsroom timezone
    published_at: str = ""  # ISO timestamp
    version: int = 1


@dataclass
class UsageRecord:
    role: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    web_search_requests: int = 0
    seconds: float = 0.0
    batch: bool = False  # answered through the Message Batches API, where every token costs half


@dataclass
class RunLog:
    run_date: str
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str = ""
    status: str = "running"  # running | ok | partial | failed
    mode: str = "live"  # live | mock
    feed_health: list[FeedHealth] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    stories: list[Story] = field(default_factory=list)
    debates: list[StoryDebate] = field(default_factory=list)
    ranking: list[RankingVerdict] = field(default_factory=list)
    selected_story_ids: list[str] = field(default_factory=list)
    published: list[str] = field(default_factory=list)  # article ids
    rejected: list[dict[str, str]] = field(default_factory=list)  # {article_id|story_id, reason}
    usage: list[UsageRecord] = field(default_factory=list)
    # Every call sent to the batch queue: role, model, searches allowed, seconds into the run it
    # joined, seconds it waited and what came of it. Empty when batch mode is off.
    batch_waits: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def usage_totals(self) -> dict[str, int]:
        totals = {
            "calls": len(self.usage),
            "batch_calls": sum(1 for u in self.usage if u.batch),
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "web_search_requests": 0,
        }
        for u in self.usage:
            totals["input_tokens"] += u.input_tokens
            totals["output_tokens"] += u.output_tokens
            totals["cache_read_input_tokens"] += u.cache_read_input_tokens
            totals["cache_creation_input_tokens"] += u.cache_creation_input_tokens
            totals["web_search_requests"] += u.web_search_requests
        return totals
