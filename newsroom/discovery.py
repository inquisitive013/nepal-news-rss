"""Stage 1: scan feeds for the last 24 hours and produce a clean candidate pool.

No model calls happen here. Everything is deterministic so it can be tested
offline with fixture feeds.
"""

from __future__ import annotations

import html
import logging
import re
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from dateutil import parser as dateparser

from .models import Candidate, FeedHealth

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; NepalWireBot/0.1; +https://github.com/inquisitive013/nepal-news-rss)"
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref"}
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

Fetcher = Callable[[str], bytes]


# --------------------------------------------------------------------------- helpers

def http_fetch(url: str, timeout: float = 20.0) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def strip_html(text: str, limit: int = 600) -> str:
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    text = WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def normalize_url(url: str) -> str:
    """Drop tracking params, fragments and trailing slashes so duplicates match."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    t = html.unescape(title or "").lower()
    t = re.sub(r"[^\w\sऀ-ॿ]", " ", t)
    return WS_RE.sub(" ", t).strip()


def title_tokens(title: str) -> set[str]:
    return {tok for tok in normalize_title(title).split() if len(tok) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def detect_language(*texts: str) -> str:
    joined = " ".join(t for t in texts if t)
    letters = [ch for ch in joined if ch.isalpha()]
    if not letters:
        return "unknown"
    deva = sum(1 for ch in letters if DEVANAGARI.match(ch))
    ratio = deva / len(letters)
    if ratio > 0.5:
        return "ne"
    if ratio < 0.1:
        return "en"
    return "mixed"


def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime.fromtimestamp(timegm(val), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    for key in ("published", "updated", "created", "dc_date"):
        val = entry.get(key)
        if val:
            try:
                dt = dateparser.parse(val)
            except (ValueError, OverflowError, TypeError):
                continue
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    return None


def google_news_search_url(query: str, hl: str = "en-NP", gl: str = "NP", ceid: str = "NP:en") -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"


def split_google_title(title: str) -> tuple[str, str]:
    """Google News titles end with ' - Publisher'. Return (title, publisher)."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head.strip() and len(tail.strip()) < 60:
            return head.strip(), tail.strip()
    return title.strip(), ""


# --------------------------------------------------------------------------- parsing

def parse_feed(
    raw: bytes,
    source_name: str,
    source_domain: str,
    via: str,
    default_language: str = "unknown",
    now: datetime | None = None,
    window_hours: int = 24,
    keep_undated: bool = True,
) -> tuple[list[Candidate], int]:
    """Turn raw feed bytes into Candidates inside the time window.

    Returns (candidates_in_window, total_entries).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    parsed = feedparser.parse(raw)
    out: list[Candidate] = []
    for entry in parsed.entries:
        title = html.unescape(WS_RE.sub(" ", entry.get("title", "") or "")).strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        publisher = source_name
        domain = source_domain
        if via.startswith("google_news"):
            title, gpub = split_google_title(title)
            src = entry.get("source")
            if isinstance(src, dict) and src.get("title"):
                publisher = src["title"]
                href = src.get("href") or ""
                if href:
                    domain = urlsplit(href).netloc.lower().removeprefix("www.")
            elif gpub:
                publisher = gpub
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        if via.startswith("google_news") and summary.lower().startswith(title.lower()[:40]):
            summary = ""  # Google News summaries repeat the headline as a link
        when = entry_datetime(entry)
        if when is not None:
            if when < cutoff or when > now + timedelta(hours=2):
                continue
            published = when.replace(microsecond=0).isoformat()
        else:
            if not keep_undated:
                continue
            published = None
        lang = detect_language(title, summary)
        if lang == "unknown":
            lang = default_language
        out.append(
            Candidate(
                id=Candidate.make_id(normalize_url(link), title),
                title=title,
                summary=summary,
                url=link,
                source=publisher,
                source_domain=domain,
                published=published,
                language=lang,
                via=via,
            )
        )
    return out, len(parsed.entries)


# --------------------------------------------------------------------------- dedupe

def dedupe(candidates: Iterable[Candidate], title_threshold: float = 0.8) -> list[Candidate]:
    """Merge duplicates by URL and near identical titles. Native RSS wins over Google News."""
    ranked = sorted(
        candidates,
        key=lambda c: (c.via != "rss", c.published is None, c.published or ""),
    )
    seen_urls: dict[str, Candidate] = {}
    kept: list[Candidate] = []
    kept_tokens: list[set[str]] = []
    for cand in ranked:
        key = normalize_url(cand.url)
        if key in seen_urls:
            continue
        toks = title_tokens(cand.title)
        duplicate = False
        for other_toks in kept_tokens:
            if jaccard(toks, other_toks) >= title_threshold:
                duplicate = True
                break
        if duplicate:
            continue
        seen_urls[key] = cand
        kept.append(cand)
        kept_tokens.append(toks)
    kept.sort(key=lambda c: (c.published is None, c.published or ""), reverse=True)
    # Undated items sort last regardless of the reverse flag above.
    dated = [c for c in kept if c.published]
    undated = [c for c in kept if not c.published]
    dated.sort(key=lambda c: c.published or "", reverse=True)
    return dated + undated


def cap_per_source(candidates: list[Candidate], max_per_source: int, max_total: int) -> list[Candidate]:
    counts: dict[str, int] = {}
    out: list[Candidate] = []
    for cand in candidates:
        key = cand.source_domain or cand.source
        if counts.get(key, 0) >= max_per_source:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(cand)
        if len(out) >= max_total:
            break
    return out


# --------------------------------------------------------------------------- orchestration

def discover(
    sources: list[dict],
    google_news: list[dict],
    window_hours: int = 24,
    max_per_source: int = 20,
    max_total: int = 120,
    fetcher: Fetcher = http_fetch,
    now: datetime | None = None,
    fixtures_dir: Path | None = None,
) -> tuple[list[Candidate], list[FeedHealth]]:
    """Fetch every source, fall back to Google News per domain, dedupe and cap."""
    now = now or datetime.now(timezone.utc)
    health: list[FeedHealth] = []
    pool: list[Candidate] = []

    def fetch_named(url: str, fixture_name: str) -> bytes:
        if fixtures_dir is not None:
            path = fixtures_dir / fixture_name
            if not path.exists():
                raise FileNotFoundError(f"no fixture {path.name}")
            return path.read_bytes()
        return fetcher(url)

    for src in sources:
        name = src["name"]
        domain = src.get("domain", "")
        lang = src.get("language", "unknown")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        got_native = 0
        rss_url = src.get("rss")
        if rss_url:
            try:
                raw = fetch_named(rss_url, f"{slug}.xml")
                cands, total = parse_feed(raw, name, domain, "rss", lang, now, window_hours)
                got_native = len(cands)
                pool.extend(cands)
                health.append(FeedHealth(name, rss_url, "rss", True, total, len(cands)))
            except Exception as exc:  # noqa: BLE001 - any feed failure is recorded, never fatal
                health.append(FeedHealth(name, rss_url, "rss", False, error=f"{type(exc).__name__}: {exc}"[:200]))
        if got_native == 0 and domain:
            hl, gl, ceid = ("ne", "NP", "NP:ne") if lang == "ne" else ("en-NP", "NP", "NP:en")
            gurl = google_news_search_url(f"site:{domain} when:1d", hl, gl, ceid)
            try:
                raw = fetch_named(gurl, f"{slug}.gnews.xml")
                cands, total = parse_feed(raw, name, domain, "google_news_fallback", lang, now, window_hours)
                pool.extend(cands)
                health.append(FeedHealth(name, gurl, "google_news_fallback", True, total, len(cands)))
            except Exception as exc:  # noqa: BLE001
                health.append(FeedHealth(name, gurl, "google_news_fallback", False, error=f"{type(exc).__name__}: {exc}"[:200]))
        time.sleep(0)  # yield point for very long source lists

    for i, gq in enumerate(google_news):
        url = google_news_search_url(gq["query"], gq.get("hl", "en-NP"), gq.get("gl", "NP"), gq.get("ceid", "NP:en"))
        name = f"Google News: {gq['query']}"
        try:
            raw = fetch_named(url, f"gnews-{i}.xml")
            cands, total = parse_feed(raw, name, "news.google.com", "google_news", gq.get("language", "unknown"), now, window_hours)
            pool.extend(cands)
            health.append(FeedHealth(name, url, "google_news", True, total, len(cands)))
        except Exception as exc:  # noqa: BLE001
            health.append(FeedHealth(name, url, "google_news", False, error=f"{type(exc).__name__}: {exc}"[:200]))

    deduped = dedupe(pool)
    capped = cap_per_source(deduped, max_per_source, max_total)
    log.info("discovery: %d raw, %d deduped, %d kept", len(pool), len(deduped), len(capped))
    return capped, health
