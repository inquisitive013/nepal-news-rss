"""The comment desk: Nepal Wire answers its readers in the first two hours after a post.

Content engine section 17, the first hour doctrine: early replies multiply the thread, and a
Page that sat idle for months needs every comment it can earn. Every quarter hour in the hours
after each posting slot, the desk reads the new top level comments under Facebook posts that
went live within `window_minutes`, and one model call sorts them. A reader answering the post's
question, a question the story answers, and thanks get a short reply grounded in the record.
A correction, a legal complaint and a comment exposing a private person are left for a person
and listed in the run summary. Politics, anything about the monarchy, abuse, questions the story
cannot answer and everything else get nothing.

A reply is a public statement by Nepal Wire, so it is held to the story's standard, and code
checks every one again before it goes out: no links, no hashtags, no tagging, no repeats, a
length cap, and caps on replies per post and per run. Readers' words are sent to the model and
nowhere else: the record keeps each comment's id and category, never its text, and the summary
prints only Nepal Wire's own replies.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import httpx

from . import social
from .config import Settings
from .llm import BudgetExceeded, LLMError

log = logging.getLogger(__name__)

REPLY_CATEGORIES = ("answer", "question", "thanks")
FLAG_CATEGORIES = ("correction", "legal", "private")
CATEGORIES = (*REPLY_CATEGORIES, *FLAG_CATEGORIES, "politics", "abuse", "unanswered", "other")
MAX_REPLY_CHARS = 280
FORBIDDEN = re.compile(r"https?://|www\.|@|#", re.IGNORECASE)

DESK_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "reply": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
    },
}


def desk_settings(settings: Settings) -> dict[str, Any]:
    cfg = settings.get("social.facebook.comments") or {}
    return {
        "reply": bool(cfg.get("reply", True)),
        "window_minutes": int(cfg.get("window_minutes", 120)),
        "max_replies_per_post": int(cfg.get("max_replies_per_post", 10)),
        "max_replies_per_run": int(cfg.get("max_replies_per_run", 10)),
    }


def guard(reply: str, earlier: list[str]) -> str:
    """Why a drafted reply may not go out, or "" when it may."""
    text = " ".join((reply or "").split())
    if not text:
        return "empty"
    if len(text) > MAX_REPLY_CHARS:
        return "too long"
    if FORBIDDEN.search(text):
        return "a link, a tag or a hashtag"
    if text in {" ".join(e.split()) for e in earlier}:
        return "a repeat"
    return ""


def fetch_comments(client: httpx.Client, environ: Mapping[str, str], post_id: str, *, pages: int = 3) -> list[dict[str, Any]]:
    """The post's top level comments, oldest first: id, text, time, and whether the Page wrote it."""
    fb = social.facebook_page(client, environ)
    url = f"{social.META_GRAPH}/{social._graph_version(environ)}/{post_id}/comments"
    params: dict[str, str] = {"filter": "toplevel", "order": "chronological", "fields": "id,message,created_time,from", "limit": "100", "access_token": fb.token}
    out: list[dict[str, Any]] = []
    for _ in range(pages):
        data = social._raise_for(client.get(url, params=params), "Facebook")
        for c in data.get("data") or []:
            author = str((c.get("from") or {}).get("id") or "")
            out.append({"id": str(c.get("id") or ""), "text": str(c.get("message") or ""), "created_time": str(c.get("created_time") or ""), "from_page": author == fb.id})
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = str(nxt), {}
    return [c for c in out if c["id"]]


def post_reply(client: httpx.Client, environ: Mapping[str, str], comment_id: str, text: str) -> str:
    fb = social.facebook_page(client, environ)
    data = social._raise_for(client.post(f"{social.META_GRAPH}/{social._graph_version(environ)}/{comment_id}/comments", data={"message": text, "access_token": fb.token}), "Facebook")
    return str(data.get("id") or "")


def article_record(settings: Settings, article_id: str) -> dict[str, Any]:
    """What the desk may say: the verified story in English and, when checked, in Nepali."""
    path = settings.data_dir / "articles" / f"{article_id}.json"
    if not path.exists():
        return {}
    a = json.loads(path.read_text(encoding="utf-8"))
    ne = a.get("nepali") or {}
    record = {k: a.get(k) for k in ("headline", "dek", "key_facts", "body_markdown", "published_at")}
    if ne.get("headline") and ne.get("body_markdown"):
        record["nepali"] = {"headline": ne.get("headline"), "body_markdown": ne.get("body_markdown")}
    return record


def run_desk(
    settings: Settings,
    environ: Mapping[str, str],
    llm: Any,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read and answer new comments on every post inside its window. Saves each record it changes.

    Returns {"replied": [(article_id, category, reply)], "flagged": [(article_id, category, post_url)],
    "skipped": {category: count}, "held": [(article_id, why)], "posts": number of posts read}.
    """
    cfg = desk_settings(settings)
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"replied": [], "flagged": [], "skipped": {}, "held": [], "posts": 0}
    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    budget = cfg["max_replies_per_run"]
    stop = False
    try:
        for path in sorted((settings.data_dir / "social").glob("*.json")):
            if stop:
                break
            rec = social.read_record(path)
            changed = False
            for post in rec.posts:
                live = _went_live(post)
                if post.network != "facebook" or post.status != "posted" or not post.id or live is None:
                    continue
                if not timedelta(0) <= now - live <= timedelta(minutes=cfg["window_minutes"]):
                    continue
                out["posts"] += 1
                seen = {d.get("comment_id") for d in post.comment_desk}
                fresh = [c for c in fetch_comments(client, environ, post.id) if c["id"] not in seen and not c["from_page"] and c["text"].strip()]
                if not fresh:
                    continue
                record = article_record(settings, rec.article_id)
                if not record:
                    out["held"].append((rec.article_id, "the story's record is missing, so nothing was answered"))
                    continue
                payload = {"article": record, "post": post.text, "comments": [{"id": c["id"], "text": c["text"]} for c in fresh]}
                try:
                    data = llm.structured("comment_desk", "Sort these comments and draft the replies the rules allow.", payload, DESK_SCHEMA)
                except BudgetExceeded:
                    out["held"].append((rec.article_id, "the run's model calls ran out; the next run takes these comments"))
                    stop = True
                    break
                except LLMError as exc:  # a refusal included: nothing is recorded, so the next run asks again
                    out["held"].append((rec.article_id, f"the model could not sort the comments ({type(exc).__name__})"))
                    continue
                decisions = {str(d.get("comment_id") or ""): d for d in data.get("decisions") or []}
                replies_here = [str(d.get("reply") or "") for d in post.comment_desk if d.get("reply_id") or (dry_run and d.get("reply"))]
                at = now.isoformat(timespec="seconds")
                for c in fresh:
                    d = decisions.get(c["id"])
                    if d is None:
                        continue  # the model left it out; the next run reads it again
                    category = str(d.get("category") or "other")
                    category = category if category in CATEGORIES else "other"
                    reply = " ".join(str(d.get("reply") or "").split())
                    entry: dict[str, Any] = {"comment_id": c["id"], "category": category, "at": at}
                    if category in REPLY_CATEGORIES and cfg["reply"]:
                        why_not = guard(reply, replies_here)
                        if not why_not and len(replies_here) >= cfg["max_replies_per_post"]:
                            why_not = "the post's reply cap"
                        if not why_not and budget <= 0:
                            continue  # the run's cap: the next run takes it
                        if why_not:
                            entry["held"] = why_not
                            out["held"].append((rec.article_id, f"a {category} reply was held: {why_not}"))
                        else:
                            if not dry_run:
                                try:
                                    entry["reply_id"] = post_reply(client, environ, c["id"], reply)
                                except (social.SocialError, httpx.HTTPError) as exc:
                                    log.warning("reply refused for %s: %s", rec.article_id, exc)
                                    out["held"].append((rec.article_id, f"Facebook refused a reply: {str(exc)[:160]}"))
                                    continue  # not recorded, so the next run tries again
                            entry["reply"] = reply
                            replies_here.append(reply)
                            budget -= 1
                            out["replied"].append((rec.article_id, category, reply))
                    elif category in FLAG_CATEGORIES:
                        entry["flag"] = True
                        out["flagged"].append((rec.article_id, category, post.url))
                    else:
                        out["skipped"][category] = out["skipped"].get(category, 0) + 1
                    post.comment_desk.append(entry)
                    changed = True
            if changed and not dry_run:
                social.save_record(settings, rec)
    finally:
        if own:
            client.close()
    return out


def _went_live(post: social.Post) -> datetime | None:
    for stamp in (post.scheduled_for, post.posted_at):
        if not stamp:
            continue
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return None


def summary(result: dict[str, Any], *, dry_run: bool = False) -> list[str]:
    """The run summary: Nepal Wire's own replies in full, and only counts and links for the rest."""
    lines = [f"## Comment desk{' (dry run: nothing posted)' if dry_run else ''}", ""]
    if not result["posts"]:
        lines.append("- No post is inside its reply window.")
        return lines
    for article_id, category, reply in result["replied"]:
        lines.append(f"- {article_id}: {'would reply' if dry_run else 'replied'} to a {category}: {reply}")
    flagged: dict[tuple[str, str], list[str]] = {}
    for article_id, category, url in result["flagged"]:
        flagged.setdefault((article_id, url), []).append(category)
    for (article_id, url), cats in flagged.items():
        lines.append(f"- {article_id}: {len(cats)} comment{'s' if len(cats) != 1 else ''} need{'s' if len(cats) == 1 else ''} a person ({', '.join(sorted(set(cats)))}): {url}")
    for article_id, why in result["held"]:
        lines.append(f"- {article_id}: {why}")
    if result["skipped"]:
        lines.append("- Left alone: " + ", ".join(f"{n} {c}" for c, n in sorted(result["skipped"].items())))
    if not (result["replied"] or result["flagged"] or result["held"] or result["skipped"]):
        lines.append(f"- {result['posts']} post{'s' if result['posts'] != 1 else ''} in the window, no new comments.")
    return lines
