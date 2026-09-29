"""What each Facebook post did: the calibration loop of content engine sections 13 and 18.2.

Every live Facebook post is read twice, 24 and 72 hours after it went live: unique viewers and
views from Meta's post insights, and shares, comments and reactions from the post itself. Each
run also notes the Page's follower count, so a post's first day can be set against the follower
change over the same hours. The report puts every post beside the reach score the ranking
judges forecast for its story, which is what the weekly review checks.

Meta serves post insights only to a Page token with read_insights, and the counts only with
pages_read_engagement. A reading records Graph error codes, never Meta's messages. A reading
that got nothing back is tried again on the next run; one that came back in part is read again
until it is whole or a day past its hour. When the token may not read at all, the run stops at
the first post instead of asking again for every one. Meta renamed its reach metrics in
November 2025 and June 2026, so the names live in config/settings.yaml.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import httpx

from . import social
from .config import Settings

log = logging.getLogger(__name__)

# Meta's names since June 2026: unique viewers replaced post_impressions_unique, views replaced post_impressions.
DEFAULT_METRICS = {"viewers": "post_total_media_view_unique", "views": "post_media_view"}
DEFAULT_HOURS = (24, 72)
# A reading is taken inside this many hours after its mark, or not at all.
GRACE_HOURS = 24


def _summary_total(key: str) -> Callable[[dict[str, Any]], int]:
    return lambda data: int(((data.get(key) or {}).get("summary") or {}).get("total_count", 0))


# The post's own counts: the field to ask for, and how to read the answer.
COUNTS: dict[str, tuple[str, Callable[[dict[str, Any]], int]]] = {
    "shares": ("shares", lambda data: int((data.get("shares") or {}).get("count", 0))),  # absent when nobody shared
    "comments": ("comments.summary(true).limit(0)", _summary_total("comments")),
    "reactions": ("reactions.summary(true).limit(0)", _summary_total("reactions")),
}
# Graph error codes that mean this token may not read the Page's numbers, so no other post will do better.
PERMISSION_CODES = {10, 190} | set(range(200, 300))
READING_KEYS = ("viewers", "views", "shares", "comments", "reactions")


def graph_error(resp: httpx.Response) -> str:
    """"code 10", or "HTTP 500" without one. Meta's message stays out of a public record."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    code = err.get("code") if isinstance(err, dict) else None
    return f"code {code}" if code is not None else f"HTTP {resp.status_code}"


def _code(error: str) -> int | None:
    m = re.fullmatch(r"code (\d+)", error)
    return int(m.group(1)) if m else None


def insight_settings(settings: Settings) -> tuple[dict[str, str], list[int]]:
    cfg = settings.get("social.facebook.insights") or {}
    metrics = {str(k): str(v) for k, v in (cfg.get("metrics") or DEFAULT_METRICS).items()}
    hours = sorted({int(h) for h in (cfg.get("readings_hours") or DEFAULT_HOURS)})
    return metrics, hours


def went_live(post: social.Post) -> datetime | None:
    """When readers could first see it: the scheduled time for a scheduled post, else the time it was sent."""
    for stamp in (post.scheduled_for, post.posted_at):
        if not stamp:
            continue
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return None


def due(post: social.Post, hour: int, now: datetime) -> bool:
    """A reading is due from its mark for GRACE_HOURS, unless one already came back whole."""
    live = went_live(post)
    if live is None or post.network != "facebook" or post.status != "posted" or not post.id:
        return False
    age = (now - live).total_seconds() / 3600
    if not hour <= age <= hour + GRACE_HOURS:
        return False
    slot = post.metrics.get(f"{hour}h")
    return slot is None or bool(slot.get("errors"))


def _metric_value(resp: httpx.Response) -> tuple[int, str]:
    """The number in an insights answer, or why there is none, by the answer's shape and never its words."""
    try:
        rows = (resp.json() or {}).get("data")
    except (ValueError, AttributeError):
        return 0, "not JSON"
    if not rows:
        return 0, "empty"
    points = rows[0].get("values") if isinstance(rows[0], dict) else None
    if not points:
        return 0, "no values"
    value = points[-1].get("value") if isinstance(points[-1], dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0, "a breakdown, not a number" if isinstance(value, dict) else f"{type(value).__name__}, not a number"
    return int(value), ""


def read_post(client: httpx.Client, environ: Mapping[str, str], post_id: str, metrics: Mapping[str, str]) -> tuple[dict[str, int], dict[str, str]]:
    """One reading of one post: the values that came back, and an error code for each that did not."""
    fb = social.facebook_page(client, environ)
    base = f"{social.META_GRAPH}/{social._graph_version(environ)}"
    values: dict[str, int] = {}
    errors: dict[str, str] = {}
    for label, metric in metrics.items():
        # Both metrics exist only for the lifetime period; without it Meta answers with empty data.
        resp = client.get(f"{base}/{post_id}/insights", params={"metric": metric, "period": "lifetime", "access_token": fb.token})
        if resp.status_code >= 400:
            errors[label] = graph_error(resp)
            continue
        value, why = _metric_value(resp)
        if why:
            errors[label] = f"no value ({why})"
        else:
            values[label] = value
    for label, (fields, pick) in COUNTS.items():
        resp = client.get(f"{base}/{post_id}", params={"fields": fields, "access_token": fb.token})
        if resp.status_code >= 400:
            errors[label] = graph_error(resp)
            continue
        try:
            values[label] = pick(resp.json())
        except (ValueError, TypeError, AttributeError):
            errors[label] = "no value"
    return values, errors


def followers_path(settings: Settings):
    return settings.data_dir / "insights" / "followers.json"


def load_followers(settings: Settings) -> list[dict[str, Any]]:
    path = followers_path(settings)
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("readings") or [])


def note_followers(settings: Settings, client: httpx.Client, environ: Mapping[str, str], now: datetime) -> tuple[int | None, str]:
    """The Page's follower count, kept once an hour at most. Returns the count, or None and an error code."""
    readings = load_followers(settings)
    if readings:
        last = datetime.fromisoformat(readings[-1]["at"])
        if now - last < timedelta(hours=1):
            return int(readings[-1]["followers"]), ""
    fb = social.facebook_page(client, environ)
    resp = client.get(f"{social.META_GRAPH}/{social._graph_version(environ)}/{fb.id}", params={"fields": "followers_count", "access_token": fb.token})
    if resp.status_code >= 400:
        return None, graph_error(resp)
    count = resp.json().get("followers_count")
    if not isinstance(count, int):
        return None, "no value"
    readings.append({"at": now.isoformat(timespec="seconds"), "followers": count})
    path = followers_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"readings": readings}, indent=1), encoding="utf-8")
    return count, ""


def take_readings(settings: Settings, environ: Mapping[str, str], *, client: httpx.Client | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read every post that is due and note the followers. Saves each record it changes.

    Returns {"read": [(article_id, hour, values, errors)], "failed": [(article_id, hour, errors)],
    "followers": count or None, "followers_error": code, "stopped": why the run stopped early}.
    """
    metrics, hours = insight_settings(settings)
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"read": [], "failed": [], "followers": None, "followers_error": "", "stopped": ""}
    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        out["followers"], out["followers_error"] = note_followers(settings, client, environ, now)
        for path in sorted((settings.data_dir / "social").glob("*.json")):
            rec = social.read_record(path)
            changed = False
            for post in rec.posts:
                for hour in hours:
                    if not due(post, hour, now):
                        continue
                    values, errors = read_post(client, environ, post.id, metrics)
                    if not values:
                        out["failed"].append((rec.article_id, hour, errors))
                        codes = {_code(e) for e in errors.values()}
                        if codes and codes <= PERMISSION_CODES:
                            codes_text = ", ".join(sorted(set(errors.values())))
                            out["stopped"] = f"the Page token may not read post numbers ({codes_text})"
                            break
                        continue
                    live = went_live(post)
                    age = round((now - live).total_seconds() / 3600, 1) if live else None
                    reading: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "hours": age, **values}
                    if errors:
                        reading["errors"] = errors
                    post.metrics[f"{hour}h"] = reading
                    changed = True
                    out["read"].append((rec.article_id, hour, values, errors))
                if out["stopped"]:
                    break
            if changed:
                social.save_record(settings, rec)
            if out["stopped"]:
                break
    finally:
        if own:
            client.close()
    return out


REACH_IN_REASON = re.compile(r"\breach (?:score )?(?:is )?(?:about |around |roughly )?(\d{1,3})\b", re.IGNORECASE)


def load_forecasts(settings: Settings) -> dict[str, dict[str, int]]:
    """Story id -> {"reach", "score"} from the final ranking verdict of the latest run that ranked it.

    `reach` is the judges' reach score. Runs from before the field existed carry it in the reason,
    "Reach is about 69 (hook 8, ...)", and it is read from there.
    """
    runs = []
    for path in (settings.data_dir / "runs").glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        runs.append((str(run.get("started_at") or ""), path.name, run))
    forecasts: dict[str, dict[str, int]] = {}
    for _, _, run in sorted(runs, key=lambda r: (r[0], r[1])):
        verdicts = run.get("ranking") or []
        if not verdicts:
            continue
        for item in verdicts[-1].get("ranked") or []:  # judge 2's list is final
            sid = item.get("story_id")
            if not sid:
                continue
            found: dict[str, int] = {}
            if isinstance(item.get("score"), (int, float)):
                found["score"] = int(item["score"])
            reach = item.get("reach")
            if not isinstance(reach, (int, float)):
                m = REACH_IN_REASON.search(str(item.get("reason") or ""))
                reach = int(m.group(1)) if m else None
            if reach is not None:
                found["reach"] = int(reach)
            forecasts[sid] = found
    return forecasts


def follower_change(readings: list[dict[str, Any]], live: datetime, hours: int = 24) -> tuple[int, float] | None:
    """The follower change from the last count at or before `live` to the first count `hours` later, and the hours it spans."""
    stamped = sorted((datetime.fromisoformat(r["at"]), int(r["followers"])) for r in readings)
    before = [r for r in stamped if r[0] <= live]
    after = [r for r in stamped if r[0] >= live + timedelta(hours=hours)]
    if not before or not after:
        return None
    (t0, f0), (t1, f1) = before[-1], after[0]
    return f1 - f0, round((t1 - t0).total_seconds() / 3600, 1)


def _cell(reading: dict[str, Any], key: str) -> str:
    value = reading.get(key)
    return f"{value:,}" if isinstance(value, int) else "–"


def report(settings: Settings, *, now: datetime | None = None, days: int = 7) -> list[str]:
    """A markdown table of every Facebook post that went live in the last `days`, newest first."""
    now = now or datetime.now(timezone.utc)
    forecasts = load_forecasts(settings)
    followers = load_followers(settings)
    rows = []
    for path in sorted((settings.data_dir / "social").glob("*.json")):
        rec = social.read_record(path)
        art_path = settings.data_dir / "articles" / f"{rec.article_id}.json"
        story_id = ""
        if art_path.exists():
            story_id = str(json.loads(art_path.read_text(encoding="utf-8")).get("story_id") or "")
        for post in rec.posts:
            if post.network != "facebook" or post.status != "posted":
                continue
            live = went_live(post)
            if live is None or now - live > timedelta(days=days):
                continue
            rows.append((live, rec.article_id, forecasts.get(story_id, {}), post.metrics, follower_change(followers, live)))
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
        tz = timezone.utc
    lines = [f"| Went live ({'Nepal time' if settings.timezone == 'Asia/Kathmandu' else settings.timezone}) | Story | Forecast reach | Viewers 24h | Views 24h | Shares 24h | Comments 24h | Reactions 24h | Viewers 72h | Followers, first day |", "|---|---|---|---|---|---|---|---|---|---|"]
    for live, article_id, forecast, metrics, change in sorted(rows, key=lambda r: r[0], reverse=True):
        r24, r72 = metrics.get("24h") or {}, metrics.get("72h") or {}
        moved = "–" if change is None else f"{change[0]:+,} over {change[1]}h"
        lines.append(
            f"| {live.astimezone(tz):%Y-%m-%d %H:%M} | {article_id} | {forecast.get('reach', '–')} | {_cell(r24, 'viewers')} | {_cell(r24, 'views')} "
            f"| {_cell(r24, 'shares')} | {_cell(r24, 'comments')} | {_cell(r24, 'reactions')} | {_cell(r72, 'viewers')} | {moved} |"
        )
    if not rows:
        lines.append(f"| – | no Facebook post went live in the last {days} days | | | | | | | | |")
    return lines
