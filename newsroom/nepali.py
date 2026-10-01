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
from .outlets import fix_spellings

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
            "properties": {"hook": {"type": "string"}, "angle": {"type": "string"}, "trigger": {"type": "string"}},
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
            "items": {
                "type": "object",
                "properties": {
                    "passage": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                    # fact: fidelity to the record and the legal standard; language: craft and completeness.
                    "severity": {"type": "string", "enum": ["fact", "language"]},
                },
            },
        },
        "reason": {"type": "string"},
    },
}

FIELDS = ("headline", "dek", "take", "body_markdown", "image_headline", "social_hook")
# The Facebook caption under the card: what the story changes for the reader, the one fact the
# coverage missed, and a question about the reader's own life. The card carries the sources.
CAPTION_KEYS = ("hook", "angle", "trigger")


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
    """The verified record the writer works from: the approved English story and nothing else.

    The investigator's notes stay out. The English story carries the findings and open questions
    the judges passed; on 29 September the notes gave the Nepali edition two open questions, a
    paragraph and an outlet the English never published, and the editor, reading the same notes,
    passed them. The English caption stays out too: the Nepali caption sums up the story itself.
    """
    weekday, date_ne = date_words(settings, article.published_at or article.run_date)
    return {
        "id": article.id,
        "headline": article.headline,
        "dek": article.dek,
        "take": article.take,
        "body_markdown": article.body_markdown,
        "key_facts": article.key_facts,
        "sources": [{"name": s.get("name", ""), "url": s.get("url", ""), "used_for": s.get("used_for", "")} for s in article.sources],
        "tags": article.tags,
        "run_date": article.run_date,
        "weekday_ne": weekday,
        "date_ne": date_ne,
        "site_name_ne": str(settings.get("site.name_ne", "") or "").strip() or settings.site_name,
    }


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: fix_spellings(str(data.get(k, "") or "").strip()) for k in FIELDS}
    cap = data.get("caption") or {}
    out["caption"] = {k: fix_spellings(str(cap.get(k, "") or "").strip()) for k in CAPTION_KEYS}
    out["notes"] = str(data.get("notes", "") or "").strip()
    return out


def _piece(nepali: dict[str, Any]) -> dict[str, Any]:
    return {k: nepali[k] for k in (*FIELDS, "caption")}


def _slots(piece: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Every text a reader sees: the top level fields, then the caption."""
    return [(k, None) for k in FIELDS] + [("caption", k) for k in CAPTION_KEYS]


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
    """A piece to publish: written, and not held for a fact the editor's final reading still found wrong."""
    return bool(nepali and nepali.get("headline") and nepali.get("body_markdown") and not nepali.get("held"))


def facts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The problems that hold a piece: every one not marked language. An unmarked problem counts as a fact."""
    return [p for p in problems if str(p.get("severity") or "fact").strip().lower() != "language"]


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
    reason = ""
    problems: list[dict[str, Any]] = []
    unread = False  # a fix went in after the editor's last reading
    for _ in range(rounds):
        check = _read(llm, record, piece)
        passes += 1
        reason = str(check.get("reason", "") or "").strip()
        problems = check["problems"]
        unread = False
        if check.get("decision", "approve") == "approve" or not problems:
            break
        piece, placed = fix_piece(llm, record, piece, problems, in_place=in_place)
        fixed += len(problems)
        fixed_in_place += placed
        unread = True
    if unread:
        # The text that publishes is the text the editor last read. Until 30 September the last
        # round's fixes went out unread, and every piece from 27 September on ended unapproved.
        check = _read(llm, record, piece)
        passes += 1
        reason = str(check.get("reason", "") or "").strip()
        problems = check["problems"] if check.get("decision", "approve") != "approve" else []
    left = facts(problems)
    piece = _clean(piece)  # a fix written in place never passed through the cleaner
    piece["checked"] = True
    piece["approved"] = not left
    # A fact still wrong after the final reading holds the piece: no Nepali page, card, caption or Reel.
    piece["held"] = bool(left)
    piece["fact_problems"] = [str(p.get("problem") or "").strip()[:300] for p in left][:10]
    piece["language_notes"] = len(problems) - len(left)
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


def recheck(llm: BaseLLM, settings: Settings, article: Article) -> dict[str, Any]:
    """One editor reading of the stored piece as it stands, for a held piece fixed by hand from the record.

    The text is never changed here. The reading decides the gate the same way the edition's final
    reading does: a fact still wrong keeps the piece held.
    """
    piece = dict(article.nepali or {})
    check = _read(llm, _record(settings, article), piece)
    problems = check["problems"] if check.get("decision", "approve") != "approve" else []
    left = facts(problems)
    piece["checked"] = True
    piece["approved"] = not left
    piece["held"] = bool(left)
    piece["fact_problems"] = [str(p.get("problem") or "").strip()[:300] for p in left][:10]
    piece["language_notes"] = len(problems) - len(left)
    piece["passes"] = int(piece.get("passes") or 0) + 1
    piece["editor"] = str(check.get("reason", "") or "").strip()
    return piece


def read_again(llm: BaseLLM, settings: Settings, article: Article, piece: dict[str, Any]) -> list[dict[str, Any]]:
    """One more reading of a finished piece, so pieces from different trials meet the same editor."""
    return _read(llm, _record(settings, article), piece)["problems"]


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
