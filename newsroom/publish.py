"""Stage 6: persist approved articles and build the static site, feed and sitemap."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import graphic
from .config import Settings
from .models import (
    Article,
    ImageAsset,
    ImageCredit,
    ReviewRecord,
    RunLog,
    ValidationRound,
    dump_json,
)

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent / "templates"
STATIC = Path(__file__).resolve().parent / "static"
INDEX_LIMIT = 40
CARD_LIMIT = 60  # cards rendered per build: the recent articles social posting can still reach

_SCRIPT_RE = re.compile(r"<\s*(script|style|iframe|object|embed)[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_ON_ATTR_RE = re.compile(r"\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_HREF_RE = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


def render_markdown(text: str) -> str:
    html = markdown.markdown(text or "", extensions=["sane_lists"], output_format="html5")
    html = _SCRIPT_RE.sub("", html)
    html = _ON_ATTR_RE.sub("", html)
    html = _JS_HREF_RE.sub(r'\1=\2#\2', html)
    return html


# --------------------------------------------------------------------------- persistence

def _from_dict(cls, data: dict[str, Any]):
    """Rebuild nested dataclasses from JSON. Unknown keys are ignored."""
    names = [f.name for f in dataclasses.fields(cls)]
    kwargs = {}
    for name in names:
        if name not in data:
            continue
        val = data[name]
        if name == "image" and isinstance(val, dict):
            val = _from_dict(ImageAsset, val)
        elif name == "credit" and isinstance(val, dict):
            val = _from_dict(ImageCredit, val)
        elif name == "review" and isinstance(val, dict):
            val = _from_dict(ReviewRecord, val)
        elif name == "validation_rounds" and isinstance(val, list):
            val = [_from_dict(ValidationRound, v) for v in val]
        kwargs[name] = val
    return cls(**kwargs)


def article_path(settings: Settings, article: Article) -> Path:
    return settings.data_dir / "articles" / f"{article.id}.json"


def save_article(settings: Settings, article: Article) -> Path:
    path = article_path(settings, article)
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(article, path)
    return path


def save_rejected(settings: Settings, article: Article) -> Path:
    path = settings.data_dir / "rejected" / f"{article.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(article, path)
    return path


def run_order(path: Path) -> tuple[str, int]:
    """Sort key for run records: <date>.json is the day's first run, <date>-2.json the second."""
    stem = path.stem
    date, rest = stem[:10], stem[10:]
    try:
        return date, int(rest[1:]) if rest else 1
    except ValueError:
        return stem, 0


def run_records(settings: Settings) -> list[Path]:
    """Every run record, oldest first."""
    runs = settings.data_dir / "runs"
    return sorted(runs.glob("*.json"), key=run_order) if runs.exists() else []


def save_run(settings: Settings, run: RunLog) -> Path:
    # A day can have more than one run (a manual one and the scheduled one). The first
    # keeps <date>.json, later ones get -2, -3, so no record is ever overwritten.
    runs = settings.data_dir / "runs"
    path = runs / f"{run.run_date}.json"
    n = 2
    while path.exists():
        path = runs / f"{run.run_date}-{n}.json"
        n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(run)
    payload["usage_totals"] = run.usage_totals()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")
    return path


def load_articles(settings: Settings) -> list[Article]:
    out: list[Article] = []
    folder = settings.data_dir / "articles"
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                out.append(_from_dict(Article, json.load(fh)))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping unreadable article %s: %s", path.name, exc)
    out.sort(key=lambda a: a.published_at or a.run_date, reverse=True)
    return out


# --------------------------------------------------------------------------- site build

def _tz(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _parse_iso(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _env(settings: Settings) -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "xml"]), trim_blocks=True, lstrip_blocks=True)
    tz = _tz(settings)

    def fmt_date(value: str, fmt: str = "%d %B %Y, %H:%M") -> str:
        return _parse_iso(value).astimezone(tz).strftime(fmt) + (" NPT" if "%H" in fmt else "")

    def rfc822(value: str) -> str:
        return format_datetime(_parse_iso(value))

    def iso(value: str) -> str:
        return _parse_iso(value).isoformat()

    env.filters["fmt_date"] = fmt_date
    env.filters["rfc822"] = rfc822
    env.filters["iso"] = iso
    env.filters["markdown"] = render_markdown
    return env


def evidenced_angles(article: Article) -> list[dict[str, Any]]:
    """The investigation angles that carry at least one source with a URL."""
    angles = (article.investigation or {}).get("angles") or []
    return [a for a in angles if any((e or {}).get("url") for e in (a.get("evidence") or []))]


def _image_rel(article: Article) -> str:
    return "images/" + Path(article.image.path).name if article.image else ""


def _final_scores(article: Article) -> dict[str, int] | None:
    rounds = article.review.validation_rounds if article.review else []
    if not rounds:
        return None
    last = rounds[-1]
    ruling = last.judge_2_recheck or last.judge_2 or last.judge_1
    return ruling.get("scores") if isinstance(ruling, dict) else None


def _review_summary(article: Article) -> dict[str, Any]:
    rounds = article.review.validation_rounds if article.review else []
    last = rounds[-1] if rounds else None
    return {
        "ranking": article.review.ranking if article.review else [],
        "rounds": len(rounds),
        "judge_1": (last.judge_1.get("reason", "") if last else ""),
        "judge_2": ((last.judge_2_recheck or last.judge_2).get("reason", "") if last and (last.judge_2 or last.judge_2_recheck) else ""),
        # Older records kept revisions between rounds only as a version bump.
        "revisions": sum(len(r.revisions) for r in rounds) or max(0, article.version - 1),
        "scores": _final_scores(article),
        "decision": article.review.final_decision if article.review else "",
    }


def build_site(settings: Settings, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "articles").mkdir(parents=True)
    (out_dir / "images").mkdir(parents=True)
    (out_dir / "cards").mkdir(parents=True)

    env = _env(settings)
    articles = load_articles(settings)
    site = {
        "name": settings.site_name,
        "tagline": settings.get("site.tagline", ""),
        "url": settings.site_url,
        "language": settings.language,
        "publisher": settings.get("site.publisher", settings.site_name),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "repo_url": "https://github.com/inquisitive013/nepal-news-rss",
        # The money and trust layer. Every value is empty until the publisher fills it in settings.yaml.
        "contact_email": str(settings.get("site.contact_email", "") or "").strip(),
        "google_site_verification": str(settings.get("site.google_site_verification", "") or "").strip(),
        "facebook_followers": str(settings.get("site.facebook_followers", "") or "").strip(),
        "newsletter_url": str(settings.get("newsletter.signup_url", "") or "").strip(),
        "newsletter_embed": str(settings.get("newsletter.embed_html", "") or "").strip(),
        "members_url": str(settings.get("newsletter.members_url", "") or "").strip(),
        "adsense_client": str(settings.get("ads.adsense_client", "") or "").strip(),
    }

    def render(template: str, dest: Path, **ctx: Any) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(site=site, **ctx), encoding="utf-8")

    # Images live beside the data so the archive survives rebuilds.
    for article in articles:
        if article.image:
            src = settings.root / article.image.path
            if src.exists():
                shutil.copy2(src, out_dir / "images" / src.name)
            else:
                log.warning("image missing for %s: %s", article.id, article.image.path)

    # The Facebook card for every recent article with a photo. Rendered here, never stored in git.
    for article in articles[:CARD_LIMIT]:
        if article.image and (settings.root / article.image.path).exists():
            try:
                graphic.render_card(settings, article, out_dir / "cards" / graphic.card_name(article))
            except Exception as exc:  # noqa: BLE001 - a card is never worth a failed build
                log.warning("card failed for %s: %s", article.id, exc)

    cards = [
        {
            "article": a,
            "url": f"articles/{a.slug}/",
            "image": _image_rel(a),
            "card": f"cards/{graphic.card_name(a)}" if (a.image and (out_dir / "cards" / graphic.card_name(a)).exists()) else "",
            "angles": evidenced_angles(a),
        }
        for a in articles
    ]
    investigations = [c for c in cards if c["angles"]]

    render("index.html", out_dir / "index.html", root="./", cards=cards[:INDEX_LIMIT], total=len(articles), investigations=investigations)
    render("archive.html", out_dir / "archive.html", root="./", cards=cards)
    render("investigations.html", out_dir / "investigations.html", root="./", investigations=investigations)
    render("about.html", out_dir / "about.html", root="./", settings_raw=settings.raw, source_names=[s["name"] for s in settings.sources])
    render("standards.html", out_dir / "standards.html", root="./")
    render("sponsor.html", out_dir / "sponsor.html", root="./")
    render("newsletter.html", out_dir / "newsletter.html", root="./")
    render("privacy.html", out_dir / "privacy.html", root="./")
    for a in articles:
        render(
            "article.html",
            out_dir / "articles" / a.slug / "index.html",
            root="../../",
            article=a,
            image=_image_rel(a),
            body_html=render_markdown(a.body_markdown),
            review=_review_summary(a),
            canonical=f"{site['url']}/articles/{a.slug}/",
            og_image=(f"{site['url']}/{_image_rel(a)}" if a.image else ""),
        )
    (out_dir / "rss.xml").write_text(env.get_template("rss.xml").render(site=site, articles=articles[:50], image_size=_image_sizes(settings, articles[:50])), encoding="utf-8")
    (out_dir / "sitemap.xml").write_text(env.get_template("sitemap.xml").render(site=site, articles=articles), encoding="utf-8")
    # Google News reads only the last two days.
    fresh = [a for a in articles if _parse_iso(a.published_at or "1970-01-01T00:00:00+00:00") >= datetime.now(timezone.utc) - timedelta(hours=48)]
    (out_dir / "news-sitemap.xml").write_text(env.get_template("news-sitemap.xml").render(site=site, articles=fresh), encoding="utf-8")
    (out_dir / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {site['url']}/sitemap.xml\nSitemap: {site['url']}/news-sitemap.xml\n", encoding="utf-8")
    if site["adsense_client"]:
        # AdSense refuses to serve until this file names the publisher. The last field is Google's own seller id.
        (out_dir / "ads.txt").write_text(f"google.com, {site['adsense_client'].removeprefix('ca-')}, DIRECT, f08c47fec0942fa0\n", encoding="utf-8")
    shutil.copy2(STATIC / "style.css", out_dir / "style.css")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    if settings.custom_domain:
        # GitHub Pages reads this when the site is published from a branch; with Actions the
        # domain is set in the repository's Pages settings, and the file does no harm.
        (out_dir / "CNAME").write_text(settings.custom_domain + "\n", encoding="utf-8")
    log.info("site built at %s with %d articles", out_dir, len(articles))
    return out_dir


def _image_sizes(settings: Settings, articles: list[Article]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for a in articles:
        if a.image:
            p = settings.root / a.image.path
            if p.exists():
                sizes[a.id] = p.stat().st_size
    return sizes


def copy_root_rss(settings: Settings, out_dir: Path) -> Path:
    """Keep a copy of the feed at the repository root for automations that read the raw file."""
    src = Path(out_dir) / "rss.xml"
    dest = settings.root / "rss.xml"
    shutil.copy2(src, dest)
    return dest
