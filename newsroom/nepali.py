"""Stage 6b: the Nepali edition.

Not a translation. A Nepali writer takes the verified record of the approved English story,
its facts, numbers, names, quotes, attributions and sources, and writes the story the way
Nepali news is written. A Nepali editor checks the piece against the record and against the
Nepali style guide, and the writer applies every fix.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .llm import BaseLLM
from .models import Article

log = logging.getLogger(__name__)

NE_DIGITS = str.maketrans("0123456789", "०१२३४५६७८९")
NE_MONTHS = ["जनवरी", "फेब्रुअरी", "मार्च", "अप्रिल", "मे", "जुन", "जुलाई", "अगस्ट", "सेप्टेम्बर", "अक्टोबर", "नोभेम्बर", "डिसेम्बर"]
NE_WEEKDAYS = ["सोमबार", "मंगलबार", "बुधबार", "बिहीबार", "शुक्रबार", "शनिबार", "आइतबार"]

WRITER_SCHEMA = {
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


def date_words(settings: Settings, value: str) -> tuple[str, str]:
    """('आइतबार', '२७ सेप्टेम्बर २०२६') for an ISO timestamp or a YYYY-MM-DD date, in newsroom time."""
    if not value:
        return "", ""
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    if "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(tz)
    else:
        dt = datetime.fromisoformat(value)
    return NE_WEEKDAYS[dt.weekday()], f"{dt.day} {NE_MONTHS[dt.month - 1]} {dt.year}".translate(NE_DIGITS)


def _record(settings: Settings, article: Article) -> dict[str, Any]:
    """The verified record the writer works from: everything the approved English story carries."""
    inv = article.investigation or {}
    weekday, date_ne = date_words(settings, article.published_at or article.run_date)
    return {
        "id": article.id,
        "headline": article.headline,
        "dek": article.dek,
        "take": article.take,
        "body_markdown": article.body_markdown,
        "key_facts": article.key_facts,
        "sources": [{"name": s.get("name", ""), "url": s.get("url", ""), "used_for": s.get("used_for", "")} for s in article.sources],
        "investigation": {
            "angles": [
                {"kind": a.get("kind", ""), "claim": a.get("claim", ""), "evidence": [{"source": e.get("source", ""), "url": e.get("url", ""), "fact": e.get("fact", "")} for e in (a.get("evidence") or [])[:3]]}
                for a in (inv.get("angles") or [])
            ],
            "unanswered": inv.get("unanswered") or [],
        },
        "caption": article.caption or {},
        "tags": article.tags,
        "run_date": article.run_date,
        "weekday_ne": weekday,
        "date_ne": date_ne,
        "site_name_ne": str(settings.get("site.name_ne", "") or "").strip() or settings.site_name,
    }


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: str(data.get(k, "") or "").strip() for k in FIELDS}
    cap = data.get("caption") or {}
    out["caption"] = {k: str(cap.get(k, "") or "").strip() for k in ("hook", "body", "trigger")}
    out["notes"] = str(data.get("notes", "") or "").strip()
    return out


def _piece(nepali: dict[str, Any]) -> dict[str, Any]:
    return {k: nepali[k] for k in (*FIELDS, "caption")}


def _slots(piece: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Every text a reader sees: the top level fields, then the caption's three parts."""
    return [(k, None) for k in FIELDS] + [("caption", k) for k in ("hook", "body", "trigger")]


def apply_fixes(piece: dict[str, Any], problems: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Write each of the editor's fixes in where its passage stands, and nothing else.

    The editor quotes each passage exactly as it stands and writes the fix out in Nepali. A
    passage found once in the whole piece, allowing for spacing, is replaced by its fix. One
    found nowhere, or in more than one place, is returned for the writer to place.
    """
    out = {**piece, "caption": dict(piece.get("caption") or {})}
    left: list[dict[str, Any]] = []
    for problem in problems:
        passage = str(problem.get("passage", "") or "").strip()
        fix = str(problem.get("fix", "") or "").strip()
        if not passage or not fix:
            left.append(problem)
            continue
        pattern = re.compile(r"\s+".join(re.escape(word) for word in passage.split()))
        hits = [(field, key, m) for field, key in _slots(out) for m in pattern.finditer((out[field] if key is None else out[field].get(key, "")) or "")]
        if len(hits) != 1:
            left.append(problem)
            continue
        field, key, match = hits[0]
        text = out[field] if key is None else out[field][key]
        text = text[: match.start()] + fix + text[match.end():]
        if key is None:
            out[field] = text
        else:
            out[field][key] = text
    return out, left


def usable(nepali: dict[str, Any] | None) -> bool:
    return bool(nepali and nepali.get("headline") and nepali.get("body_markdown"))


def wanting(settings: Settings, *, only: list[str] | None = None, everything: bool = False, limit: int = 0) -> list[Article]:
    """The stored articles to write in Nepali: the ones without a usable version unless told otherwise."""
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


def backfill(settings: Settings, llm: BaseLLM, articles: list[Article], *, workers: int = 1) -> list[tuple[str, str, str]]:
    """Write and store each article's Nepali edition. Returns (article id, status, detail) per attempt, in input order.

    With one worker the stories go one after another and a spent call budget ends the loop.
    With more, that many stories are written at once; a story the budget cuts off is left
    as it was and not reported. A refusal or model error skips the article. Every story
    written is saved the moment it is done.
    """
    from . import publish
    from .llm import BudgetExceeded, LLMError, LLMRefusal

    def one(article: Article) -> tuple[str, str, str] | None:
        try:
            article.nepali = nepali_for(llm, settings, article)
        except BudgetExceeded as exc:
            log.warning("budget spent before %s was finished: %s", article.id, exc)
            return None
        except (LLMRefusal, LLMError) as exc:
            log.warning("nepali edition failed for %s: %s", article.id, exc)
            return (article.id, "failed", str(exc)[:200])
        publish.save_article(settings, article)
        return (article.id, "written", article.nepali.get("headline", ""))

    workers = max(1, min(int(workers or 1), len(articles) or 1))
    if workers == 1:
        out: list[tuple[str, str, str]] = []
        for article in articles:
            result = one(article)
            if result is None:
                break
            out.append(result)
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for r in pool.map(one, articles) if r is not None]


def nepali_for(llm: BaseLLM, settings: Settings, article: Article) -> dict[str, Any]:
    """Write the piece, then let the editor read it up to `pipeline.nepali_rounds` times, fixing after each pass that asks.

    Returns the Nepali fields plus `checked`, `approved`, `passes`, `problems_fixed`, `fixed_in_place`
    and the editor's last `reason`.
    """
    record = _record(settings, article)
    searches = settings.web_search_uses("nepali_writer")
    piece = _clean(llm.structured("nepali_writer", "Write this story in Nepali from the verified record.", {"article": record}, WRITER_SCHEMA, web_search_uses=searches))
    rounds = max(1, int(settings.get("pipeline.nepali_rounds", 2) or 1))
    in_place = settings.get("pipeline.nepali_fix", "rewrite") == "in_place"
    passes = fixed = fixed_in_place = 0
    approved = False
    reason = ""
    for _ in range(rounds):
        check = _read(llm, record, piece)
        passes += 1
        reason = str(check.get("reason", "") or "").strip()
        problems = check["problems"]
        approved = check.get("decision", "approve") == "approve" or not problems
        if approved:
            break
        piece, placed = fix_piece(llm, record, piece, problems, in_place=in_place)
        fixed += len(problems)
        fixed_in_place += placed
    piece["checked"] = True
    piece["approved"] = approved
    piece["passes"] = passes
    piece["problems_fixed"] = fixed
    piece["fixed_in_place"] = fixed_in_place
    piece["editor"] = reason
    return piece


def _read(llm: BaseLLM, record: dict[str, Any], piece: dict[str, Any]) -> dict[str, Any]:
    """One reading by the Nepali editor. `problems` keeps only the entries that name a problem."""
    check = llm.structured(
        "nepali_editor",
        "Check the Nepali piece against the verified English record and the Nepali style guide.",
        {"record": record, "nepali": _piece(piece)},
        CHECK_SCHEMA,
    )
    check["problems"] = [p for p in (check.get("problems") or []) if (p.get("problem") or "").strip()]
    return check


WAYS = ("rewrite", "in_place")


def trial(llm: BaseLLM, settings: Settings, article: Article, *, ways: tuple[str, ...] = WAYS) -> dict[str, Any]:
    """Write one story in Nepali and fix it each way in `ways` from the same draft and the same first reading.

    Nothing is saved. For each way it returns the finished piece, what the second reading found,
    what a closing reading finds in the finished piece, how many fixes went in word for word, and
    what that way cost in model calls after the shared draft and first reading.
    """
    record = _record(settings, article)
    searches = settings.web_search_uses("nepali_writer")
    meter = llm.meter
    draft = _clean(llm.structured("nepali_writer", "Write this story in Nepali from the verified record.", {"article": record}, WRITER_SCHEMA, web_search_uses=searches))
    first = _read(llm, record, draft)["problems"]
    out: dict[str, Any] = {"id": article.id, "first_reading": first}
    for way in ways:
        mark = len(meter.records)
        piece, placed, second = draft, 0, []
        if first:
            piece, placed = fix_piece(llm, record, draft, first, in_place=way == "in_place")
            second = _read(llm, record, piece)["problems"]
            if second:
                piece, more = fix_piece(llm, record, piece, second, in_place=way == "in_place")
                placed += more
        closing = _read(llm, record, piece)["problems"]
        out[way] = {"piece": piece, "second_reading": second, "closing_reading": closing, "placed": placed, "records": meter.records[mark:]}
    return out


def fix_piece(llm: BaseLLM, record: dict[str, Any], piece: dict[str, Any], problems: list[dict[str, Any]], *, in_place: bool) -> tuple[dict[str, Any], int]:
    """Apply the editor's fixes. Returns the piece and how many fixes went in word for word.

    In place, each fix replaces its passage and nothing else moves; the writer places only the
    fixes whose passage it could not find. Otherwise the writer applies them all, leaves every
    sentence no fix touches as it was, and returns the whole piece.
    """
    placed = 0
    if in_place:
        piece, left = apply_fixes(piece, problems)
        placed = len(problems) - len(left)
        problems = left
    if problems:
        # A fix works from the editor's notes and never searches. With the search tool attached
        # it still looped through the tool's code sandbox: on 28 September each fix pass made no
        # search, reread about 185,000 tokens and took over six minutes.
        piece = _clean(
            llm.structured(
                "nepali_writer",
                "Apply the editor's fixes where each passage sits, leave every sentence no fix touches word for word, keep each fact's source in its sentence, and return the piece complete.",
                {"article": record, "nepali": _piece(piece), "fixes": problems},
                WRITER_SCHEMA,
            )
        )
    return piece, placed
