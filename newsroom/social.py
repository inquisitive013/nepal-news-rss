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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Callable, Mapping

import httpx

from . import graphic, nepali as nepali_edition, publish
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

LIMITS = {"x": 280, "facebook": 60000, "instagram": 2200, "threads": 500, "telegram": 1024, "bluesky": 300, "mastodon": 500}
X_URL_LENGTH = 23  # every link counts as 23 characters on X
# A rule between the desk's take and the article, and before the sources, so a caption reads in sections.
RULE = "\u2500" * 24
STORY_LABEL = "THE STORY"

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
    status: str  # posted | skipped | failed | removed (taken down and replaced by a corrected post)
    text: str = ""
    id: str = ""
    url: str = ""
    error: str = ""
    posted_at: str = ""
    scheduled_for: str = ""  # local time when the network will release it, empty when posted at once
    edited_at: str = ""  # when the caption was last rewritten in place; `text` is the caption now live
    # What the post did, read by `python -m newsroom insights`: "24h" and "72h", each a reading
    # {at, hours, viewers, views, shares, comments, reactions, errors}.
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    # What the comment desk did with each reader comment: its id, category and time, and Nepal
    # Wire's reply when one went out. Never the reader's words.
    comment_desk: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SocialRecord:
    article_id: str
    article_url: str = ""
    posts: list[Post] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def secret(environ: Mapping[str, str], key: str) -> str:
    """A secret as stored, minus the accidents of pasting: whitespace, quotes, a `key=` label."""
    value = (environ.get(key) or "").strip()
    for label in (f"{key}=", f"{key.lower()}=", "access_token=", "access_token:", "token=", "token:", "id=", "id:"):
        if value.lower().startswith(label):
            value = value[len(label):].strip()
    value = value.strip("\"'`,;{} \t\r\n")
    # No secret in this file has whitespace inside it. A token copied across a wrapped
    # line picks up a line break in the middle; drop every space and break anywhere.
    return "".join(value.split())


def secret_shape_problem(value: str, *, kind: str) -> str:
    """Why a secret cannot be right, without echoing it. Empty when it looks fine."""
    if not value:
        return "is empty"
    if any(ch.isspace() for ch in value):
        return "contains a space or line break"
    if kind == "digits":
        return "" if value.isdigit() else "should be digits only"
    if kind == "token":
        if len(value) < 20:
            return "is far too short to be a token"
        if not re.fullmatch(r"[A-Za-z0-9_\-.:]+", value):
            return "contains characters no token has (quotes, braces, a label)"
    return ""


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


def plain_text(markdown: str) -> str:
    """The article body without markdown marks: headings become their own line, links keep their text."""
    text = markdown or ""
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<!\w)([*_])(?!\s)(.+?)(?<!\s)\1(?!\w)", r"\2", text)
    text = re.sub(r"^[ \t]*[-*][ \t]+", "• ", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def facebook_mode(settings: Settings) -> str:
    """'photo': the picture with the whole article as caption. 'link': a short post with a link card."""
    mode = str(settings.get("social.facebook.mode", "photo") or "photo").lower()
    return mode if mode in ("photo", "link") else "photo"


def caption_style(settings: Settings) -> str:
    """'short': the Nepali hook, the one fact and the question. 'synopsis': the Nepali two line
    synopsis of 29 September. 'full': the long caption in every language listed."""
    style = str(settings.get("social.facebook.caption", "short") or "short").lower()
    return style if style in ("short", "synopsis", "full") else "short"


def nepali_short(article: Article) -> str:
    """The hook and the fact on two lines, the question under them; "" when the piece has no hook.

    A piece from before 29 September carries a long caption with a body; its hook was written to
    open 80 words, not to stand alone, so it is not used here.
    """
    nepali = article.nepali or {}
    if not nepali_edition.usable(nepali):
        return ""
    cap = nepali.get("caption") or {}
    hook, angle, trigger = (str(cap.get(k, "") or "").strip() for k in ("hook", "angle", "trigger"))
    if not hook or str(cap.get("body", "") or "").strip():
        return ""
    return "\n\n".join(p for p in ("\n".join(p for p in (hook, angle) if p), trigger) if p)


def nepali_synopsis(article: Article) -> str:
    """The checked Nepali piece's two line synopsis, or "" when the story has none."""
    nepali = article.nepali or {}
    if not nepali_edition.usable(nepali):
        return ""
    return str((nepali.get("caption") or {}).get("synopsis", "") or "").strip()


def article_url(settings: Settings, article: Article) -> str:
    return f"{settings.site_url.rstrip('/')}/articles/{article.slug}/"


def image_url(settings: Settings, article: Article) -> str:
    if not article.image:
        return ""
    return f"{settings.site_url.rstrip('/')}/images/{Path(article.image.path).name}"


def card_url(settings: Settings, article: Article, language: str = "en") -> str:
    """The 1080x1400 card the site build renders for every article with a photo, English or Nepali.

    The article's version rides on the address, so a corrected card is fetched fresh: neither
    the site's cache nor Facebook's can hand back the card it replaced.
    """
    if not article.image:
        return ""
    sub = "ne/" if language == "ne" else ""
    return f"{settings.site_url.rstrip('/')}/cards/{sub}{article.id}.jpg?v={max(1, int(article.version or 1))}"


def facebook_card_url(settings: Settings, article: Article, client: httpx.Client) -> str:
    """The card Facebook fetches: the Nepali one when the settings ask for it and the site has it, else the English."""
    english = card_url(settings, article)
    if not english or not graphic.wants_nepali_card(settings, article):
        return english
    nepali = card_url(settings, article, "ne")
    try:
        if client.get(nepali).status_code == 200:
            return nepali
    except httpx.HTTPError as exc:
        log.debug("checking %s: %s", nepali, exc)
    log.warning("the Nepali card for %s is not on the site; Facebook gets the English card", article.id)
    return english


def nepali_article_url(settings: Settings, article: Article) -> str:
    return f"{settings.site_url.rstrip('/')}/ne/articles/{article.slug}/"


def caption_languages(settings: Settings) -> list[str]:
    """The languages of the Facebook caption, in order. Unknown codes are dropped; nothing left means English."""
    raw = settings.get("social.facebook.languages") or ["en"]
    codes = [str(code).strip().lower() for code in raw if str(code).strip()]
    return [code for code in codes if code in ("en", "ne")] or ["en"]


_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "no.", "rs.", "jr.", "sr.", "lt.", "col.", "gen.", "capt.", "prof.", "u.s.", "u.k.", "e.g.", "i.e.", "vs.", "etc.", "govt.", "dept."}
_SENTENCE_BREAK = re.compile(r"([.!?\u0964][\"\u201d\u2019)]?)\s+(?=[\"\u201c\u2018(]?[A-Z0-9\u0900-\u097F])")


def split_sentences(text: str) -> list[str]:
    """Sentences in English or Nepali. Initials and common abbreviations never end one."""
    out: list[str] = []
    start = 0
    for m in _SENTENCE_BREAK.finditer(text):
        head = text[start : m.end(1)]
        last = head.split()[-1].lower() if head.split() else ""
        if last in _ABBREVIATIONS or re.fullmatch(r"[a-z]\.", last):
            continue
        out.append(head.strip())
        start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def paragraphs(body: str, per: int = 2, min_words: int = 60) -> str:
    """A caption body in short paragraphs, as the style guide asks, even when the writer sent one block.

    Only the line breaks change; every word stays where it was. A body that already has
    paragraphs, or is short, is left alone.
    """
    text = (body or "").strip()
    if "\n\n" in text or len(text.split()) < min_words:
        return text
    sentences = split_sentences(text)
    if len(sentences) <= per:
        return text
    return "\n\n".join(" ".join(sentences[i : i + per]) for i in range(0, len(sentences), per))


def engine_caption(article: Article, settings: Settings, tags: str) -> str:
    """The Facebook caption to the content engine.

    With ``social.facebook.caption`` on "short" and a checked Nepali piece that has them, the post
    is three Nepali lines: what the story changes for the reader, the one fact the coverage
    missed, and a question about the reader's own life, nothing else: the sources live on the
    card. A piece without them falls back to its two line synopsis ("synopsis"), and one without
    either to the long form: headline, hook, body, trigger and the locked close. With a checked
    Nepali version and ``social.facebook.languages`` listing ``ne`` first, the Nepali caption opens
    the post and the English follows under a rule. The close carries both.
    """
    site = (settings.get("site.name") or "Nepal Wire").strip()
    site_ne = str(settings.get("site.name_ne") or "").strip() or site
    link = bool(settings.get("social.facebook.include_link"))
    nepali = article.nepali or {}
    ne_cap = nepali.get("caption") or {}
    style = caption_style(settings)
    short = (nepali_short(article) if style == "short" else "") or (nepali_synopsis(article) if style in ("short", "synopsis") else "")
    if short:
        # The card carries the headline and the sources; the caption is a few Nepali lines under it.
        extra = [f"पूरा समाचार: {nepali_article_url(settings, article)}"] if link else []
        return "\n\n".join(p for p in (short, *extra) if p)

    def english() -> tuple[str, list[str]]:
        cap = article.caption or {}
        if (cap.get("body") or "").strip():
            parts = [article.headline.strip(), (cap.get("hook") or "").strip(), paragraphs(cap["body"]), (cap.get("trigger") or "").strip()]
        else:  # a story from before the engine caption existed: the headline, its one line hook, the take
            parts = [article.headline.strip(), (article.social_hook or article.dek or "").strip(), (article.take or "").strip()]
        close = ([f"Full story: {article_url(settings, article)}"] if link else []) + ["Sources available in graphic.", f"Follow {site}."]
        return "\n\n".join(p for p in parts if p), close

    def nepali_block() -> tuple[str, list[str]] | None:
        if not (nepali_edition.usable(nepali) and (ne_cap.get("body") or "").strip()):
            return None
        parts = [str(nepali.get("headline", "")).strip(), (ne_cap.get("hook") or "").strip(), paragraphs(ne_cap.get("body") or ""), (ne_cap.get("trigger") or "").strip()]
        close = ([f"पूरा समाचार: {nepali_article_url(settings, article)}"] if link else []) + ["स्रोतहरू ग्राफिकमा छन्।", f"{site_ne} फलो गर्नुहोस्।"]
        return "\n\n".join(p for p in parts if p), close

    blocks: list[str] = []
    closes: list[str] = []
    for lang in caption_languages(settings):
        got = nepali_block() if lang == "ne" else english()
        if got:
            blocks.append(got[0])
            closes.extend(got[1])
    if not blocks:  # only Nepali was asked for and this story has none yet
        text, closes = english()
        blocks = [text]
    close = "\n".join(closes) + (f"\n{tags}" if tags else "")
    return f"\n\n{RULE}\n\n".join(blocks) + "\n\n" + close


def compose(network: str, article: Article, settings: Settings) -> str:
    """The text for one network. Links count, limits hold, hashtags come last."""
    url = article_url(settings, article)
    hook = (article.social_hook or article.dek or article.headline).strip()
    # The desk's take leads the long form posts: it is what readers see before "See more".
    take = (article.take or "").strip()
    take_line = f"{(settings.get('site.name') or 'Nepal Wire').strip()}'s take: {take}" if take else ""
    n_tags = int((settings.get("social.hashtags") or {}).get(network, DEFAULT_HASHTAGS.get(network, 0)))
    tags = " ".join(hashtags(article.tags, n_tags))
    limit = LIMITS[network]

    if network == "x":
        budget = limit - X_URL_LENGTH - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    if network == "facebook":
        ne_cap = (article.nepali or {}).get("caption") or {}
        nepali_caption = ne_cap.get("synopsis") or ne_cap.get("body")
        if facebook_mode(settings) == "photo" and article.image and ((article.caption or {}).get("body") or nepali_caption):
            # The card carries the hook and the sources; the caption carries the depth, to the engine's format.
            return fit(engine_caption(article, settings, tags), limit)
        if facebook_mode(settings) == "photo" and article.image:
            # Older articles without an engine caption: the whole story, the take on top.
            names = []
            for src in article.sources:
                name = (src.get("name") or "").strip()
                if name and name not in names:
                    names.append(name)
            parts = [
                take_line,
                f"{RULE}\n{STORY_LABEL}\n{RULE}" if take_line else "",
                article.headline.strip(),
                article.dek.strip(),
                plain_text(article.body_markdown),
                RULE,
                ("Sources: " + ", ".join(names)) if names else "",
                f"Every source, with links: {url}",
                tags,
            ]
            return fit("\n\n".join(p for p in parts if p), limit)
        parts = [take_line, article.headline.strip(), hook if hook != article.headline.strip() and not take_line else "", url, tags]
        return fit("\n\n".join(p for p in parts if p), limit)
    if network == "instagram":
        site = settings.site_url.rstrip("/")
        parts = [
            take_line,
            f"{RULE}\n{STORY_LABEL}\n{RULE}" if take_line else "",
            article.headline.strip(),
            article.dek.strip(),
            hook if hook not in (article.dek.strip(), article.headline.strip()) and not take_line else "",
            f"Full story at the link in our bio: {site}",
            tags,
        ]
        return fit("\n\n".join(p for p in parts if p), limit)
    if network == "threads":
        budget = limit - len(url) - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    if network == "telegram":
        # HTML caption. Telegram escapes: &, <, >.
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        head = f"<b>{esc(article.headline.strip())}</b>\n\n{esc(article.dek.strip())}"
        link = f"<a href=\"{url}\">Read the full story</a>"
        if take_line and len(f"{head}\n\n{esc(take_line)}\n\n{link}") <= limit:
            return f"{head}\n\n{esc(take_line)}\n\n{link}"
        body = f"{head}\n\n{link}"
        return body if len(body) <= limit else f"<b>{esc(fit(article.headline.strip(), 200))}</b>\n\n<a href=\"{url}\">Read the full story</a>"
    if network == "bluesky":
        budget = limit - len(url) - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    if network == "mastodon":
        budget = limit - len(url) - 2 - (len(tags) + 2 if tags else 0)
        return "\n\n".join(p for p in (fit(hook, budget), url, tags) if p)
    raise ValueError(f"unknown network {network}")


def paused_networks(settings: Settings) -> set[str]:
    return {str(n).strip().lower() for n in (settings.get("social.paused") or []) if str(n).strip()}


def configured_networks(settings: Settings, environ: Mapping[str, str]) -> list[str]:
    """Networks that have every secret and are not paused in settings, in posting order."""
    order = settings.get("social.networks") or list(NETWORKS)
    paused = paused_networks(settings)
    return [n for n in order if n in ENV_KEYS and n not in paused and all(environ.get(k, "").strip() for k in ENV_KEYS[n])]


def _graph_version(environ: Mapping[str, str]) -> str:
    return environ.get("META_GRAPH_VERSION", "").strip() or "v23.0"


def instagram_link(environ: Mapping[str, str], client: httpx.Client) -> tuple[str, str]:
    """The Instagram professional account linked to the Facebook Page, by INSTAGRAM_ACCESS_TOKEN: its id, or why there is none.

    The id never reaches a log: callers keep it in the environment they pass on.
    """
    token = environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    page = environ.get("FACEBOOK_PAGE_ID", "").strip()
    if not token or not page:
        return "", "needs INSTAGRAM_ACCESS_TOKEN and FACEBOOK_PAGE_ID to find the account"
    try:
        resp = client.get(f"{META_GRAPH}/{_graph_version(environ)}/{page}", params={"fields": "instagram_business_account", "access_token": token})
    except httpx.HTTPError as exc:
        return "", f"could not reach Meta to find the account ({type(exc).__name__})"
    if resp.status_code >= 400:
        try:
            code = (resp.json().get("error") or {}).get("code")
        except ValueError:
            code = None
        return "", f"Meta would not say which account is linked (code {code})" if code is not None else f"Meta would not say which account is linked (HTTP {resp.status_code})"
    try:
        found = str(((resp.json() or {}).get("instagram_business_account") or {}).get("id") or "")
    except (ValueError, AttributeError):
        found = ""
    if not found:
        return "", "the Page has no linked Instagram professional account yet"
    return found, ""


def with_instagram(environ: Mapping[str, str], client: httpx.Client | None = None) -> Mapping[str, str]:
    """The environment with INSTAGRAM_USER_ID filled in from the Page when only INSTAGRAM_ACCESS_TOKEN is set.

    The account the token may post to is the one linked to the Page, so nobody has to look the
    id up by hand. Without a link the environment comes back as it was and Instagram stays off.
    """
    if environ.get("INSTAGRAM_USER_ID", "").strip() or not environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip():
        return environ
    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        found, why = instagram_link(environ, client)
    finally:
        if own:
            client.close()
    if not found:
        log.warning("Instagram stays off: %s", why)
        return environ
    return {**environ, "INSTAGRAM_USER_ID": found}


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


_PAGE_TOKENS: dict[tuple[str, str], "FacebookPage"] = {}

USER_TOKEN_NOTE = (
    "posting with a Page token derived from your user token. That user token expires in about 60 days; "
    "store the Page's own access_token from me/accounts to make it permanent"
)
STALE_ID_NOTE = (
    "FACEBOOK_PAGE_ID does not match the one Page this token manages, so the newsroom posts to that Page. "
    "Store its id from me/accounts as FACEBOOK_PAGE_ID to make this explicit"
)


@dataclass
class FacebookPage:
    id: str
    token: str
    note: str = ""


def facebook_page(client: httpx.Client, environ: Mapping[str, str]) -> FacebookPage:
    """The Page to act as, the token that acts as it, and a note when something had to be derived.

    FACEBOOK_PAGE_TOKEN may hold the Page's own token, which never expires, or the admin's
    user token. A user token that manages the Page is turned into the Page's token through
    me/accounts on every run, which works until the user token itself expires. When the
    stored id matches none of its Pages but the token manages exactly one, that Page is the
    one the admin ticked for this app, and it is used.
    """
    base = f"{META_GRAPH}/{_graph_version(environ)}"
    page_id = secret(environ, "FACEBOOK_PAGE_ID")
    token = secret(environ, "FACEBOOK_PAGE_TOKEN")
    if (page_id, token) in _PAGE_TOKENS:
        return _PAGE_TOKENS[(page_id, token)]
    me = _raise_for(client.get(f"{base}/me", params={"fields": "id", "access_token": token}), "Facebook")
    if str(me.get("id", "")) == page_id:
        _PAGE_TOKENS[(page_id, token)] = FacebookPage(page_id, token)
        return _PAGE_TOKENS[(page_id, token)]
    # Never echo the token owner's name or ID: workflow logs of a public repo are public.
    url = f"{base}/me/accounts"
    params: dict[str, str] = {"fields": "id,access_token", "limit": "100", "access_token": token}
    managed: list[dict[str, Any]] = []
    for _ in range(5):  # admins of many Pages get several pages of results
        try:
            data = _raise_for(client.get(url, params=params), "Facebook")
        except SocialError as exc:
            if "node type (Page)" in str(exc):
                raise SocialError(
                    "Facebook: FACEBOOK_PAGE_TOKEN is a Page token, but for a different Page than FACEBOOK_PAGE_ID. "
                    "In the me/accounts result, store the id from the same block as this token as FACEBOOK_PAGE_ID, "
                    "or store the access_token of the Page whose id is already there."
                ) from exc
            raise
        managed.extend(e for e in (data.get("data") or []) if e.get("id") and e.get("access_token"))
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = str(nxt), {}
    for entry in managed:
        if str(entry["id"]) == page_id:
            found = FacebookPage(page_id, str(entry["access_token"]), USER_TOKEN_NOTE)
            _PAGE_TOKENS[(page_id, token)] = found
            return found
    # Counts are safe to print; names and IDs are not.
    if not managed:
        raise SocialError(
            "Facebook: FACEBOOK_PAGE_TOKEN is a personal user token that manages no Page. The Page was not ticked when the token was made. "
            "Click Generate Access Token again, choose Edit previous settings in the login window, tick the Page, then extend and store the new token."
        )
    if len(managed) == 1:
        only = managed[0]
        found = FacebookPage(str(only["id"]), str(only["access_token"]), f"{STALE_ID_NOTE}. Also {USER_TOKEN_NOTE}")
        _PAGE_TOKENS[(page_id, token)] = found
        return found
    raise SocialError(
        f"Facebook: FACEBOOK_PAGE_TOKEN is a personal user token that manages {len(managed)} Pages, none with the id in FACEBOOK_PAGE_ID. "
        "In the me/accounts result, copy the id from your Page's block into FACEBOOK_PAGE_ID."
    )


def post_facebook(
    client: httpx.Client,
    environ: Mapping[str, str],
    text: str,
    art_url: str,
    img_url: str,
    *,
    mode: str = "photo",
    publish_at: int | None = None,
) -> tuple[str, str]:
    """Post now, or hand Facebook a scheduled post it releases at `publish_at` (unix time)."""
    fb = facebook_page(client, environ)
    token = fb.token
    base = f"{META_GRAPH}/{_graph_version(environ)}/{fb.id}"
    timing: dict[str, str] = {}
    if publish_at:
        timing = {"published": "false", "scheduled_publish_time": str(int(publish_at))}
    if mode == "photo" and img_url:
        data = _raise_for(client.post(f"{base}/photos", data={"url": img_url, "caption": text, "access_token": token, **timing}), "Facebook")
        post_id = str(data.get("post_id") or data.get("id") or "")
    else:
        data = _raise_for(client.post(f"{base}/feed", data={"message": text, "link": art_url, "access_token": token, **timing}), "Facebook")
        post_id = str(data.get("id", ""))
    return post_id, f"https://www.facebook.com/{post_id}" if post_id else ""


def delete_facebook_post(client: httpx.Client, environ: Mapping[str, str], post_id: str) -> None:
    """Take a post down, for the correction protocol: a card with an error is replaced, not left to circulate."""
    fb = facebook_page(client, environ)
    data = _raise_for(client.delete(f"{META_GRAPH}/{_graph_version(environ)}/{post_id}", params={"access_token": fb.token}), "Facebook")
    if data.get("success") is False:
        raise SocialError(f"Facebook did not delete post {post_id}")


def edit_facebook_caption(client: httpx.Client, environ: Mapping[str, str], post_id: str, text: str) -> None:
    """Rewrite a live post's caption in place. Unlike a takedown, the post keeps its reactions, comments and shares."""
    fb = facebook_page(client, environ)
    data = _raise_for(client.post(f"{META_GRAPH}/{_graph_version(environ)}/{post_id}", data={"message": text, "access_token": fb.token}), "Facebook")
    if data.get("success") is False:
        raise SocialError(f"Facebook did not edit post {post_id}")


def facebook_slots(settings: Settings) -> list[str]:
    """Posting slots in newsroom local time: "now" or HH:MM. One article per slot, top ranked first."""
    slots = settings.get("social.facebook.slots") or ["now"]
    return [str(x).strip().lower() for x in slots if str(x).strip()]


def article_rank(article: Article) -> int:
    """The final ranking position, from the last ranking judge that ranked it. 99 when unknown."""
    rank = 99
    for verdict in (article.review.ranking if article.review else []) or []:
        try:
            rank = int(verdict.get("rank") or rank)
        except (TypeError, ValueError):
            continue
    return rank


def plan_facebook_slots(articles: list[Article], settings: Settings, now: datetime | None = None) -> dict[str, int | None]:
    """Article id -> unix time Facebook should release it, or None to post at once.

    Today's articles come first, best ranked first, then older ones. Slots that are
    already past, or less than ten minutes away (Facebook's minimum), post at once.
    Articles beyond the slots post at once too.
    """
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    ordered = sorted(articles, key=lambda a: (a.run_date or "", article_rank(a), a.published_at), reverse=False)
    ordered.sort(key=lambda a: a.run_date or "", reverse=True)  # stable: newest run date first, rank order kept
    plan: dict[str, int | None] = {}
    slots = facebook_slots(settings)
    for i, art in enumerate(ordered):
        slot = slots[i] if i < len(slots) else "now"
        when: int | None = None
        if slot != "now":
            try:
                hh, mm = (int(x) for x in slot.split(":", 1))
                target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target - local_now >= timedelta(minutes=10):
                    when = int(target.timestamp())
            except ValueError:
                log.warning("ignoring Facebook slot %r: use HH:MM or now", slot)
        plan[art.id] = when
    return plan


def check_facebook(client: httpx.Client, environ: Mapping[str, str]) -> str:
    """Prove the token can act as the Page FACEBOOK_PAGE_ID and that the ID is a Page."""
    base = f"{META_GRAPH}/{_graph_version(environ)}"
    page_id = secret(environ, "FACEBOOK_PAGE_ID")
    token = secret(environ, "FACEBOOK_PAGE_TOKEN")
    problems = []
    if p := secret_shape_problem(page_id, kind="digits"):
        problems.append(f"FACEBOOK_PAGE_ID {p}")
    if p := secret_shape_problem(token, kind="token"):
        problems.append(f"FACEBOOK_PAGE_TOKEN {p}")
    if problems:
        raise SocialError("Facebook: " + "; ".join(problems) + ". Paste the bare value from me/accounts, nothing around it.")
    # With a Page token, /me is the Page itself. With a user token, the Page's token comes from me/accounts.
    try:
        fb = facebook_page(client, environ)
        token, note, page_id = fb.token, fb.note, fb.id
    except SocialError as exc:
        if "could not be decrypted" in str(exc) or "Invalid OAuth access token" in str(exc) or "Error validating access token" in str(exc):
            raise SocialError("Facebook: the stored token is not one Facebook accepts. It was probably cut short, pasted with something extra, or has expired. Copy the Page's access_token from me/accounts again, whole.") from exc
        raise
    # Only Pages have a category. On a personal profile this field does not exist.
    try:
        page = _raise_for(client.get(f"{base}/{page_id}", params={"fields": "name,category", "access_token": token}), "Facebook")
    except SocialError as exc:
        if "could not be decrypted" in str(exc) or "Invalid OAuth access token" in str(exc):
            raise SocialError("Facebook: the stored token is not one Facebook issued. It was probably cut short or pasted with something extra. Copy the Page's access_token from me/accounts again, whole.") from exc
        if "node type (User)" in str(exc) or "nonexisting field" in str(exc):
            raise SocialError(f"Facebook: {page_id} is a personal profile, not a Page. The API can only post to Pages.") from exc
        raise
    label = f"{page.get('name', '?')} (Page"
    if page.get("category"):
        label += f", {page['category']}"
    try:
        counts = _raise_for(client.get(f"{base}/{page_id}", params={"fields": "followers_count,fan_count", "access_token": token}), "Facebook")
        followers = counts.get("followers_count", counts.get("fan_count"))
        if isinstance(followers, int):
            label += f", {followers:,} followers"
    except SocialError:
        pass  # follower counts need pages_read_engagement; the Page is still confirmed
    return label + ")" + (f". Note: {note}" if note else "")


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
    return read_record(path, article.id)


def read_record(path: Path, article_id: str = "") -> SocialRecord:
    data = json.loads(path.read_text(encoding="utf-8"))
    return SocialRecord(article_id=data.get("article_id", article_id or path.stem), article_url=data.get("article_url", ""), posts=[Post(**p) for p in data.get("posts", [])])


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

def articles_to_post(
    settings: Settings,
    run_date: str | None = None,
    max_age_hours: float | None = None,
    article_ids: list[str] | None = None,
) -> list[Article]:
    """Published articles recent enough to announce, newest first.

    `run_date` keeps only that day's articles. `article_ids` names articles outright and
    ignores the age window. The per article record stops anything posting twice.
    """
    max_age = float(max_age_hours if max_age_hours is not None else settings.get("social.max_age_hours", 36))
    now = time.time()
    wanted = set(article_ids or [])
    out = []
    for art in publish.load_articles(settings):
        if wanted:
            if art.id in wanted:
                out.append(art)
            continue
        if run_date and art.run_date != run_date:
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
    facebook_publish_at: int | None = None,
    again: bool = False,
    replace: bool = False,
    edit: bool = False,
) -> SocialRecord:
    rec = load_record(settings, article)
    rec.article_url = article_url(settings, article)
    img = image_url(settings, article)
    card = card_url(settings, article) if facebook_mode(settings) == "photo" else ""
    if card and not dry_run and not edit and "facebook" in (networks if networks is not None else configured_networks(settings, environ)):
        card = facebook_card_url(settings, article, client)
    # `again` posts even where the record says posted; the new post is appended, the old one kept.
    # `replace` takes the last Facebook post down first, then posts the corrected one.
    # `edit` rewrites the live Facebook post's caption in place and posts nothing new: a caption fix
    # that keeps the post's reactions, comments and shares. A card fix still needs `replace`.
    done = set() if (again or replace or edit) else {p.network for p in rec.posts if p.status == "posted"}
    for network in networks if networks is not None else configured_networks(settings, environ):
        if network in done or (edit and network != "facebook"):
            continue
        if network == "facebook" and (article.nepali or {}).get("held"):
            # Facebook carries the Nepali card and caption; a fact the editor's final reading still
            # found wrong keeps the story off the Page until a person fixes it.
            rec.posts.append(Post(network=network, status="skipped", error="held: the Nepali editor's final reading still found a fact wrong", posted_at=utcnow_iso()))
            continue
        text = compose(network, article, settings)
        if dry_run:
            rec.posts.append(Post(network=network, status="skipped", text=text, error="dry run"))
            continue
        if edit:
            live = next((p for p in reversed(rec.posts) if p.network == "facebook" and p.status == "posted" and p.id), None)
            if live is None:
                rec.posts.append(Post(network=network, status="failed", text=text, error="no live Facebook post to edit", posted_at=utcnow_iso()))
                log.warning("no live Facebook post to edit for %s", article.id)
            elif live.text == text:
                log.info("the Facebook post for %s already carries this caption", article.id)
            else:
                try:
                    edit_facebook_caption(client, environ, live.id, text)
                except (SocialError, httpx.HTTPError) as exc:
                    # The live post stays exactly as it was; the attempt is recorded beside it.
                    rec.posts.append(Post(network=network, status="failed", text=text, error=f"could not edit {live.id}, so it stays as it was: {str(exc)[:300]}", posted_at=utcnow_iso()))
                    log.warning("facebook edit failed for %s: %s", article.id, exc)
                else:
                    live.text = text
                    live.edited_at = utcnow_iso()
                    log.info("edited the caption of %s for %s", live.id, article.id)
            continue
        if replace and network == "facebook":
            old = next((p for p in reversed(rec.posts) if p.network == "facebook" and p.status == "posted" and p.id), None)
            if old is not None:
                try:
                    delete_facebook_post(client, environ, old.id)
                except (SocialError, httpx.HTTPError) as exc:
                    # Never leave two copies up: without the takedown, the corrected post waits.
                    rec.posts.append(Post(network=network, status="failed", text=text, error=f"could not take down {old.id}, so the corrected post was not sent: {str(exc)[:300]}", posted_at=utcnow_iso()))
                    log.warning("facebook takedown failed for %s: %s", article.id, exc)
                    continue
                old.status = "removed"
                old.error = f"taken down {utcnow_iso()} and replaced by a corrected post"
                log.info("took down %s for %s", old.id, article.id)
        try:
            kwargs: dict[str, Any] = {}
            if network in ("instagram", "threads"):
                kwargs["sleep"] = sleep
            if network == "bluesky":
                kwargs["article"] = article
            scheduled_for = ""
            if network == "facebook":
                kwargs["mode"] = facebook_mode(settings) if article.image else "link"
                if facebook_publish_at:
                    kwargs["publish_at"] = facebook_publish_at
                    try:
                        tz = ZoneInfo(settings.timezone)
                    except Exception:  # noqa: BLE001
                        tz = timezone.utc
                    scheduled_for = datetime.fromtimestamp(facebook_publish_at, tz).isoformat(timespec="minutes")
            picture = card if network == "facebook" and card else img
            post_id, url = POSTERS[network](client, environ, text, rec.article_url, picture, **kwargs)
            rec.posts.append(Post(network=network, status="posted", text=text, id=post_id, url=url, posted_at=utcnow_iso(), scheduled_for=scheduled_for))
            log.info("posted %s to %s: %s%s", article.id, network, url or post_id, f" (scheduled for {scheduled_for})" if scheduled_for else "")
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
    article_ids: list[str] | None = None,
    now: datetime | None = None,
    again: bool = False,
    replace: bool = False,
    edit: bool = False,
) -> list[SocialRecord]:
    if (again or replace or edit) and not article_ids:
        raise ValueError("posting again or editing needs explicit article ids, or every recent story would be touched")
    if sum((again, replace, edit)) > 1:
        raise ValueError("choose one of again, replace and edit")
    chosen = networks if networks is not None else configured_networks(settings, environ)
    articles = articles_to_post(settings, run_date, max_age_hours, article_ids)
    if not chosen or not articles:
        return []
    # Only articles not yet on Facebook take a slot, so a rerun never shifts the plan.
    pending = [a for a in articles if not any(p.network == "facebook" and p.status == "posted" for p in load_record(settings, a).posts)]
    plan = plan_facebook_slots(pending, settings, now) if "facebook" in chosen else {}
    own_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        wait_s = float(wait_seconds if wait_seconds is not None else settings.get("social.wait_for_site_seconds", 180))
        if wait_s > 0 and not dry_run:
            for art in articles:
                if not wait_for_url(client, article_url(settings, art), wait_s, sleep=sleep):
                    log.warning("%s is not answering yet; posting anyway", article_url(settings, art))
                    break
        return [
            post_article(settings, art, environ, client, networks=chosen, dry_run=dry_run, sleep=sleep, facebook_publish_at=None if (replace or edit) else plan.get(art.id), again=again, replace=replace, edit=edit)
            for art in articles
        ]
    finally:
        if own_client:
            client.close()


def check_networks(settings: Settings, environ: Mapping[str, str], client: httpx.Client | None = None) -> list[dict[str, str]]:
    """One row per network: configured, reachable, which account."""
    own_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    rows = []
    try:
        given = environ
        environ = with_instagram(environ, client)
        for network in settings.get("social.networks") or list(NETWORKS):
            keys = ENV_KEYS.get(network, [])
            missing = [k for k in keys if not environ.get(k, "").strip()]
            if network == "instagram" and missing == ["INSTAGRAM_USER_ID"]:
                # The token is there; the account behind it is what is missing.
                rows.append({"network": network, "configured": "yes", "ok": "no", "account": "", "note": instagram_link(given, client)[1]})
                continue
            if missing:
                rows.append({"network": network, "configured": "no", "ok": "", "account": "", "note": "missing " + ", ".join(missing)})
                continue
            note = "paused in settings, not posting" if network in paused_networks(settings) else ""
            try:
                account = CHECKERS[network](client, environ)
                rows.append({"network": network, "configured": "yes", "ok": "yes", "account": account, "note": note})
            except (SocialError, httpx.HTTPError) as exc:
                rows.append({"network": network, "configured": "yes", "ok": "no", "account": "", "note": str(exc)[:200]})
        # YouTube takes only the Reel, never the card, so it sits outside the posting list.
        from . import youtube

        missing = [k for k in youtube.ENV_KEYS if not environ.get(k, "").strip()]
        if missing:
            rows.append({"network": "youtube", "configured": "no", "ok": "", "account": "", "note": "missing " + ", ".join(missing)})
        else:
            try:
                rows.append({"network": "youtube", "configured": "yes", "ok": "yes", "account": youtube.check(client, environ, settings), "note": "Reels only"})
            except (SocialError, httpx.HTTPError) as exc:
                rows.append({"network": "youtube", "configured": "yes", "ok": "no", "account": "", "note": str(exc)[:200]})
    finally:
        if own_client:
            client.close()
    return rows
