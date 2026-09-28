"""Stage 4: the picture desk. A real photo first, then an illustration, then the cover card.

The content engine (docs/content-engine.md, section 14) puts a real photograph of the
story's subject on the card: the person, the place, the institution or the thing. For each
subject the writer names, the desk looks in this order:

1. the image Wikidata keeps for the subject (property P18, a file on Commons),
2. Commons files whose structured data says they depict the subject (P180),
3. the subject's own Commons category (P373),
4. a Commons full text search, shortening the query until something turns up,
5. Openverse.

Every candidate carries its documentation: the file's title, description, categories, date
and what it depicts. Identity is confirmed by documentation, never by resemblance, so the
model sees the documentation beside the pixels and picks one picture or none. Only when no
real photo passes does the desk generate an illustration, labelled as one on the card, and
only when that fails too does it draw the cover card. Every image carries a structured
credit, and the article's review record says what the desk searched and why it decided.
"""

from __future__ import annotations

import base64
import html
import io
import logging
import os
import re
import textwrap
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .llm import BaseLLM, BudgetExceeded, LLMError
from .models import Article, ImageAsset, ImageCredit

log = logging.getLogger(__name__)

USER_AGENT = "NepalWireBot/0.2 (https://github.com/inquisitive013/nepal-news-rss; newsroom picture desk)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
# The Commons metadata the identity gate reads, and the credit needs.
EXTMETADATA_KEYS = "ObjectName|ImageDescription|Artist|LicenseShortName|LicenseUrl|Categories|DateTimeOriginal|Restrictions"
CARD_SIZE = (1080, 1400)  # graphic.W, graphic.H: the photo must cover this without blowing up
TAG_RE = re.compile(r"<[^>]+>")
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
]

PICK_SCHEMA = {
    "type": "object",
    "properties": {"chosen_index": {"type": "integer"}, "reason": {"type": "string"}, "alt_text": {"type": "string"}},
}

JsonFetcher = Callable[[str], dict]
BytesFetcher = Callable[[str], tuple[bytes, str]]

# How each candidate was found, strongest identity documentation first.
VIA_ORDER = ("wikidata image", "depicts", "commons category", "commons search", "openverse")


@dataclass
class ImageCandidate:
    provider: str
    url: str  # image bytes to store
    thumb_url: str  # smaller version for the vision check
    page_url: str
    title: str = ""
    author: str = ""
    author_url: str = ""
    license: str = ""
    license_url: str = ""
    width: int = 0  # the original's size, 0 when the provider does not say
    height: int = 0
    # The documentation the identity gate reads.
    file_name: str = ""
    description: str = ""
    categories: str = ""
    date: str = ""
    depicts: str = ""  # what Wikidata or the file's structured data says the picture shows
    restrictions: str = ""
    found_via: str = ""  # one of VIA_ORDER
    subject: str = ""  # the subject the desk was looking for when it found this


# --------------------------------------------------------------------------- HTTP

# Seconds between two requests to one host. Wikimedia asks bots to keep their requests in
# series; Openverse throttles anonymous callers (one request a second when it last said).
_PACE = {
    "api.openverse.org": 1.1,
    "commons.wikimedia.org": 0.2,
    "www.wikidata.org": 0.2,
    "upload.wikimedia.org": 0.2,
}
_PACE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}


def _pace(url: str) -> None:
    host = urlsplit(url).netloc.lower()
    gap = _PACE.get(host, 0.0)
    if not gap:
        return
    with _PACE_LOCK:
        wait = _LAST_CALL.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[host] = time.monotonic()


def fetch_json(url: str, timeout: float = 25.0) -> dict:
    _pace(url)
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def fetch_bytes(url: str, timeout: float = 40.0) -> tuple[bytes, str]:
    _pace(url)
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "").split(";")[0].strip().lower()


class _OutOfRequests(Exception):
    """The story used its share of free library requests."""


class _Counter:
    """Counts requests so one story never hammers the free libraries."""

    def __init__(self, fetch: JsonFetcher, limit: int) -> None:
        self.fetch, self.left, self.used = fetch, max(0, limit), 0

    def __call__(self, url: str) -> dict:
        if self.left <= 0:
            raise _OutOfRequests()
        self.left -= 1
        self.used += 1
        return self.fetch(url)


# --------------------------------------------------------------------------- licences

def license_allowed(name: str, allowed: list[str]) -> bool:
    n = (name or "").lower().replace("_", " ").replace("-", " ")
    if not n:
        return False
    if any(bad in n for bad in (" nc", "non commercial", "noncommercial", " nd", "no derivatives", "fair use", "copyright")):
        return False
    for a in allowed:
        if a.lower().replace("-", " ") in n:
            return True
    return False


def license_rank(name: str) -> int:
    """0 for public domain and CC0, 1 for attribution only, 2 for share alike. Lower is simpler to reuse."""
    n = (name or "").lower()
    if "sa" in re.findall(r"[a-z]+", n) or "share" in n:
        return 2
    if "cc0" in n or "public domain" in n or "pdm" in n:
        return 0
    return 1


# --------------------------------------------------------------------------- Wikimedia Commons

def _imageinfo(width: int) -> str:
    return (
        f"&prop=imageinfo&iiprop=url|extmetadata|size|mime&iiurlwidth={width}"
        f"&iiextmetadatafilter={quote(EXTMETADATA_KEYS, safe='')}&iiextmetadatalanguage=en"
    )


def commons_search_url(query: str, limit: int = 6, width: int = 1600) -> str:
    q = quote_plus(f"{query} filetype:bitmap")
    return f"{COMMONS_API}?action=query&format=json&generator=search&gsrsearch={q}&gsrnamespace=6&gsrlimit={limit}" + _imageinfo(width)


def commons_depicts_url(qid: str, limit: int = 8, width: int = 1600) -> str:
    """Files whose structured data says they depict the entity: the file documents its own subject."""
    return commons_search_url(f"haswbstatement:P180={qid}", limit, width)


def commons_category_url(category: str, limit: int = 8, width: int = 1600) -> str:
    title = quote_plus(f"Category:{category}")
    return f"{COMMONS_API}?action=query&format=json&generator=categorymembers&gcmtitle={title}&gcmtype=file&gcmlimit={limit}" + _imageinfo(width)


def commons_files_url(names: list[str], width: int = 1600) -> str:
    titles = quote_plus("|".join(f"File:{n}" for n in names))
    return f"{COMMONS_API}?action=query&format=json&titles={titles}" + _imageinfo(width)


def _meta_value(meta: dict, key: str) -> str:
    v = (meta.get(key) or {}).get("value") or ""
    text = html.unescape(TAG_RE.sub(" ", str(v)))
    return re.sub(r"\s+", " ", text).strip()


def _pages(data: dict) -> list[dict]:
    """Result pages in the order the search ranked them. Works for both API format versions."""
    pages = (data.get("query") or {}).get("pages") or {}
    items = list(pages.values()) if isinstance(pages, dict) else list(pages)
    return sorted((p for p in items if isinstance(p, dict)), key=lambda p: p.get("index", 0))


def parse_commons(data: dict, allowed: list[str], depicts: str = "") -> list[ImageCandidate]:
    out: list[ImageCandidate] = []
    for page in _pages(data):
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = (info.get("mime") or "").lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = info.get("extmetadata") or {}
        lic = _meta_value(meta, "LicenseShortName") or _meta_value(meta, "License")
        if not license_allowed(lic, allowed):
            continue
        url = info.get("thumburl") or info.get("url") or ""
        if not url:
            continue
        file_name = re.sub(r"^File:", "", page.get("title", ""))
        title = _meta_value(meta, "ObjectName") or file_name.rsplit(".", 1)[0]
        out.append(
            ImageCandidate(
                provider="Wikimedia Commons",
                url=url,
                thumb_url=url,
                page_url=info.get("descriptionurl") or "",
                title=title[:140],
                author=_meta_value(meta, "Artist")[:120] or "unknown author",
                author_url="",
                license=lic,
                license_url=_meta_value(meta, "LicenseUrl"),
                width=int(info.get("width") or info.get("thumbwidth") or 0),
                height=int(info.get("height") or info.get("thumbheight") or 0),
                file_name=file_name[:160],
                description=_meta_value(meta, "ImageDescription")[:500],
                categories=_meta_value(meta, "Categories").replace("|", ", ")[:400],
                date=_meta_value(meta, "DateTimeOriginal")[:40],
                depicts=depicts,
                restrictions=_meta_value(meta, "Restrictions")[:80],
            )
        )
    return out


# --------------------------------------------------------------------------- Openverse

def openverse_search_url(query: str, limit: int = 6) -> str:
    return f"{OPENVERSE_API}?q={quote_plus(query)}&page_size={limit}&license=by,by-sa,cc0,pdm&mature=false"


def parse_openverse(data: dict, allowed: list[str]) -> list[ImageCandidate]:
    out: list[ImageCandidate] = []
    for item in data.get("results") or []:
        lic_code = (item.get("license") or "").lower()
        version = item.get("license_version") or ""
        if lic_code in ("cc0", "pdm"):
            lic = "CC0 1.0" if lic_code == "cc0" else "Public Domain Mark"
        else:
            lic = f"CC {lic_code.upper()} {version}".strip()
        if not license_allowed(lic, allowed):
            continue
        url = item.get("url") or ""
        if not url:
            continue
        tags = [t.get("name", "") for t in (item.get("tags") or []) if isinstance(t, dict)]
        out.append(
            ImageCandidate(
                provider=f"Openverse ({item.get('source') or item.get('provider') or 'unknown source'})",
                url=url,
                thumb_url=item.get("thumbnail") or url,
                page_url=item.get("foreign_landing_url") or "",
                title=(item.get("title") or "")[:140],
                author=(item.get("creator") or "unknown author")[:120],
                author_url=item.get("creator_url") or "",
                license=lic,
                license_url=item.get("license_url") or "",
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                categories=", ".join(t for t in tags if t)[:400],
            )
        )
    return out


# --------------------------------------------------------------------------- Wikidata

_SMALL_WORDS = {"of", "the", "and", "in", "at", "for", "on", "from", "to", "a", "an", "near", "with", "by"}
_YEAR_RE = re.compile(r"\b(?:1[89]|20)\d{2}\b")
# Search hits that are about a word, not a thing.
_NOT_A_SUBJECT = (
    "disambiguation page",
    "family name",
    "given name",
    "surname",
    "wikimedia list",
    "wikimedia category",
    "wikimedia template",
    "scholarly article",
    "scientific article",
)


@dataclass
class Subject:
    query: str
    qid: str = ""
    label: str = ""
    description: str = ""
    matched_on: str = ""  # the head of the query that found the item
    images: tuple[str, ...] = ()  # P18 file names
    category: str = ""  # P373, the subject's Commons category

    @property
    def depicts(self) -> str:
        """What Wikidata says the item is, and how the desk found it, so the identity gate can judge the match."""
        if not self.qid:
            return ""
        text = f"{self.label}, {self.description} (Wikidata {self.qid})" if self.description else f"{self.label} (Wikidata {self.qid})"
        if self.matched_on and self.matched_on.lower() != self.query.lower():
            text += f", matched on the shorter name \"{self.matched_on}\" while looking for \"{self.query}\""
        return text


def wikidata_search_url(text: str, limit: int = 5) -> str:
    return f"{WIKIDATA_API}?action=wbsearchentities&format=json&language=en&uselang=en&type=item&limit={limit}&search={quote_plus(text)}"


def wikidata_entity_url(qid: str) -> str:
    return f"{WIKIDATA_API}?action=wbgetentities&format=json&ids={quote(qid)}&props={quote('claims|labels|descriptions', safe='')}&languages=en"


def clean_subject(query: str) -> str:
    """A library files a picture under a name, not under the news: drop years and quotes."""
    text = _YEAR_RE.sub(" ", query or "")
    text = re.sub(r"[\"“”‘’()\[\],;]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.;:-")


def heads(query: str) -> list[str]:
    """The query, then shorter and shorter heads of it, never ending on a small word.

    "Supreme Court of Nepal building exterior" gives the whole, then "Supreme Court of Nepal
    building", then "Supreme Court of Nepal", then "Supreme Court". A head never drops below two
    words, because one word names too many things: "Singha Durbar" must not become "Singha".
    """
    words = query.split()
    floor = min(2, len(words))
    out: list[str] = []
    for n in range(len(words), floor - 1, -1):
        head = words[:n]
        if head[-1].lower() in _SMALL_WORDS:
            continue
        out.append(" ".join(head))
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _SMALL_WORDS}


def _hit_text(hit: dict, key: str) -> str:
    value = hit.get(key)
    if isinstance(value, str):
        return value
    return (((hit.get("display") or {}).get(key) or {}).get("value")) or ""


def _claim_strings(statements: Any) -> list[str]:
    out: list[str] = []
    for st in statements or []:
        if not isinstance(st, dict) or st.get("rank") == "deprecated":
            continue
        value = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def pick_entity(head: str, hits: list[dict], country: str = "") -> dict | None:
    """The hit whose label or matched alias holds every word of the head. One that names the country wins."""
    wanted = _words(head)
    if not wanted:
        return None
    fits = []
    for hit in hits:
        label, desc = _hit_text(hit, "label"), _hit_text(hit, "description")
        if any(bad in desc.lower() for bad in _NOT_A_SUBJECT):
            continue
        names = _words(label) | _words((hit.get("match") or {}).get("text", ""))
        for alias in hit.get("aliases") or []:
            names |= _words(alias if isinstance(alias, str) else "")
        if wanted <= names:
            fits.append(hit)
    if not fits:
        return None
    place = (country or "").lower()
    if place:
        for hit in fits:
            if place in f"{_hit_text(hit, 'label')} {_hit_text(hit, 'description')}".lower():
                return hit
    return fits[0]


def resolve_subject(query: str, fetch: JsonFetcher, country: str = "") -> Subject:
    """Find the Wikidata item a subject names, with its image and its Commons category."""
    subject = Subject(query=query)
    for head in heads(query):
        hit = pick_entity(head, (fetch(wikidata_search_url(head)).get("search") or []), country)
        if hit:
            subject.qid = str(hit.get("id") or "")
            subject.label = _hit_text(hit, "label")
            subject.description = _hit_text(hit, "description")
            subject.matched_on = head
            break
    if not subject.qid:
        return subject
    entity = (fetch(wikidata_entity_url(subject.qid)).get("entities") or {}).get(subject.qid) or {}
    claims = entity.get("claims") or {}
    subject.images = tuple(_claim_strings(claims.get("P18"))[:3])
    categories = _claim_strings(claims.get("P373"))
    subject.category = categories[0] if categories else ""
    return subject


# --------------------------------------------------------------------------- the search

_NOT_A_PHOTO = re.compile(
    r"\b(?:logo|logos|emblem|seal|coat of arms|flag of|map|maps|locator|signature|diagram|chart|graph|icon|symbol|insignia|stamp|coin|banknote|screenshot|infographic|poster|letterhead)\b",
    re.IGNORECASE,
)


def usable(cand: ImageCandidate, max_upscale: float = 2.2) -> bool:
    """A photograph, not a logo or a map, big enough to cover the card without turning to mush."""
    if _NOT_A_PHOTO.search(f"{cand.file_name} {cand.title}"):
        return False
    if re.search(r"insignia|trademark", cand.restrictions or "", re.IGNORECASE):
        return False
    if cand.width and cand.height:
        if max(CARD_SIZE[0] / cand.width, CARD_SIZE[1] / cand.height) > max_upscale:
            return False
    return True


def find_candidates(
    queries: list[str],
    allowed: list[str],
    fetch: JsonFetcher = fetch_json,
    *,
    country: str = "Nepal",
    exclude: set[str] | frozenset[str] = frozenset(),
    max_requests: int = 48,
    per_subject: int = 8,
    total: int = 24,
    max_upscale: float = 2.2,
    width: int = 1600,
    stats: dict[str, Any] | None = None,
) -> tuple[list[ImageCandidate], list[dict[str, Any]]]:
    """Every licensed photo the libraries hold of the story's subjects, best documented first.

    Returns the candidates and a trail: per subject, the Wikidata item it resolved to and how
    many photos each source gave. Recently used photos (`exclude`, by file or page address)
    are skipped so recurring subjects rotate pictures. `stats`, when given, receives the
    number of library requests made.
    """
    subjects: list[str] = []
    for q in queries:
        text = clean_subject(q)
        if text and text.lower() not in {s.lower() for s in subjects}:
            subjects.append(text)
    subjects = subjects[:4]
    counted = _Counter(fetch, max_requests)
    seen: set[str] = set(exclude)
    found: list[ImageCandidate] = []
    trail: list[dict[str, Any]] = []

    for order, text in enumerate(subjects):
        if len(found) >= total:
            break
        step: dict[str, Any] = {"subject": text, "entity": "", "found": {}}
        trail.append(step)
        mine: list[ImageCandidate] = []

        def take(cands: list[ImageCandidate], via: str) -> int:
            n = 0
            for cand in cands:
                if len(mine) >= per_subject:
                    break
                keys = {cand.url, cand.page_url} - {""}
                if not keys or keys & seen or not usable(cand, max_upscale):
                    continue
                seen.update(keys)
                cand.found_via, cand.subject = via, text
                mine.append(cand)
                n += 1
            if n:
                step["found"][via] = step["found"].get(via, 0) + n
            return n

        def attempt(label: str, work: Callable[[], Any]) -> Any:
            try:
                return work()
            except _OutOfRequests:
                raise
            except Exception as exc:  # noqa: BLE001 - a provider outage must not stop publication
                log.warning("picture desk: %s failed for %r: %s", label, text, exc)
                step.setdefault("errors", []).append(f"{label}: {type(exc).__name__}")
                return None

        try:
            subject = attempt("wikidata", lambda: resolve_subject(text, counted, country)) or Subject(query=text)
            if subject.qid:
                step["entity"] = f"{subject.qid} {subject.label}".strip()
                if subject.images:
                    attempt("wikidata image", lambda: take(parse_commons(counted(commons_files_url(list(subject.images), width)), allowed, subject.depicts), "wikidata image"))
                if len(mine) < per_subject:
                    attempt("depicts", lambda: take(parse_commons(counted(commons_depicts_url(subject.qid, per_subject, width)), allowed, subject.depicts), "depicts"))
                if subject.category and len(mine) < per_subject:
                    filed = f"in the Commons category {subject.category}, which Wikidata gives for {subject.depicts}"
                    attempt("commons category", lambda: take(parse_commons(counted(commons_category_url(subject.category, per_subject, width)), allowed, filed), "commons category"))
            if len(mine) < per_subject:
                tries = ([subject.label] if subject.label else []) + [h for h in heads(text) if h != subject.label]
                for head in tries[:4]:
                    got = attempt("commons search", lambda head=head: parse_commons(counted(commons_search_url(head, per_subject, width)), allowed))
                    if got:
                        take(got, "commons search")
                        break
            if len(mine) < per_subject:
                tries = ([subject.label] if subject.label else []) + [h for h in heads(text) if h != subject.label]
                for head in tries[:2]:
                    got = attempt("openverse", lambda head=head: parse_openverse(counted(openverse_search_url(head, per_subject)), allowed))
                    if got:
                        take(got, "openverse")
                        break
        except _OutOfRequests:
            step.setdefault("errors", []).append(f"stopped at {counted.used} library requests")
            found.extend(mine)
            break
        found.extend(mine)

    if stats is not None:
        stats["requests"] = counted.used
    rank = {s: i for i, s in enumerate(subjects)}
    ordered = sorted(
        enumerate(found),
        key=lambda item: (
            rank.get(item[1].subject, 99),
            VIA_ORDER.index(item[1].found_via) if item[1].found_via in VIA_ORDER else len(VIA_ORDER),
            license_rank(item[1].license),
            item[0],
        ),
    )
    return [cand for _, cand in ordered][:total], trail


def recent_photo_keys(settings: Settings, article_id: str = "", days: int = 30) -> set[str]:
    """Every real photo a recent story used, by file and page address. Recurring subjects rotate pictures."""
    from . import publish  # late: publishing never needs the picture desk

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    keys: set[str] = set()
    try:
        articles = publish.load_articles(settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("picture desk: could not read recent stories for rotation: %s", exc)
        return keys
    for art in articles:
        if art.id == article_id or not art.image or art.image.credit.kind != "found" or (art.run_date or "") < cutoff:
            continue
        keys.update(k for k in (art.image.original_url, art.image.credit.source_url) if k)
    return keys


# --------------------------------------------------------------------------- vision pick

def _downscale_for_vision(data: bytes, max_side: int = 800) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), "image/jpeg"


@dataclass
class Pick:
    candidate: ImageCandidate | None = None
    alt: str = ""
    reason: str = ""
    shown: int = 0
    rounds: int = 0
    data: bytes = b""  # the picture already downloaded for the check, when it is the file to store


def _documentation(i: int, c: ImageCandidate) -> dict[str, Any]:
    doc = {
        "index": i,
        "title": c.title,
        "file": c.file_name,
        "description": c.description,
        "categories": c.categories,
        "date": c.date,
        "depicts": c.depicts,
        "found_via": c.found_via,
        "looking_for": c.subject,
        "author": c.author,
        "license": c.license,
        "source": c.provider,
        "page_url": c.page_url,
        "size": f"{c.width}x{c.height}" if c.width and c.height else "",
    }
    return {k: v for k, v in doc.items() if v not in ("", None)}


def review_candidates(
    llm: BaseLLM,
    article: Article,
    candidates: list[ImageCandidate],
    fetch: BytesFetcher = fetch_bytes,
    per_round: int = 6,
    rounds: int = 2,
) -> Pick:
    """Show the model the pictures with their documentation, a round at a time, until it picks one or the rounds run out."""
    pick = Pick()
    reasons: list[str] = []
    pos = 0
    subjects = list(dict.fromkeys(c.subject for c in candidates if c.subject))
    for _ in range(max(1, rounds)):
        shown: list[ImageCandidate] = []
        pictures: list[tuple[bytes, str]] = []
        raw: dict[int, bytes] = {}
        while pos < len(candidates) and len(shown) < per_round:
            cand = candidates[pos]
            pos += 1
            try:
                data, ctype = fetch(cand.thumb_url)
                if not ctype.startswith("image/") and data[:3] != b"\xff\xd8\xff" and data[:4] != b"\x89PNG":
                    continue
                pictures.append(_downscale_for_vision(data))
                raw[len(shown)] = data
                shown.append(cand)
            except Exception as exc:  # noqa: BLE001
                log.info("skipping image candidate %s: %s", cand.thumb_url, exc)
        if not shown:
            break
        pick.rounds += 1
        pick.shown += len(shown)
        payload = {
            "headline": article.headline,
            "dek": article.dek,
            "card_headline": article.image_headline,
            "looking_for": subjects,
            "alt_hint": (article.image_brief or {}).get("alt_text", ""),
            "round": pick.rounds,
            "candidates": [_documentation(i, c) for i, c in enumerate(shown)],
        }
        try:
            data = llm.structured("image_picker", "Pick the photograph that carries this story, or reject them all.", payload, PICK_SCHEMA, images=pictures)
        except BudgetExceeded:
            raise
        except LLMError as exc:
            log.warning("image picker failed: %s", exc)
            reasons.append(f"the picture editor failed: {exc}")
            break
        idx = int(data.get("chosen_index", -1))
        reason = (data.get("reason") or "").strip()
        if 0 <= idx < len(shown):
            chosen = shown[idx]
            pick.candidate = chosen
            pick.alt = (data.get("alt_text") or "").strip() or article.headline
            pick.reason = reason
            if chosen.thumb_url == chosen.url:
                pick.data = raw.get(idx, b"")
            return pick
        if reason:
            reasons.append(reason)
    pick.reason = " ".join(reasons) if reasons else "no candidate picture could be downloaded"
    return pick


def pick_image(
    llm: BaseLLM,
    article: Article,
    candidates: list[ImageCandidate],
    fetch: BytesFetcher = fetch_bytes,
    max_candidates: int = 6,
) -> tuple[ImageCandidate, str] | None:
    """One round of the picture editor: the candidate it picked and the alt text, or None."""
    pick = review_candidates(llm, article, candidates, fetch=fetch, per_round=max_candidates, rounds=1)
    return (pick.candidate, pick.alt) if pick.candidate else None


# --------------------------------------------------------------------------- generation

def _openai_error_code(resp: httpx.Response) -> str:
    """OpenAI's machine readable error code. Never the message: it can quote part of the key."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        return "no detail"
    return str(err.get("code") or err.get("type") or "no detail")


def check_openai_key(settings: Settings, environ=None, client: httpx.Client | None = None) -> tuple[str, str]:
    """Prove the OpenAI key works without generating anything. Returns (status, note).

    status is "ok", "failed", "not set" (no key, generation skipped) or "off" (generation disabled).
    Looking up the configured model is free and fails on the same bad key a generation would.
    """
    environ = os.environ if environ is None else environ
    if (settings.get("images.generation.provider") or "none").lower() != "openai":
        return "off", "image generation is switched off in config/settings.yaml"
    key = str(environ.get("OPENAI_API_KEY", "") or "").strip()
    if not key:
        return "not set", "OPENAI_API_KEY is not set; stories without a licensed photo get the cover card"
    model = settings.get("images.generation.openai_model", "gpt-image-1")
    own = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        resp = client.get(f"https://api.openai.com/v1/models/{model}", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return "failed", f"could not reach OpenAI: {type(exc).__name__}"
    finally:
        if own:
            client.close()
    if resp.status_code == 200:
        return "ok", f"OpenAI accepted the key and can see {model}"
    code = _openai_error_code(resp)
    if resp.status_code == 401:
        return "failed", f"OpenAI rejected the key ({code}). Create a new key and replace the OPENAI_API_KEY secret."
    if resp.status_code == 404:
        return "failed", f"the key works but this OpenAI account cannot use {model} ({code})"
    return "failed", f"OpenAI answered HTTP {resp.status_code} ({code})"


def generate_image(prompt: str, settings: Settings) -> tuple[bytes, str] | None:
    """Return (png_or_jpeg_bytes, model_name) or None when generation is unavailable."""
    provider = (settings.get("images.generation.provider") or "none").lower()
    if provider == "none" or not prompt.strip():
        return None
    if provider == "openai":
        # Strip what a copy and paste can drag along: a space or a newline makes a valid key fail.
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            log.info("OPENAI_API_KEY not set, skipping image generation")
            return None
        model = settings.get("images.generation.openai_model", "gpt-image-1")
        size = settings.get("images.generation.size", "1536x1024")
        safe_prompt = (
            prompt.strip()
            + " Editorial illustration for a news site. No text, no letters, no logos, no identifiable real people, no watermarks."
        )
        try:
            with httpx.Client(timeout=180.0) as client:
                resp = client.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "prompt": safe_prompt, "size": size, "n": 1},
                )
                resp.raise_for_status()
                body = resp.json()
            item = (body.get("data") or [{}])[0]
            if item.get("b64_json"):
                return base64.b64decode(item["b64_json"]), model
            if item.get("url"):
                data, _ = fetch_bytes(item["url"])
                return data, model
        except httpx.HTTPStatusError as exc:
            log.warning("image generation failed: HTTP %s from OpenAI (%s)", exc.response.status_code, _openai_error_code(exc.response))
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("image generation failed: %s", exc)
            return None
    log.warning("unknown image generation provider %r", provider)
    return None


# --------------------------------------------------------------------------- cover card

def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _is_latin(text: str) -> bool:
    return all(ord(ch) < 0x0250 or ch in "\u2013\u2014\u2018\u2019\u201c\u201d\u2026" for ch in text)


def cover_card(headline: str, site_name: str, date_label: str, size: tuple[int, int] = (1600, 900)) -> bytes:
    """A branded card used when no photo or illustration is available."""
    w, h = size
    img = Image.new("RGB", size, (24, 24, 32))
    draw = ImageDraw.Draw(img)
    # Crimson to deep blue gradient, the two colours of Nepal's flag.
    top, bottom = (200, 16, 46), (0, 56, 147)
    for y in range(h):
        t = y / max(1, h - 1)
        color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=color)
    # Two pennant shapes as a quiet nod to the flag.
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.polygon([(w * 0.62, 0), (w, 0), (w, h * 0.55)], fill=(255, 255, 255, 18))
    od.polygon([(w * 0.72, h), (w, h * 0.35), (w, h)], fill=(0, 0, 0, 40))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    margin = int(w * 0.06)
    draw.text((margin, margin), site_name.upper(), font=_font(int(h * 0.045)), fill=(255, 255, 255))
    draw.text((margin, margin + int(h * 0.06)), date_label, font=_font(int(h * 0.03)), fill=(235, 235, 245))
    if headline and _is_latin(headline):
        font = _font(int(h * 0.085))
        lines = textwrap.wrap(headline, width=28)[:4]
        y = int(h * 0.36)
        for line in lines:
            draw.text((margin, y), line, font=font, fill=(255, 255, 255))
            y += int(h * 0.105)
    else:
        # Non Latin headlines render in the page, not in the card, so fonts never break the glyphs.
        draw.rectangle([margin, int(h * 0.42), margin + int(w * 0.28), int(h * 0.42) + 10], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- finalise

def _burn_credit(img: Image.Image, text: str) -> Image.Image:
    if not text or not _is_latin(text):
        return img
    w, h = img.size
    bar_h = max(28, int(h * 0.045))
    font = _font(max(14, int(bar_h * 0.55)))
    overlay = Image.new("RGBA", (w, bar_h), (0, 0, 0, 150))
    od = ImageDraw.Draw(overlay)
    label = text
    while od.textlength(label, font=font) > w - 24 and len(label) > 12:
        label = label[:-4].rstrip() + "\u2026"
    od.text((12, (bar_h - font.size) // 2 - 1), label, font=font, fill=(255, 255, 255, 235))
    base = img.convert("RGBA")
    base.alpha_composite(overlay, (0, h - bar_h))
    return base.convert("RGB")


def store_image(
    data: bytes,
    article_id: str,
    settings: Settings,
    credit: ImageCredit,
    alt: str,
    original_url: str = "",
    prefer_png: bool = False,
) -> ImageAsset:
    max_width = int(settings.get("images.max_width", 1600))
    quality = int(settings.get("images.jpeg_quality", 84))
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
    if settings.get("images.burn_credit", True):
        img = _burn_credit(img, credit.line())
    ext = "png" if prefer_png else "jpg"
    images_dir = settings.data_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    rel = f"data/images/{article_id}.{ext}"
    out = settings.root / rel
    if prefer_png:
        img.save(out, format="PNG", optimize=True)
    else:
        img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return ImageAsset(path=rel, alt=alt, width=img.width, height=img.height, credit=credit, original_url=original_url)


def find_real_photo(
    llm: BaseLLM,
    settings: Settings,
    article: Article,
    fetch_json_fn: JsonFetcher = fetch_json,
    fetch_bytes_fn: BytesFetcher = fetch_bytes,
    exclude: set[str] | None = None,
) -> tuple[ImageAsset | None, dict[str, Any]]:
    """The picture desk's first and main job: a licensed real photo of the subject, stored with its credit.

    Returns the stored asset (None when no photo passed) and the desk's record: what it looked
    for, what each library gave, how many pictures the model saw and why it decided.
    """
    brief = article.image_brief or {}
    queries = [q for q in (brief.get("search_queries") or []) if str(q).strip()] or [article.headline]
    allowed = list(settings.get("images.allowed_licenses", ["cc0", "public domain", "cc by"]))
    if exclude is None:
        exclude = recent_photo_keys(settings, article.id, int(settings.get("images.rotation_days", 30)))
    stats: dict[str, Any] = {}
    candidates, trail = find_candidates(
        queries,
        allowed,
        fetch_json_fn,
        country=(article.country or "Nepal").title(),
        exclude=exclude,
        max_requests=int(settings.get("images.max_search_requests", 48)),
        max_upscale=float(settings.get("images.max_upscale", 2.2)),
        stats=stats,
    )
    record: dict[str, Any] = {
        "searched": trail,
        "requests": stats.get("requests", 0),
        "candidates": len(candidates),
        "shown": 0,
        "rounds": 0,
        "decision": "",
        "reason": "",
    }
    if not candidates:
        record["reason"] = "No licensed photo of any subject turned up in Wikimedia Commons or Openverse."
        return None, record
    pick = review_candidates(
        llm,
        article,
        candidates,
        fetch=fetch_bytes_fn,
        per_round=int(settings.get("images.max_candidates_for_vision", 6)),
        rounds=int(settings.get("images.picker_rounds", 2)),
    )
    record.update(shown=pick.shown, rounds=pick.rounds, reason=pick.reason)
    cand = pick.candidate
    if cand is None:
        return None, record
    try:
        data = pick.data or fetch_bytes_fn(cand.url)[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("downloading chosen image failed: %s", exc)
        record["reason"] = f"The chosen photo could not be downloaded ({type(exc).__name__})."
        return None, record
    credit = ImageCredit(
        kind="found",
        title=cand.title,
        author=cand.author,
        author_url=cand.author_url,
        source=cand.provider,
        source_url=cand.page_url,
        license=cand.license,
        license_url=cand.license_url,
    )
    asset = store_image(data, article.id, settings, credit, pick.alt, original_url=cand.url)
    record.update(decision="found", chosen={"page": cand.page_url, "found_via": cand.found_via, "subject": cand.subject, "depicts": cand.depicts})
    return asset, record


def make_image_for_article(
    llm: BaseLLM,
    settings: Settings,
    article: Article,
    date_label: str,
    fetch_json_fn: JsonFetcher = fetch_json,
    fetch_bytes_fn: BytesFetcher = fetch_bytes,
    generate_fn: Callable[[str, Settings], tuple[bytes, str] | None] = generate_image,
    exclude: set[str] | None = None,
) -> ImageAsset:
    """A real photo when one passes the identity gate, else a labelled illustration, else the cover card.

    The desk's record lands on `article.review.picture`, so every story says why it carries
    the picture it carries.
    """
    brief = article.image_brief or {}
    record: dict[str, Any] = {"searched": [], "candidates": 0, "shown": 0, "rounds": 0, "decision": "", "reason": ""}

    # 1. A licensed real photo of the subject, checked by the model against its documentation.
    if not settings.mock or fetch_json_fn is not fetch_json:
        asset, record = find_real_photo(llm, settings, article, fetch_json_fn, fetch_bytes_fn, exclude)
        if asset is not None:
            article.review.picture = record
            return asset

    # 2. Only when no real photo passed: an illustration, labelled as one on the card and in the credit.
    prompt = brief.get("generation_prompt") or f"Editorial illustration about: {article.headline}"
    generated = None if settings.mock and generate_fn is generate_image else generate_fn(prompt, settings)
    if generated:
        data, model = generated
        credit = ImageCredit(kind="generated", model=model, source=settings.site_name, note=f"Made for {settings.site_name}. Not a photograph.")
        alt = brief.get("alt_text") or f"AI generated illustration for: {article.headline}"
        record["decision"] = "generated"
        article.review.picture = record
        return store_image(data, article.id, settings, credit, alt)

    # 3. Cover card, always available.
    data = cover_card(article.headline, settings.site_name, date_label)
    credit = ImageCredit(kind="cover_card", source=settings.site_name, note=f"Graphic: {settings.site_name}")
    record["decision"] = "cover_card"
    article.review.picture = record
    return store_image(data, article.id, settings, credit, alt=f"{settings.site_name} cover card: {article.headline}", prefer_png=True)
