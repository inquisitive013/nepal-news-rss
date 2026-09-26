"""The daily run: discover, debate, rank, write, illustrate, validate, publish."""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import discovery, images, publish, ranking, validation, writing
from .config import Settings
from .llm import BaseLLM, BudgetExceeded, LLMError, LLMRefusal, UsageMeter, make_llm
from .models import (
    Article,
    Candidate,
    RankingVerdict,
    RunLog,
    Story,
    StoryDebate,
    utcnow_iso,
)

log = logging.getLogger(__name__)


def newsroom_now(settings: Settings, now: datetime | None = None) -> tuple[datetime, str, str]:
    """Return (utc_now, run_date, human_date_label) in the newsroom timezone."""
    now = now or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    return now, local.strftime("%Y-%m-%d"), local.strftime("%d %B %Y")


def recently_published(settings: Settings, days: int = 3) -> list[dict[str, str]]:
    out = []
    for art in publish.load_articles(settings)[:60]:
        out.append({"headline": art.headline, "date": art.run_date, "story_id": art.story_id})
    return out[: days * 10]


def _process_story(
    llm: BaseLLM,
    settings: Settings,
    story: Story,
    candidates: list[Candidate],
    debate: StoryDebate | None,
    verdicts: list[RankingVerdict],
    run_date: str,
    date_label: str,
    now_iso: str,
) -> tuple[str, Article | None, str]:
    """Write, illustrate, validate and persist one story. Returns (outcome, article, reason)."""
    article = writing.write_article(llm, settings, story, candidates, debate, verdicts, run_date)
    article.review.ranking = writing._judge_reasons(story.id, verdicts)

    try:
        article.image = images.make_image_for_article(llm, settings, article, date_label)
    except Exception as exc:  # noqa: BLE001 - never lose an article over a picture
        log.warning("image stage failed for %s: %s", article.id, exc)
        article.image = None

    def reviser(art: Article, edits: list[str], findings: list[dict[str, Any]], defense: dict[str, Any]) -> Article:
        return writing.revise_article(llm, settings, art, story, edits, findings, defense)

    article, record = validation.validate_article(llm, settings, article, story, candidates, reviser)
    if record.final_decision == "approved":
        article.published_at = now_iso
        publish.save_article(settings, article)
        return "published", article, record.final_reason
    publish.save_rejected(settings, article)
    return record.final_decision or "rejected", article, record.final_reason


def run(settings: Settings, llm: BaseLLM | None = None, now: datetime | None = None) -> RunLog:
    now_utc, run_date, date_label = newsroom_now(settings, now)
    run_log = RunLog(run_date=run_date, mode="mock" if settings.mock else "live")
    meter = UsageMeter(int(settings.get("pipeline.max_llm_calls", 90)))
    llm = llm or make_llm(settings, meter)
    meter = llm.meter
    fixtures = settings.fixtures_dir

    try:
        # 1. Discovery
        candidates, health = discovery.discover(
            settings.sources,
            settings.google_news,
            window_hours=int(settings.get("pipeline.window_hours", 24)),
            max_per_source=int(settings.get("pipeline.max_per_source", 20)),
            max_total=int(settings.get("pipeline.max_candidates", 120)),
            now=now_utc,
            fixtures_dir=fixtures,
        )
        run_log.feed_health = health
        run_log.candidates = candidates
        ok_feeds = sum(1 for h in health if h.ok)
        log.info("discovery: %d candidates from %d/%d working feeds", len(candidates), ok_feeds, len(health))
        if not candidates:
            run_log.errors.append("no candidates found in the window")
            run_log.status = "ok"
            return run_log

        # 2. Stories
        stories, notes = ranking.cluster_stories(llm, settings, candidates, run_date)
        run_log.stories = stories
        log.info("clustered into %d stories. %s", len(stories), notes)
        if not stories:
            run_log.errors.append("clusterer returned no stories")
            run_log.status = "ok"
            return run_log

        # 3. Debate and rank
        debates, verdicts, selected = ranking.rank_stories(llm, settings, stories, candidates, run_date)
        run_log.debates = debates
        run_log.ranking = verdicts
        run_log.selected_story_ids = selected
        log.info("selected %d stories: %s", len(selected), selected)
        for v in verdicts:
            for r in v.rejected:
                run_log.rejected.append({"story_id": r.get("story_id", ""), "stage": v.judge, "reason": r.get("reason", "")})

        # 4. Write, illustrate, validate, publish
        story_by_id = {s.id: s for s in stories}
        debate_by_id = {d.story_id: d for d in debates}
        now_iso = utcnow_iso()
        workers = max(1, int(settings.get("pipeline.concurrency", 3)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _process_story, llm, settings, story_by_id[sid], candidates, debate_by_id.get(sid), verdicts, run_date, date_label, now_iso
                ): sid
                for sid in selected
                if sid in story_by_id
            }
            for fut in as_completed(futures):
                sid = futures[fut]
                try:
                    outcome, article, reason = fut.result()
                except BudgetExceeded as exc:
                    run_log.errors.append(f"{sid}: {exc}")
                    run_log.status = "partial"
                    continue
                except LLMRefusal as exc:
                    run_log.rejected.append({"story_id": sid, "stage": "writer", "reason": str(exc)})
                    continue
                except LLMError as exc:
                    run_log.errors.append(f"{sid}: {exc}")
                    continue
                if outcome == "published" and article:
                    run_log.published.append(article.id)
                    log.info("published %s", article.id)
                else:
                    run_log.rejected.append({"story_id": sid, "article_id": article.id if article else "", "stage": "validation", "reason": reason})
                    log.info("not published %s: %s", sid, reason)
        if run_log.status == "running":
            run_log.status = "ok"
    except BudgetExceeded as exc:
        run_log.errors.append(str(exc))
        run_log.status = "partial"
    except Exception as exc:  # noqa: BLE001
        run_log.errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        run_log.status = "failed"
        log.exception("run failed")
    finally:
        run_log.usage = list(meter.records)
        run_log.finished_at = utcnow_iso()
        publish.save_run(settings, run_log)
    return run_log
