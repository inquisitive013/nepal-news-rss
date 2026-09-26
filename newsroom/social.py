"""Stage 7: tell the world. Posts approved articles to every network that has credentials.

Each network is switched on by its secrets alone. Missing secrets mean the network is
skipped, never an error. Every post is recorded in data/social/<article id>.json so a
rerun never posts the same article twice to the same network.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from . import publish
from .config import Settings
from .models import Article, utcnow_iso

log = logging.getLogger(__name__)

NETWORKS = ("x", "facebook", "instagram", "threads", "telegram", "bluesky", "mastodon")

# The secrets that switch each network on. All of them must be present and non empty.
ENV_KEYS: dict[str, list[str]] = {
    "x": ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"],
    "facebook": ["FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_TOKEN"],
    "instagram": ["INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"],
    "threads": ["THREADS_USER_ID", "THREADS_ACCESS_TOKEN"],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
    "bluesky": ["BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"],
    "mastodon": ["MASTODON_BASE_URL", "MASTODON_ACCESS_TOKEN"],
}

LIMITS = {"x": 280, "facebook": 5000, "instagram": 2200, "threads": 500, "telegram": 1024, "bluesky": 300, "mastodon": 500}
X_URL_LENGTH = 23  # every link counts as 23 characters on X
DEFAULT_HASHTAGS = {"x": 2, "facebook": 3, "instagram": 8, "threads": 3, "telegram": 0, "bluesky": 2, "mastodon": 3}
META_GRAPH = "https://graph.facebook.com"
THREADS_GRAPH = "https://graph.threads.net/v1.0"
X_API = "https://api.x.com/2"
BLUESKY_PDS = "https://bsky.social"
BLUESKY_BLOB_LIMIT = 950_000  # bytes, under the 1,000,000 byte cap for blobs


class SocialError(Exception):
    """A network refused or failed. Recorded, never fatal for the run."""


@dataclass
class Post:
    network: str
    status: str  # posted | skipped | failed
    text: str = ""
    id: str = ""
    url: str = ""
    error: str = ""
    posted_at: str = ""


@dataclass
class SocialRecord:
    article_id: str
    article_url: str = ""
    posts: list[Post] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _pct(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="-._~")


def oauth1_authorization(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    params: Mapping[str, Any] | None = None,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """OAuth 1.0a HMAC-SHA1 Authorization header, as X still requires for posting."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp or int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    pairs = sorted((_pct(k), _pct(v)) for k, v in {**dict(params or {}), **oauth}.items())
    param_string = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join([method.upper(), _pct(url), _pct(param_string)])
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def hashtags(tags: list[str], count: int) -> list[str]:
    """#CamelCase tags from the article's tags. Devanagari tags pass through unchanged."""
    out: list[str] = []
    if count <= 0:
        return out
    for tag in tags:
        words = [w for w in re.split(r"[^0-9A-Za-zऀ-ॿ]+", tag or "") if w]
        if not words:
            continue
        joined = "".join(w if re.search(r"[ऀ-ॿ]", w) else w[:1].upper() + w[1:] for w in words)
        tag_text = "#" + joined
        if tag_text not in out:
            out.append(tag_text)
        if len(out) >= count:
            break
    return out


def fit(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)].rstrip()
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def article_url(settings: Settings, article: Article) -> str:
    return f"{settings.site_url.rstrip('/')}/articles/{article.slug}/"


def image_url(settings: Settings, article: Article) -> str:
    if not article.image:
        return ""
    return f"{settings.site_url.rstrip('/')}/images/{Path(article.image.path).name}"


def compose(network: str, article: Article, settings: Settings) -> str:
    """The text for one network. Links count, limits hold, hashtags come last."""
    url = article_url(settings, article)
    hook = (article.social_hook or article.dek or article.headline).strip()
    n_tags = int((settings.get("social.hashtags") or {}).get(network, DEFAULT_HASHTAGS.get(network, 0)))
    tags = " ".join(hashtags(article.tags, n_tags))
    limit = LIMITS[network]

    if network == "x":
        budget = limit - X_URL_LENGTH - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    if network == "facebook":
        parts = [article.headline.strip(), hook if hook != article.headline.strip() else "", url, tags]
        return fit("\n\n".join(p for p in parts if p), limit)
    if network == "instagram":
        site = settings.site_url.rstrip("/")
        parts = [article.headline.strip(), article.dek.strip(), hook if hook not in (article.dek.strip(), article.headline.strip()) else "", f"Full story at the link in our bio: {site}", tags]
        return fit("\n\n".join(p for p in parts if p), limit)
    if network == "threads":
        budget = limit - len(url) - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    if network == "telegram":
        # HTML caption. Telegram escapes: &, <, >.
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        body = f"<b>{esc(article.headline.strip())}</b>\n\n{esc(article.dek.strip())}\n\n<a href=\"{url}\">Read the full story</a>"
        return body if len(body) <= limit else f"<b>{esc(fit(article.headline.strip(), 200))}</b>\n\n<a href=\"{url}\">Read the full story</a>"
    if network == "bluesky":
        budget = limit - len(url) - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    if network == "mastodon":
        budget = limit - len(url) - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    raise ValueError(f"unknown network {network}")


def configured_networks(settings: Settings, environ: Mapping[str, str]) -> list[str]:
    order = settings.get("social.networks") or list(NETWORKS)
    return [n for n in order if n in ENV_KEYS and all(environ.get(k, "").strip() for k in ENV_KEYS[n])]


def _graph_version(environ: Mapping[str, str]) -> str:
    return environ.get("META_GRAPH_VERSION", "").strip() or "v23.0"


def _raise_for(resp: httpx.Response, what: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:300]}
    if resp.status_code >= 400:
        detail = data.get("error") or data.get("errors") or data.get("detail") or data.get("description") or data.get("message") or data
        raise SocialError(f"{what}: HTTP {resp.status_code}: {json.dumps(detail, ensure_ascii=False)[:400]}")
    return data if isinstance(data, dict) else {"data": data}


def _poll_container(client: httpx.Client, url: str, params: dict[str, str], *, what: str, tries: int = 12, delay: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> None:
    """Meta media containers need a moment before publishing. Wait for FINISHED."""
    for _ in range(tries):
        data = _raise_for(client.get(url, params=params), what)
        status = data.get("status_code") or data.get("status") or "FINISHED"
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise SocialError(f"{what}: container failed: {data}")
        sleep(delay)
    raise SocialError(f"{what}: container not ready after {tries} checks")


# --------------------------------------------------------------------------- networks

def post_x(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str) -> tuple[str, str]:
    url = f"{X_API}/tweets"
    auth = oauth1_authorization(
        "POST",
        url,
        consumer_key=environ["X_API_KEY"],
        consumer_secret=environ["X_API_SECRET"],
        token=environ["X_ACCESS_TOKEN"],
        token_secret=environ["X_ACCESS_TOKEN_SECRET"],
    )
    data = _raise_for(client.post(url, json={"text": text}, headers={"Authorization": auth}), "X")
    post_id = str((data.get("data") or {}).get("id", ""))
    return post_id, f"https://x.com/i/web/status/{post_id}" if post_id else ""


def check_x(client: httpx.Client, environ: Mapping[str, str]) -> str:
    url = f"{X_API}/users/me"
    auth = oauth1_authorization("GET", url, consumer_key=environ["X_API_KEY"], consumer_secret=environ["X_API_SECRET"], token=environ["X_ACCESS_TOKEN"], token_secret=environ["X_ACCESS_TOKEN_SECRET"])
    data = _raise_for(client.get(url, headers={"Authorization": auth}), "X")
    return "@" + str((data.get("data") or {}).get("username", "?"))


def post_facebook(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str) -> tuple[str, str]:
    page = environ["FACEBOOK_PAGE_ID"]
    url = f"{META_GRAPH}/{_graph_version(environ)}/{page}/feed"
    data = _raise_for(client.post(url, data={"message": text, "link": art_url, "access_token": environ["FACEBOOK_PAGE_TOKEN"]}), "Facebook")
    post_id = str(data.get("id", ""))
    return post_id, f"https://www.facebook.com/{post_id}" if post_id else ""


def check_facebook(client: httpx.Client, environ: Mapping[str, str]) -> str:
    url = f"{META_GRAPH}/{_graph_version(environ)}/{environ['FACEBOOK_PAGE_ID']}"
    data = _raise_for(client.get(url, params={"fields": "name,id", "access_token": environ["FACEBOOK_PAGE_TOKEN"]}), "Facebook")
    return str(data.get("name", "?"))


def post_instagram(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str, *, sleep: Callable[[float], None] = time.sleep) -> tuple[str, str]:
    if not img_url:
        raise SocialError("Instagram needs a picture and this article has none")
    base = environ.get("INSTAGRAM_API_BASE", "").strip() or f"{META_GRAPH}/{_graph_version(environ)}"
    user = environ["INSTAGRAM_USER_ID"]
    token = environ["INSTAGRAM_ACCESS_TOKEN"]
    created = _raise_for(client.post(f"{base}/{user}/media", data={"image_url": img_url, "caption": text, "access_token": token}), "Instagram")
    creation_id = str(created.get("id", ""))
    if not creation_id:
        raise SocialError(f"Instagram: no container id in {created}")
    _poll_container(client, f"{base}/{creation_id}", {"fields": "status_code", "access_token": token}, what="Instagram", sleep=sleep)
    published = _raise_for(client.post(f"{base}/{user}/media_publish", data={"creation_id": creation_id, "access_token": token}), "Instagram")
    media_id = str(published.get("id", ""))
    permalink = ""
    if media_id:
        try:
            permalink = str(_raise_for(client.get(f"{base}/{media_id}", params={"fields": "permalink", "access_token": token}), "Instagram").get("permalink", ""))
        except SocialError:
            permalink = ""
    return media_id, permalink


def check_instagram(client: httpx.Client, environ: Mapping[str, str]) -> str:
    base = environ.get("INSTAGRAM_API_BASE", "").strip() or f"{META_GRAPH}/{_graph_version(environ)}"
    data = _raise_for(client.get(f"{base}/{environ['INSTAGRAM_USER_ID']}", params={"fields": "username", "access_token": environ["INSTAGRAM_ACCESS_TOKEN"]}), "Instagram")
    return "@" + str(data.get("username", "?"))


def post_threads(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str, *, sleep: Callable[[float], None] = time.sleep) -> tuple[str, str]:
    user = environ["THREADS_USER_ID"]
    token = environ["THREADS_ACCESS_TOKEN"]
    payload = {"media_type": "IMAGE" if img_url else "TEXT", "text": text, "access_token": token}
    if img_url:
        payload["image_url"] = img_url
    created = _raise_for(client.post(f"{THREADS_GRAPH}/{user}/threads", data=payload), "Threads")
    creation_id = str(created.get("id", ""))
    if not creation_id:
        raise SocialError(f"Threads: no container id in {created}")
    if img_url:
        _poll_container(client, f"{THREADS_GRAPH}/{creation_id}", {"fields": "status", "access_token": token}, what="Threads", sleep=sleep)
    published = _raise_for(client.post(f"{THREADS_GRAPH}/{user}/threads_publish", data={"creation_id": creation_id, "access_token": token}), "Threads")
    media_id = str(published.get("id", ""))
    permalink = ""
    if media_id:
        try:
            permalink = str(_raise_for(client.get(f"{THREADS_GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token}), "Threads").get("permalink", ""))
        except SocialError:
            permalink = ""
    return media_id, permalink


def check_threads(client: httpx.Client, environ: Mapping[str, str]) -> str:
    data = _raise_for(client.get(f"{THREADS_GRAPH}/me", params={"fields": "id,username", "access_token": environ["THREADS_ACCESS_TOKEN"]}), "Threads")
    return "@" + str(data.get("username", "?"))


def post_telegram(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str) -> tuple[str, str]:
    base = f"https://api.telegram.org/bot{environ['TELEGRAM_BOT_TOKEN']}"
    chat = environ["TELEGRAM_CHAT_ID"]
    if img_url:
        data = _raise_for(client.post(f"{base}/sendPhoto", data={"chat_id": chat, "photo": img_url, "caption": text, "parse_mode": "HTML"}), "Telegram")
    else:
        data = _raise_for(client.post(f"{base}/sendMessage", data={"chat_id": chat, "text": text, "parse_mode": "HTML"}), "Telegram")
    result = data.get("result") or {}
    message_id = str(result.get("message_id", ""))
    username = (result.get("chat") or {}).get("username", "")
    return message_id, f"https://t.me/{username}/{message_id}" if username and message_id else ""


def check_telegram(client: httpx.Client, environ: Mapping[str, str]) -> str:
    base = f"https://api.telegram.org/bot{environ['TELEGRAM_BOT_TOKEN']}"
    me = _raise_for(client.get(f"{base}/getMe"), "Telegram").get("result") or {}
    chat = _raise_for(client.get(f"{base}/getChat", params={"chat_id": environ["TELEGRAM_CHAT_ID"]}), "Telegram").get("result") or {}
    return f"bot @{me.get('username', '?')} posting to {chat.get('title') or chat.get('username') or environ['TELEGRAM_CHAT_ID']}"


def _bluesky_session(client: httpx.Client, environ: Mapping[str, str]) -> dict[str, Any]:
    pds = environ.get("BLUESKY_PDS", "").strip() or BLUESKY_PDS
    data = _raise_for(client.post(f"{pds}/xrpc/com.atproto.server.createSession", json={"identifier": environ["BLUESKY_HANDLE"], "password": environ["BLUESKY_APP_PASSWORD"]}), "Bluesky")
    data["_pds"] = pds
    return data


def _link_facets(text: str, url: str) -> list[dict[str, Any]]:
    start = text.find(url)
    if start < 0:
        return []
    b_start = len(text[:start].encode("utf-8"))
    b_end = b_start + len(url.encode("utf-8"))
    return [{"index": {"byteStart": b_start, "byteEnd": b_end}, "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}]


def post_bluesky(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str, *, article: Article | None = None) -> tuple[str, str]:
    session = _bluesky_session(client, environ)
    pds, jwt, did = session["_pds"], session["accessJwt"], session["did"]
    headers = {"Authorization": f"Bearer {jwt}"}
    external: dict[str, Any] = {"uri": art_url, "title": (article.headline if article else art_url)[:300], "description": (article.dek if article else "")[:1000]}
    if img_url:
        try:
            img = client.get(img_url)
            if img.status_code == 200 and len(img.content) <= BLUESKY_BLOB_LIMIT:
                blob = _raise_for(client.post(f"{pds}/xrpc/com.atproto.repo.uploadBlob", content=img.content, headers={**headers, "Content-Type": img.headers.get("content-type", "image/jpeg")}), "Bluesky")
                if blob.get("blob"):
                    external["thumb"] = blob["blob"]
        except (httpx.HTTPError, SocialError) as exc:  # a card without a picture is still a card
            log.warning("bluesky thumbnail skipped: %s", exc)
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": utcnow_iso(),
        "langs": [article.language if article and article.language else "en"],
        "facets": _link_facets(text, art_url),
        "embed": {"$type": "app.bsky.embed.external", "external": external},
    }
    data = _raise_for(client.post(f"{pds}/xrpc/com.atproto.repo.createRecord", json={"repo": did, "collection": "app.bsky.feed.post", "record": record}, headers=headers), "Bluesky")
    uri = str(data.get("uri", ""))
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    return uri, f"https://bsky.app/profile/{environ['BLUESKY_HANDLE']}/post/{rkey}" if rkey else ""


def check_bluesky(client: httpx.Client, environ: Mapping[str, str]) -> str:
    session = _bluesky_session(client, environ)
    return "@" + str(session.get("handle", environ["BLUESKY_HANDLE"]))


def post_mastodon(client: httpx.Client, environ: Mapping[str, str], text: str, art_url: str, img_url: str) -> tuple[str, str]:
    base = environ["MASTODON_BASE_URL"].rstrip("/")
    data = _raise_for(client.post(f"{base}/api/v1/statuses", data={"status": text}, headers={"Authorization": f"Bearer {environ['MASTODON_ACCESS_TOKEN']}"}), "Mastodon")
    return str(data.get("id", "")), str(data.get("url", ""))


def check_mastodon(client: httpx.Client, environ: Mapping[str, str]) -> str:
    base = environ["MASTODON_BASE_URL"].rstrip("/")
    data = _raise_for(client.get(f"{base}/api/v1/accounts/verify_credentials", headers={"Authorization": f"Bearer {environ['MASTODON_ACCESS_TOKEN']}"}), "Mastodon")
    return "@" + str(data.get("username", "?"))


POSTERS: dict[str, Callable[..., tuple[str, str]]] = {
    "x": post_x,
    "facebook": post_facebook,
    "instagram": post_instagram,
    "threads": post_threads,
    "telegram": post_telegram,
    "bluesky": post_bluesky,
    "mastodon": post_mastodon,
}
CHECKERS: dict[str, Callable[[httpx.Client, Mapping[str, str]], str]] = {
    "x": check_x,
    "facebook": check_facebook,
    "instagram": check_instagram,
    "threads": check_threads,
    "telegram": check_telegram,
    "bluesky": check_bluesky,
    "mastodon": check_mastodon,
}


# --------------------------------------------------------------------------- records

def record_path(settings: Settings, article_id: str) -> Path:
    return settings.data_dir / "social" / f"{article_id}.json"


def load_record(settings: Settings, article: Article) -> SocialRecord:
    path = record_path(settings, article.id)
    if not path.exists():
        return SocialRecord(article_id=article.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    return SocialRecord(article_id=data.get("article_id", article.id), article_url=data.get("article_url", ""), posts=[Post(**p) for p in data.get("posts", [])])


def save_record(settings: Settings, rec: SocialRecord) -> Path:
    path = record_path(settings, rec.article_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(rec), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def wait_for_url(client: httpx.Client, url: str, timeout_s: float, *, sleep: Callable[[float], None] = time.sleep, interval: float = 10.0) -> bool:
    """True once the page answers 200. Instagram and Threads fetch the picture from the site."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if client.get(url).status_code == 200:
                return True
        except httpx.HTTPError as exc:
            log.debug("waiting for %s: %s", url, exc)
        if time.monotonic() >= deadline:
            return False
        sleep(interval)


# --------------------------------------------------------------------------- driver

def articles_to_post(settings: Settings, run_date: str | None = None, max_age_hours: float | None = None) -> list[Article]:
    """Articles from the latest run (or the given run date) that are recent enough to announce."""
    runs_dir = settings.data_dir / "runs"
    if run_date:
        run_files = [runs_dir / f"{run_date}.json"]
    else:
        run_files = sorted(runs_dir.glob("*.json"))[-1:]
    if not run_files or not run_files[0].exists():
        return []
    run = json.loads(run_files[0].read_text(encoding="utf-8"))
    wanted = set(run.get("published", []))
    if not wanted:
        return []
    max_age = float(max_age_hours if max_age_hours is not None else settings.get("social.max_age_hours", 36))
    now = time.time()
    out = []
    for art in publish.load_articles(settings):
        if art.id not in wanted:
            continue
        try:
            published = publish._parse_iso(art.published_at).timestamp() if art.published_at else now
        except Exception:  # noqa: BLE001
            published = now
        if now - published <= max_age * 3600:
            out.append(art)
    return out


def post_article(
    settings: Settings,
    article: Article,
    environ: Mapping[str, str],
    client: httpx.Client,
    *,
    networks: list[str] | None = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> SocialRecord:
    rec = load_record(settings, article)
    rec.article_url = article_url(settings, article)
    img = image_url(settings, article)
    done = {p.network for p in rec.posts if p.status == "posted"}
    for network in networks if networks is not None else configured_networks(settings, environ):
        if network in done:
            continue
        text = compose(network, article, settings)
        if dry_run:
            rec.posts.append(Post(network=network, status="skipped", text=text, error="dry run"))
            continue
        try:
            kwargs: dict[str, Any] = {}
            if network in ("instagram", "threads"):
                kwargs["sleep"] = sleep
            if network == "bluesky":
                kwargs["article"] = article
            post_id, url = POSTERS[network](client, environ, text, rec.article_url, img, **kwargs)
            rec.posts.append(Post(network=network, status="posted", text=text, id=post_id, url=url, posted_at=utcnow_iso()))
            log.info("posted %s to %s: %s", article.id, network, url or post_id)
        except (SocialError, httpx.HTTPError, KeyError, ValueError) as exc:
            rec.posts.append(Post(network=network, status="failed", text=text, error=str(exc)[:500], posted_at=utcnow_iso()))
            log.warning("%s failed for %s: %s", network, article.id, exc)
    if not dry_run:
        save_record(settings, rec)
    return rec


def post_articles(
    settings: Settings,
    environ: Mapping[str, str],
    *,
    run_date: str | None = None,
    max_age_hours: float | None = None,
    networks: list[str] | None = None,
    dry_run: bool = False,
    wait_seconds: float | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SocialRecord]:
    chosen = networks if networks is not None else configured_networks(settings, environ)
    articles = articles_to_post(settings, run_date, max_age_hours)
    if not chosen or not articles:
        return []
    own_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        wait_s = float(wait_seconds if wait_seconds is not None else settings.get("social.wait_for_site_seconds", 180))
        if wait_s > 0 and not dry_run:
            for art in articles:
                if not wait_for_url(client, article_url(settings, art), wait_s, sleep=sleep):
                    log.warning("%s is not answering yet; posting anyway", article_url(settings, art))
                    break
        return [post_article(settings, art, environ, client, networks=chosen, dry_run=dry_run, sleep=sleep) for art in articles]
    finally:
        if own_client:
            client.close()


def check_networks(settings: Settings, environ: Mapping[str, str], client: httpx.Client | None = None) -> list[dict[str, str]]:
    """One row per network: configured, reachable, which account."""
    own_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    rows = []
    try:
        for network in settings.get("social.networks") or list(NETWORKS):
            keys = ENV_KEYS.get(network, [])
            missing = [k for k in keys if not environ.get(k, "").strip()]
            if missing:
                rows.append({"network": network, "configured": "no", "ok": "", "account": "", "note": "missing " + ", ".join(missing)})
                continue
            try:
                account = CHECKERS[network](client, environ)
                rows.append({"network": network, "configured": "yes", "ok": "yes", "account": account, "note": ""})
            except (SocialError, httpx.HTTPError) as exc:
                rows.append({"network": network, "configured": "yes", "ok": "no", "account": "", "note": str(exc)[:200]})
    finally:
        if own_client:
            client.close()
    return rows
