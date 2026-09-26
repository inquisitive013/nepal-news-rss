"""Stage 4: find a licensed photo, or generate an illustration, or draw a cover card.

Every image the site publishes carries a structured credit: who made it, where it
came from, under which licence. Found images are checked for relevance by the
model looking at the actual pixels before they are accepted.
"""

from __future__ import annotations

import base64
import html
import io
import logging
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .llm import BaseLLM, LLMError
from .models import Article, ImageAsset, ImageCredit

log = logging.getLogger(__name__)

USER_AGENT = "NepalWireBot/0.1 (https://github.com/inquisitive013/nepal-news-rss; newsroom image search)"
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
    width: int = 0
    height: int = 0


# --------------------------------------------------------------------------- HTTP

def fetch_json(url: str, timeout: float = 25.0) -> dict:
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def fetch_bytes(url: str, timeout: float = 40.0) -> tuple[bytes, str]:
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "").split(";")[0].strip().lower()


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


# --------------------------------------------------------------------------- Wikimedia Commons

def commons_search_url(query: str, limit: int = 6, width: int = 1600) -> str:
    q = quote_plus(f"{query} filetype:bitmap")
    return (
        "https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search"
        f"&gsrsearch={q}&gsrnamespace=6&gsrlimit={limit}"
        f"&prop=imageinfo&iiprop=url|extmetadata|size|mime&iiurlwidth={width}"
    )


def parse_commons(data: dict, allowed: list[str]) -> list[ImageCandidate]:
    out: list[ImageCandidate] = []
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        mime = (info.get("mime") or "").lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = info.get("extmetadata") or {}

        def val(key: str, meta: dict = meta) -> str:
            v = (meta.get(key) or {}).get("value") or ""
            return html.unescape(TAG_RE.sub("", str(v))).strip()

        lic = val("LicenseShortName") or val("License")
        if not license_allowed(lic, allowed):
            continue
        title = val("ObjectName") or re.sub(r"^File:", "", page.get("title", "")).rsplit(".", 1)[0]
        out.append(
            ImageCandidate(
                provider="Wikimedia Commons",
                url=info.get("thumburl") or info.get("url") or "",
                thumb_url=info.get("thumburl") or info.get("url") or "",
                page_url=info.get("descriptionurl") or "",
                title=title[:140],
                author=val("Artist")[:120] or "unknown author",
                author_url="",
                license=lic,
                license_url=val("LicenseUrl"),
                width=int(info.get("thumbwidth") or info.get("width") or 0),
                height=int(info.get("thumbheight") or info.get("height") or 0),
            )
        )
    return out


# --------------------------------------------------------------------------- Openverse

def openverse_search_url(query: str, limit: int = 6) -> str:
    return f"https://api.openverse.org/v1/images/?q={quote_plus(query)}&page_size={limit}&license=by,by-sa,cc0,pdm&mature=false"


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
            )
        )
    return out


def search_images(queries: list[str], allowed: list[str], fetch: JsonFetcher = fetch_json, per_query: int = 6) -> list[ImageCandidate]:
    found: list[ImageCandidate] = []
    seen: set[str] = set()
    for query in [q for q in queries if q.strip()][:4]:
        for name, url_fn, parser in (
            ("commons", commons_search_url, parse_commons),
            ("openverse", openverse_search_url, parse_openverse),
        ):
            try:
                data = fetch(url_fn(query, per_query))
                for cand in parser(data, allowed):
                    if cand.url and cand.url not in seen:
                        seen.add(cand.url)
                        found.append(cand)
            except Exception as exc:  # noqa: BLE001 - a provider outage must not stop publication
                log.warning("image search %s failed for %r: %s", name, query, exc)
    return found


# --------------------------------------------------------------------------- vision pick

def _downscale_for_vision(data: bytes, max_side: int = 800) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), "image/jpeg"


def pick_image(
    llm: BaseLLM,
    article: Article,
    candidates: list[ImageCandidate],
    fetch: BytesFetcher = fetch_bytes,
    max_candidates: int = 6,
) -> tuple[ImageCandidate, str] | None:
    """Show the model the thumbnails and let it pick one, or none."""
    shown: list[ImageCandidate] = []
    images: list[tuple[bytes, str]] = []
    for cand in candidates:
        if len(shown) >= max_candidates:
            break
        try:
            data, ctype = fetch(cand.thumb_url)
            if not ctype.startswith("image/") and data[:4] not in (b"\xff\xd8\xff\xe0", b"\x89PNG"):
                continue
            images.append(_downscale_for_vision(data))
            shown.append(cand)
        except Exception as exc:  # noqa: BLE001
            log.info("skipping image candidate %s: %s", cand.thumb_url, exc)
    if not shown:
        return None
    payload = {
        "headline": article.headline,
        "dek": article.dek,
        "alt_hint": (article.image_brief or {}).get("alt_text", ""),
        "candidates": [
            {"index": i, "title": c.title, "author": c.author, "license": c.license, "source": c.provider, "page_url": c.page_url}
            for i, c in enumerate(shown)
        ],
    }
    try:
        data = llm.structured("image_picker", "Pick the image that best fits this article, or reject all.", payload, PICK_SCHEMA, images=images)
    except LLMError as exc:
        log.warning("image picker failed: %s", exc)
        return None
    idx = int(data.get("chosen_index", -1))
    if idx < 0 or idx >= len(shown):
        return None
    alt = (data.get("alt_text") or "").strip() or article.headline
    return shown[idx], alt


# --------------------------------------------------------------------------- generation

def generate_image(prompt: str, settings: Settings) -> tuple[bytes, str] | None:
    """Return (png_or_jpeg_bytes, model_name) or None when generation is unavailable."""
    provider = (settings.get("images.generation.provider") or "none").lower()
    if provider == "none" or not prompt.strip():
        return None
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
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


def make_image_for_article(
    llm: BaseLLM,
    settings: Settings,
    article: Article,
    date_label: str,
    fetch_json_fn: JsonFetcher = fetch_json,
    fetch_bytes_fn: BytesFetcher = fetch_bytes,
    generate_fn: Callable[[str, Settings], tuple[bytes, str] | None] = generate_image,
) -> ImageAsset:
    brief = article.image_brief or {}
    queries = list(brief.get("search_queries") or [])
    if not queries:
        queries = [article.headline]
    allowed = list(settings.get("images.allowed_licenses", ["cc0", "public domain", "cc by"]))

    # 1. Search licensed photo libraries and let the model check relevance.
    if not settings.mock or fetch_json_fn is not fetch_json:
        candidates = search_images(queries, allowed, fetch=fetch_json_fn)
        if candidates:
            picked = pick_image(llm, article, candidates, fetch=fetch_bytes_fn, max_candidates=int(settings.get("images.max_candidates_for_vision", 6)))
            if picked:
                cand, alt = picked
                try:
                    data, _ = fetch_bytes_fn(cand.url)
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
                    return store_image(data, article.id, settings, credit, alt, original_url=cand.url)
                except Exception as exc:  # noqa: BLE001
                    log.warning("downloading chosen image failed: %s", exc)

    # 2. Generate an illustration.
    prompt = brief.get("generation_prompt") or f"Editorial illustration about: {article.headline}"
    generated = None if settings.mock and generate_fn is generate_image else generate_fn(prompt, settings)
    if generated:
        data, model = generated
        credit = ImageCredit(kind="generated", model=model, source=settings.site_name, note=f"Made for {settings.site_name}. Not a photograph.")
        alt = brief.get("alt_text") or f"AI generated illustration for: {article.headline}"
        return store_image(data, article.id, settings, credit, alt)

    # 3. Cover card, always available.
    data = cover_card(article.headline, settings.site_name, date_label)
    credit = ImageCredit(kind="cover_card", source=settings.site_name, note=f"Graphic: {settings.site_name}")
    return store_image(data, article.id, settings, credit, alt=f"{settings.site_name} cover card: {article.headline}", prefer_png=True)
