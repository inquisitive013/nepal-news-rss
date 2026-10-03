"""YouTube Shorts: each morning's Reel, uploaded to the Nepal Wire channel.

The Reel already has a Short's shape: vertical, and no longer than Meta's one minute.
Google lets an app upload only as the channel's owner, through OAuth: a client id and secret
from a Google Cloud project with the YouTube Data API switched on, and a refresh token the owner
granted once with the youtube.upload scope. Each run trades the refresh token for an access
token, makes sure it belongs to the channel named like the site, and sends the file in YouTube's
resumable upload.

Google keeps every video that an unaudited project uploads private, whatever the request asks,
until the project passes the YouTube API Services audit (its videos.insert documentation, as
quoted by search on 3 October 2026). The run reports the privacy YouTube actually set, so that wait shows. A
consent screen left in Testing makes Google expire the refresh token after seven days, so the
project's consent screen must be In production. Errors carry Google's reason codes, never its
messages or any token.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import httpx

from .config import Settings
from .models import Article
from .social import SocialError

ENV_KEYS = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
NEWS_AND_POLITICS = "25"  # YouTube's video category id
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 4900  # YouTube allows 5,000 bytes; a little under leaves room for its counting
TAGS_LIMIT = 400  # YouTube allows 500 characters of tags in all


def connected(environ: Mapping[str, str]) -> bool:
    return all(environ.get(k, "").strip() for k in ENV_KEYS)


def google_error(resp: httpx.Response) -> str:
    """"HTTP 403 quotaExceeded", or "HTTP 400 invalid_grant": the status and Google's reason, never its message."""
    try:
        data = resp.json() or {}
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    reason = ""
    if isinstance(err, str):  # the token endpoint: {"error": "invalid_grant", "error_description": ...}
        reason = err
    elif isinstance(err, dict):
        reasons = [str(e.get("reason")) for e in err.get("errors") or [] if isinstance(e, dict) and e.get("reason")]
        reason = ", ".join(reasons) or str(err.get("status") or "")
    reason = re.sub(r"[^A-Za-z0-9_, ]", "", reason)[:80]
    return f"HTTP {resp.status_code}" + (f" {reason}" if reason else "")


def access_token(client: httpx.Client, environ: Mapping[str, str]) -> str:
    resp = client.post(TOKEN_URL, data={
        "client_id": environ["YOUTUBE_CLIENT_ID"].strip(),
        "client_secret": environ["YOUTUBE_CLIENT_SECRET"].strip(),
        "refresh_token": environ["YOUTUBE_REFRESH_TOKEN"].strip(),
        "grant_type": "refresh_token",
    })
    if resp.status_code >= 400:
        raise SocialError(f"YouTube sign in: {google_error(resp)}")
    token = str((resp.json() or {}).get("access_token") or "")
    if not token:
        raise SocialError("YouTube sign in: no access token came back")
    return token


def channel_ok(client: httpx.Client, token: str, name: str) -> None:
    """Refuse a token that belongs to any channel but the one named like the site, before anything is uploaded."""
    resp = client.get(f"{API}/channels", params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code >= 400:
        raise SocialError(f"YouTube channel: {google_error(resp)}")
    items = (resp.json() or {}).get("items") or []
    if not items:
        raise SocialError(f"YouTube: this Google account has no channel yet; create the {name} channel first")
    title = str(((items[0] or {}).get("snippet") or {}).get("title") or "")
    if title.strip().casefold() != name.strip().casefold():
        # The other channel's name stays out of the log: it may be a person's own.
        raise SocialError(f"YouTube: the token belongs to a channel not named {name}; grant access again and pick the {name} channel")


def check(client: httpx.Client, environ: Mapping[str, str], settings: Settings) -> str:
    channel_ok(client, access_token(client, environ), settings.site_name)
    return f"the {settings.site_name} channel"


def _clean(text: str) -> str:
    return re.sub(r"[<>]", "", text or "").strip()  # YouTube refuses both in titles and descriptions


def title_for(article: Article) -> str:
    """The checked Nepali headline when there is one, else the English, cut at a word to YouTube's limit."""
    nepali = article.nepali or {}
    title = _clean(str(nepali.get("headline") or "") if nepali.get("checked") and not nepali.get("held") else "") or _clean(article.headline)
    if len(title) <= TITLE_LIMIT:
        return title
    cut = title[: TITLE_LIMIT - 1].rsplit(" ", 1)[0].rstrip(" ,;:।")
    return cut + "…"


def description_for(caption: str, url: str, site_name: str) -> str:
    text = _clean(caption)
    tail = f"\n\n{site_name}: {url}" if url else ""
    budget = DESCRIPTION_LIMIT - len(tail.encode("utf-8"))
    while len(text.encode("utf-8")) > budget:
        text = text[: int(len(text) * 0.9)].rstrip()
    return text + tail


def tags_for(article: Article, site_name: str) -> list[str]:
    tags: list[str] = []
    for tag in [site_name, "Nepal", "Nepali news", *[t.replace("-", " ") for t in article.tags or []]]:
        tag = _clean(tag)
        if tag and tag.casefold() not in {t.casefold() for t in tags} and len(", ".join([*tags, tag])) <= TAGS_LIMIT:
            tags.append(tag)
    return tags


def upload_short(
    client: httpx.Client,
    environ: Mapping[str, str],
    video: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    channel: str,
) -> tuple[str, str, str]:
    """Upload the video as a public Short. Returns its id, its address, and the privacy YouTube set."""
    token = access_token(client, environ)
    channel_ok(client, token, channel)
    body = Path(video).read_bytes()
    meta = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": NEWS_AND_POLITICS, "defaultLanguage": "ne", "defaultAudioLanguage": "ne"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "embeddable": True},
    }
    headers = {"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(body))}
    opened = client.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta, headers=headers)
    if opened.status_code >= 400:
        raise SocialError(f"YouTube upload: {google_error(opened)}")
    session = opened.headers.get("Location", "")
    if not session.startswith("https://"):
        raise SocialError("YouTube upload: the upload did not open")
    sent = client.put(session, content=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, timeout=600)
    if sent.status_code >= 400:
        raise SocialError(f"YouTube upload: {google_error(sent)}")
    data = sent.json() or {}
    video_id = str(data.get("id") or "")
    if not video_id:
        raise SocialError("YouTube upload: no video id came back")
    return video_id, f"https://www.youtube.com/shorts/{video_id}", str((data.get("status") or {}).get("privacyStatus") or "")
