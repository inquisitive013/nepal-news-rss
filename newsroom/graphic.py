"""The Facebook card: one real photo, one headline that survives alone, the source line.

1080x1400, the Nepal Wire frame, built with Pillow. The locked specification lives in
docs/content-engine.md, section 14. The card is rendered into the site at build time, so
the repository never stores it; the photo and the headline are the record.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageStat

from .config import Settings
from .models import Article

log = logging.getLogger(__name__)

W, H = 1080, 1400
HEADER_H = 64
FOOTER_H = 56
MARGIN = 40
HEADLINE_MAX_WIDTH = W - 2 * MARGIN
GRADIENT_START = 0.38

BLACK = (8, 8, 8)
CRIMSON = (195, 28, 28)
DARK_RED = (140, 18, 18)
DEEP_RED = (80, 8, 8)
GOLD = (218, 182, 72)
OFF_WHITE = (240, 232, 218)
LIGHT_GRAY = (195, 185, 185)
PANEL = (22, 14, 14)
WHITE = (255, 255, 255)

THEMES = ["GEOPOLITICS", "DEFENSE", "SECURITY", "POLITICS", "GOVERNANCE", "ECONOMY", "HEALTH", "SCIENCE", "TECH", "STRATEGY", "DISASTER", "VIRAL"]
_THEME_BY_TAG = [
    (("flood", "landslide", "earthquake", "disaster", "monsoon", "rescue", "quake"), "DISASTER"),
    (("army", "military", "defence", "defense", "soldier"), "DEFENSE"),
    (("health", "hospital", "dengue", "disease", "medicine", "vaccine"), "HEALTH"),
    (("economy", "budget", "tax", "rupee", "bank", "trade", "price", "market", "remittance"), "ECONOMY"),
    (("election", "parliament", "party", "minister", "politics", "court", "judiciary", "constitution"), "POLITICS"),
    (("police", "crime", "security", "border", "arrest"), "SECURITY"),
    (("china", "india", "diplomacy", "geopolitics", "treaty"), "GEOPOLITICS"),
    (("science", "research", "climate"), "SCIENCE"),
    (("tech", "internet", "digital", "app"), "TECH"),
]

FONT_DIRS = [Path(__file__).resolve().parent / "fonts", Path("/usr/share/fonts/truetype/dejavu")]
FONT_FILES = {"sans-bold": "DejaVuSans-Bold.ttf", "sans": "DejaVuSans.ttf", "mono-bold": "DejaVuSansMono-Bold.ttf", "mono": "DejaVuSansMono.ttf"}


def font(kind: str, size: int):
    for folder in FONT_DIRS:
        path = folder / FONT_FILES[kind]
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def theme_for(article: Article) -> str:
    """The chip's theme: the writer's choice when it is on the list, else a guess from the tags."""
    given = (article.theme or "").strip().upper()
    if given in THEMES:
        return given
    tags = " ".join(article.tags).lower()
    for words, theme in _THEME_BY_TAG:
        if any(w in tags for w in words):
            return theme
    return "GOVERNANCE"


def country_for(article: Article) -> str:
    return (article.country or "NEPAL").strip().upper() or "NEPAL"


def source_names(article: Article, limit: int = 3) -> list[str]:
    names: list[str] = []
    for src in article.sources:
        name = (src.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names[:limit]


def photo_credit(article: Article, site_name: str) -> str:
    """Licensed photos stay attributed on the card; an illustration says so in plain words."""
    if not article.image:
        return ""
    c = article.image.credit
    if c.kind == "generated":
        return f"Illustration: AI generated for {site_name}. Not a photograph."
    if c.kind == "found":
        bits = ["Photo:"]
        if c.author:
            bits.append(c.author)
        if c.source:
            bits.append(f"via {c.source}" if c.author else c.source)
        if c.license:
            bits.append(f"· {c.license}")
        return " ".join(bits)
    return ""


def date_label(settings: Settings, article: Article) -> str:
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    raw = article.published_at or article.run_date or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw.upper()
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return f"{dt:%b} {dt.day}, {dt.year}".upper()


def cover_crop(img: Image.Image, w: int = W, h: int = H) -> Image.Image:
    """Scale to cover, never stretch, crop favouring the upper part where faces sit."""
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    cx = (nw - w) // 2
    cy = max(0, (nh - h) // 4)
    return img.crop((cx, cy, cx + w, cy + h))


def brightness_factor(img: Image.Image) -> float:
    """0.70 for a well lit photo. A dark photo is lifted instead: identifiability beats the darkening cap."""
    mean = ImageStat.Stat(img.convert("L")).mean[0]
    if mean >= 110:
        return 0.70
    if mean >= 70:
        return 0.85
    return 1.0


def treat(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Brightness(img).enhance(brightness_factor(img))
    return ImageEnhance.Color(img).enhance(0.75)


def bottom_gradient(img: Image.Image) -> Image.Image:
    """Black rising from 38 percent of the height, curve t**0.7, up to 93 percent opaque. Nothing over the subject."""
    y0 = int(img.height * GRADIENT_START)
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    span = max(1, img.height - 1 - y0)
    for y in range(y0, img.height):
        t = (y - y0) / span
        md.line([(0, y), (img.width, y)], fill=int((t**0.7) * 0.93 * 255))
    overlay = Image.new("RGBA", img.size, BLACK + (0,))
    overlay.putalpha(mask)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt) -> int:
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=fnt)
    return right - left


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    for word in text.split():
        trial = " ".join(current + [word])
        if current and text_width(draw, trial, fnt) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def balanced_two_lines(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> list[str] | None:
    """Split the words in two so the lines come out as even as possible. None when no split fits."""
    words = text.split()
    if len(words) < 2:
        return [text] if text_width(draw, text, fnt) <= max_width else None
    best: tuple[int, list[str]] | None = None
    for i in range(1, len(words)):
        pair = [" ".join(words[:i]), " ".join(words[i:])]
        widths = [text_width(draw, line, fnt) for line in pair]
        if max(widths) <= max_width and (best is None or max(widths) < best[0]):
            best = (max(widths), pair)
    return best[1] if best else None


def fit_headline(draw: ImageDraw.ImageDraw, text: str, *, max_width: int = HEADLINE_MAX_WIDTH, max_lines: int = 2, largest: int = 72, smallest: int = 52):
    """The largest size from 72 down to 52 at which the headline sits in two balanced lines. Three lines at the floor is the last resort."""
    for size in range(largest, smallest - 1, -2):
        fnt = font("sans-bold", size)
        if text_width(draw, text, fnt) <= max_width:
            return fnt, [text]
        lines = balanced_two_lines(draw, text, fnt, max_width) if max_lines == 2 else wrap(draw, text, fnt, max_width)
        if lines and len(lines) <= max_lines:
            return fnt, lines
    fnt = font("sans-bold", smallest)
    lines = wrap(draw, text, fnt, max_width)
    if len(lines) > 3:
        log.warning("image headline too long for the card, cut to three lines: %s", text)
        lines = lines[:3]
    return fnt, lines


def _rect(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int, fill) -> None:
    draw.rectangle([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)], fill=fill)


def render_card(settings: Settings, article: Article, out_path: Path) -> Path:
    """Render the 1080x1400 card for one article to out_path (JPEG). Raises when the photo is missing."""
    if not article.image:
        raise ValueError(f"{article.id} has no image")
    photo_path = settings.root / article.image.path
    if not photo_path.exists():
        raise FileNotFoundError(str(photo_path))
    img = bottom_gradient(treat(cover_crop(Image.open(photo_path).convert("RGB"))))
    draw = ImageDraw.Draw(img)
    site_name = settings.site_name
    theme, country = theme_for(article), country_for(article)

    # Header strip: brand, gold dot, theme, date.
    draw.rectangle([0, 0, W, HEADER_H], fill=CRIMSON)
    f_brand, f_label = font("sans-bold", 22), font("mono-bold", 15)
    x = MARGIN
    draw.text((x, 19), site_name.upper(), font=f_brand, fill=WHITE)
    x += text_width(draw, site_name.upper(), f_brand) + 16
    draw.ellipse([x, 27, x + 9, 36], fill=GOLD)
    x += 9 + 16
    draw.text((x, 24), theme, font=f_label, fill=OFF_WHITE)
    date = date_label(settings, article)
    draw.text((W - MARGIN - text_width(draw, date, f_label), 24), date, font=f_label, fill=GOLD)

    # Country and theme chip, top left.
    f_chip = font("mono-bold", 17)
    country_w, theme_w = text_width(draw, country, f_chip), text_width(draw, theme, f_chip)
    tab, pad, chip_h = 8, 16, 44
    chip_w = tab + pad + country_w + 10 + theme_w + pad
    cx0, cy0 = MARGIN, HEADER_H + 28
    chip = Image.new("RGBA", (chip_w, chip_h), PANEL + (215,))
    img.paste(chip, (cx0, cy0), chip)
    draw = ImageDraw.Draw(img)
    draw.rectangle([cx0, cy0, cx0 + chip_w - 1, cy0 + chip_h - 1], outline=GOLD, width=2)
    draw.rectangle([cx0, cy0, cx0 + tab, cy0 + chip_h - 1], fill=GOLD)
    ty = cy0 + 12
    draw.text((cx0 + tab + pad, ty), country, font=f_chip, fill=OFF_WHITE)
    draw.text((cx0 + tab + pad + country_w + 10, ty), theme, font=f_chip, fill=GOLD)

    # Footer strip.
    footer_top = H - FOOTER_H
    draw.rectangle([0, footer_top, W, H], fill=DEEP_RED)
    f_foot, f_credit = font("mono", 13), font("mono", 12)
    foot = f"© {site_name} · Informational only. No liability assumed for use of this content."
    credit = photo_credit(article, site_name)
    draw.text(((W - text_width(draw, foot, f_foot)) // 2, footer_top + (10 if credit else 20)), foot, font=f_foot, fill=LIGHT_GRAY)
    if credit:
        while text_width(draw, credit, f_credit) > W - 2 * MARGIN and len(credit) > 20:
            credit = credit[:-4].rstrip() + "…"
        draw.text(((W - text_width(draw, credit, f_credit)) // 2, footer_top + 32), credit, font=f_credit, fill=LIGHT_GRAY)

    # Source line, centred, clearing the footer by 8px.
    f_src_b, f_src = font("mono-bold", 16), font("mono", 16)
    names = source_names(article)
    label = "SOURCE:"
    while True:
        outlets = " · ".join(names) or site_name
        total = text_width(draw, label, f_src_b) + 8 + text_width(draw, outlets, f_src)
        if total <= W - 2 * MARGIN or len(names) <= 1:
            break
        names = names[:-1]
    src_h = 19
    src_y = footer_top - 8 - src_h
    sx = (W - total) // 2
    draw.text((sx, src_y), label, font=f_src_b, fill=GOLD)
    draw.text((sx + text_width(draw, label, f_src_b) + 8, src_y), outlets, font=f_src, fill=OFF_WHITE)

    # Gold underline, 22px above the source line.
    ul_h, ul_w = 4, 420
    ul_bottom = src_y - 22
    draw.rectangle([(W - ul_w) // 2, ul_bottom - ul_h, (W + ul_w) // 2, ul_bottom], fill=GOLD)

    # The headline, two lines, line one white, line two off white.
    headline = (article.image_headline or article.headline).strip().upper()
    f_head, lines = fit_headline(draw, headline)
    line_h = int(f_head.size * 1.18)
    y = ul_bottom - ul_h - 26 - line_h * len(lines)
    for i, line in enumerate(lines):
        draw.text(((W - text_width(draw, line, f_head)) // 2, y + i * line_h), line, font=f_head, fill=WHITE if i == 0 else OFF_WHITE)

    # Corner brackets, drawn last.
    arm, thick = 40, 5
    for x0, y0, dx, dy in ((0, 0, 1, 1), (W, 0, -1, 1), (0, H, 1, -1), (W, H, -1, -1)):
        _rect(draw, x0, y0, x0 + dx * arm, y0 + dy * thick, DARK_RED)
        _rect(draw, x0, y0, x0 + dx * thick, y0 + dy * arm, DARK_RED)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="JPEG", quality=92, optimize=True, progressive=True)
    return out_path


def card_name(article: Article) -> str:
    return f"{article.id}.jpg"
