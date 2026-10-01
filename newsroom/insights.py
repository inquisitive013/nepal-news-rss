"""What each Facebook post did: the calibration loop of content engine sections 13 and 18.2.

Every live Facebook post is read twice, 24 and 72 hours after it went live: unique viewers and
views from Meta's post insights, and shares, comments and reactions from the post itself. A Reel
is read at the same hours from its video's insights: plays, unique viewers and the average watch
time. Each
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
# A Reel's numbers live on its video, at /{video-id}/video_insights, under these names (Meta's Reels
# insights documentation, found by search on 1 October 2026). With no metric named, Meta answers
# with a default set; a name missing from it is asked for on its own. The probe lists every name
# Meta returns, so a renamed metric shows there.
DEFAULT_REEL_METRICS = {"plays": "blue_reels_play_count", "viewers": "post_impressions_unique", "avg_watch_ms": "post_video_avg_time_watched"}
REEL_NETWORK = "facebook_reel"
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


def reel_metric_names(settings: Settings) -> dict[str, str]:
    cfg = settings.get("social.facebook.insights") or {}
    return {str(k): str(v) for k, v in (cfg.get("reel_metrics") or DEFAULT_REEL_METRICS).items()}


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
    if live is None or post.network not in ("facebook", REEL_NETWORK) or post.status != "posted" or not post.id:
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
    story_id = post_id if "_" in post_id else story_of(client, base, post_id, fb.token)
    for label, (fields, pick) in COUNTS.items():
        resp = client.get(f"{base}/{story_id}", params={"fields": fields, "access_token": fb.token})
        if resp.status_code >= 400:
            errors[label] = graph_error(resp)
            continue
        try:
            values[label] = pick(resp.json())
        except (ValueError, TypeError, AttributeError):
            errors[label] = "no value"
    return values, errors


def story_of(client: httpx.Client, base: str, photo_id: str, token: str) -> str:
    """The Page post a scheduled photo became, or the photo itself when Meta does not say.

    A scheduled photo is recorded by the photo's id. The photo answers its insights, but its
    shares and reactions come back as error code 100: they belong to the post.
    """
    resp = client.get(f"{base}/{photo_id}", params={"fields": "page_story_id", "access_token": token})
    if resp.status_code >= 400:
        return photo_id
    try:
        story = (resp.json() or {}).get("page_story_id")
    except ValueError:
        return photo_id
    return str(story) if isinstance(story, str) and re.fullmatch(r"\d+_\d+", story) else photo_id


def _rows(resp: httpx.Response) -> dict[str, Any]:
    """Metric name -> its latest value, from an insights answer."""
    try:
        rows = (resp.json() or {}).get("data") or []
    except (ValueError, AttributeError):
        return {}
    found: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        points = row.get("values") or []
        found[str(row["name"])] = points[-1].get("value") if points and isinstance(points[-1], dict) else None
    return found


def _shape(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "a breakdown, not a number" if isinstance(value, dict) else "no value"
    return ""


def read_reel(client: httpx.Client, environ: Mapping[str, str], video_id: str, metrics: Mapping[str, str]) -> tuple[dict[str, int], dict[str, str]]:
    """One reading of one Reel from its video's insights: the values that came back, and why each other did not."""
    fb = social.facebook_page(client, environ)
    base = f"{social.META_GRAPH}/{social._graph_version(environ)}"
    resp = client.get(f"{base}/{video_id}/video_insights", params={"access_token": fb.token})
    if resp.status_code >= 400:
        error = graph_error(resp)
        return {}, {label: error for label in metrics}
    found = _rows(resp)
    values: dict[str, int] = {}
    errors: dict[str, str] = {}
    for label, name in metrics.items():
        if name not in found:
            # Not in the default set: asked for on its own.
            one = client.get(f"{base}/{video_id}/video_insights", params={"metric": name, "access_token": fb.token})
            if one.status_code >= 400:
                errors[label] = graph_error(one)
                continue
            if name not in (rows := _rows(one)):
                errors[label] = "not returned"
                continue
            found[name] = rows[name]
        why = _shape(found[name])
        if why:
            errors[label] = f"no value ({why})"
        else:
            values[label] = int(found[name])
    return values, errors


# What the probe asks of the newest live post: the two view metrics, an engagement metric that
# shows whether insights answer at all, and the reach metric Meta retired in June 2026.
PROBE_METRICS = ("post_total_media_view_unique", "post_media_view", "post_reactions_by_type_total", "post_impressions_unique")


def _answer(resp: httpx.Response) -> str:
    if resp.status_code >= 400:
        return graph_error(resp)
    _, why = _metric_value(resp)
    return why or "a number"


def probe(settings: Settings, environ: Mapping[str, str], *, client: httpx.Client | None = None) -> list[str]:
    """What the token may read, told by permission names, answer shapes and error codes only.

    It names the token's type, whether it expires and its scopes, never the ids debug_token also
    returns. Then it asks each of PROBE_METRICS of the newest live Facebook post, on the post and
    on its photo, and asks the Page for one day of views. Values stay out: they are the Page's own.
    """
    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    lines: list[str] = []
    try:
        fb = social.facebook_page(client, environ)
        base = f"{social.META_GRAPH}/{social._graph_version(environ)}"
        resp = client.get(f"{base}/debug_token", params={"input_token": fb.token, "access_token": fb.token})
        if resp.status_code >= 400:
            lines.append(f"- Token: its permissions could not be read ({graph_error(resp)})")
        else:
            data = resp.json().get("data") or {}
            expires = "never expires" if data.get("expires_at") == 0 else "expires"
            scopes = ", ".join(sorted(str(s) for s in data.get("scopes") or [])) or "none listed"
            lines.append(f"- Token: {str(data.get('type') or '?').lower()} token, {'valid' if data.get('is_valid') else 'not valid'}, {expires}. Permissions: {scopes}")
        posts = []
        for path in sorted((settings.data_dir / "social").glob("*.json")):
            for post in social.read_record(path).posts:
                live = went_live(post)
                if post.network == "facebook" and post.status == "posted" and post.id and live:
                    posts.append((live, post.id))
        if not posts:
            lines.append("- No live Facebook post to ask about.")
        else:
            post_id = max(posts)[1]
            objects = [("post", post_id)] + ([("photo", post_id.split("_", 1)[1])] if "_" in post_id else [])
            for label, object_id in objects:
                for metric in PROBE_METRICS:
                    resp = client.get(f"{base}/{object_id}/insights", params={"metric": metric, "period": "lifetime", "access_token": fb.token})
                    lines.append(f"- Newest post, asked on the {label}, {metric}: {_answer(resp)}")
        photos = [(live, post_id) for live, post_id in posts if "_" not in post_id]
        if photos:
            resp = client.get(f"{base}/{max(photos)[1]}", params={"fields": "page_story_id", "access_token": fb.token})
            story = "" if resp.status_code >= 400 else str((resp.json() or {}).get("page_story_id") or "")
            answer = graph_error(resp) if resp.status_code >= 400 else ("a post id" if re.fullmatch(r"\d+_\d+", story) else "no post id")
            lines.append(f"- Newest scheduled photo, page_story_id: {answer}")
        reels = []
        for path in sorted((settings.data_dir / "social").glob("*.json")):
            for post in social.read_record(path).posts:
                live = went_live(post)
                if post.network == REEL_NETWORK and post.status == "posted" and post.id and live:
                    reels.append((live, post.id))
        if not reels:
            lines.append("- No live Reel to ask about.")
        else:
            resp = client.get(f"{base}/{max(reels)[1]}/video_insights", params={"access_token": fb.token})
            if resp.status_code >= 400:
                lines.append(f"- Newest Reel, video_insights: {graph_error(resp)}")
            else:
                found = _rows(resp)
                listed = ", ".join(f"{name} ({_shape(value) or 'a number'})" for name, value in found.items()) or "empty"
                lines.append(f"- Newest Reel, video_insights by default: {listed}")
                for label, name in reel_metric_names(settings).items():
                    if name not in found:
                        one = client.get(f"{base}/{max(reels)[1]}/video_insights", params={"metric": name, "access_token": fb.token})
                        got = _rows(one) if one.status_code < 400 else {}
                        answer = graph_error(one) if one.status_code >= 400 else (_shape(got[name]) or "a number") if name in got else "not returned"
                        lines.append(f"- Newest Reel, {name} asked on its own: {answer}")
        resp = client.get(f"{base}/{fb.id}/insights", params={"metric": "page_media_view", "period": "day", "access_token": fb.token})
        lines.append(f"- The Page, page_media_view by day: {_answer(resp)}")
    finally:
        if own:
            client.close()
    return lines


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
    reel_metrics = reel_metric_names(settings)
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
                    if post.network == REEL_NETWORK:
                        values, errors = read_reel(client, environ, post.id, reel_metrics)
                    else:
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
    reels = []
    for path in sorted((settings.data_dir / "social").glob("*.json")):
        rec = social.read_record(path)
        art_path = settings.data_dir / "articles" / f"{rec.article_id}.json"
        story_id = ""
        if art_path.exists():
            story_id = str(json.loads(art_path.read_text(encoding="utf-8")).get("story_id") or "")
        for post in rec.posts:
            if post.network not in ("facebook", REEL_NETWORK) or post.status != "posted":
                continue
            live = went_live(post)
            if live is None or now - live > timedelta(days=days):
                continue
            if post.network == REEL_NETWORK:
                reels.append((live, rec.article_id, post.metrics))
            else:
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
    lines += ["", f"| Reel went live ({'Nepal time' if settings.timezone == 'Asia/Kathmandu' else settings.timezone}) | Story | Plays 24h | Viewers 24h | Average watch 24h | Plays 72h | Viewers 72h |", "|---|---|---|---|---|---|---|"]
    for live, article_id, metrics in sorted(reels, key=lambda r: r[0], reverse=True):
        r24, r72 = metrics.get("24h") or {}, metrics.get("72h") or {}
        watch = r24.get("avg_watch_ms")
        watched = f"{watch / 1000:.1f} s" if isinstance(watch, int) else "–"
        lines.append(f"| {live.astimezone(tz):%Y-%m-%d %H:%M} | {article_id} | {_cell(r24, 'plays')} | {_cell(r24, 'viewers')} | {watched} | {_cell(r72, 'plays')} | {_cell(r72, 'viewers')} |")
    if not reels:
        lines.append(f"| – | no Reel went live in the last {days} days | | | | | |")
    return lines
