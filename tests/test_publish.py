import dataclasses
import re
import xml.etree.ElementTree as ET

from newsroom import publish
from newsroom.config import load_settings
from newsroom.models import (
    Article,
    ImageAsset,
    ImageCredit,
    ReviewRecord,
    ValidationRound,
)


def _settings(tmp_path):
    return dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")


def _article(settings, slug="rain-story", headline="Heavy rain moves 140 households in Kathmandu"):
    (settings.data_dir / "images").mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (1600, 900), (10, 20, 30)).save(settings.root / f"data/images/2026-09-26-{slug}.jpg")
    return Article(
        id=f"2026-09-26-{slug}",
        slug=slug,
        story_id="s_1",
        headline=headline,
        dek="Police moved families to schools overnight, officials say.",
        body_markdown="Police moved **140 households**.\n\n## Why it matters\n\nRivers rose fast.<script>alert(1)</script>",
        language="en",
        key_facts=[{"fact": "140 households moved", "source_url": "https://kathmandupost.com/x"}],
        sources=[{"name": "The Kathmandu Post", "url": "https://kathmandupost.com/x", "used_for": "primary report"}],
        tags=["disaster", "kathmandu"],
        social_hook="140 households moved overnight.",
        take="Officials confirm 140 households moved. Nobody has said who signed off on the late warning.",
        image=ImageAsset(path=f"data/images/2026-09-26-{slug}.jpg", alt="Bagmati river", width=1600, height=900, credit=ImageCredit(kind="found", title="Bagmati", author="Photographer", source="Wikimedia Commons", source_url="https://commons.wikimedia.org/wiki/File:X.jpg", license="CC BY-SA 4.0", license_url="https://creativecommons.org/licenses/by-sa/4.0")),
        review=ReviewRecord(ranking=[{"judge": "ranking_judge_2", "rank": 1, "score": 88, "reason": "broad impact"}], validation_rounds=[ValidationRound(round=1, judge_1={"decision": "approve", "reason": "ok", "scores": {"accuracy": 90, "relevance": 88, "defensibility": 91, "virality": 72}}, judge_2={"decision": "approve", "reason": "concur", "scores": {"accuracy": 91, "relevance": 88, "defensibility": 91, "virality": 74}})], final_decision="approved", final_reason="Judge 2: concur"),
        run_date="2026-09-26",
        published_at="2026-09-26T01:30:00+00:00",
    )


def test_render_markdown_sanitises():
    html = publish.render_markdown("Hello **world**<script>alert(1)</script> <a href=\"javascript:alert(1)\" onclick=\"x()\">x</a>")
    assert "<strong>world</strong>" in html
    assert "<script" not in html and "onclick" not in html and "javascript:" not in html


def test_save_and_load_roundtrip(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings)
    publish.save_article(settings, art)
    loaded = publish.load_articles(settings)
    assert len(loaded) == 1
    a = loaded[0]
    assert a.headline == art.headline
    assert a.image.credit.author == "Photographer"
    assert a.review.validation_rounds[0].judge_2["decision"] == "approve"


def test_build_site(tmp_path):
    settings = _settings(tmp_path)
    publish.save_article(settings, _article(settings))
    publish.save_article(settings, _article(settings, slug="petrol", headline="Petrol drops Rs 5 a litre"))
    out = publish.build_site(settings, tmp_path / "site")
    index = (out / "index.html").read_text()
    assert "Heavy rain moves 140 households" in index and "Petrol drops Rs 5" in index
    page = (out / "articles" / "rain-story" / "index.html").read_text()
    assert "<strong>140 households</strong>" in page
    assert "<script>alert" not in page
    assert "Photographer" in page and "CC BY-SA 4.0" in page
    assert 'property="og:image"' in page and "/images/2026-09-26-rain-story.jpg" in page
    assert "Editorial review record" in page and "ranking judge 2" in page
    assert "wa.me" in page
    assert "Nepal Wire's take." in page and "Nobody has said who signed off on the late warning." in page
    assert (out / "images" / "2026-09-26-rain-story.jpg").exists()
    assert (out / "about.html").exists() and (out / "archive.html").exists() and (out / ".nojekyll").exists()
    rss = ET.parse(out / "rss.xml").getroot()
    items = rss.findall("./channel/item")
    assert len(items) == 2
    assert items[0].find("link").text.endswith("/articles/rain-story/") or items[1].find("link").text.endswith("/articles/rain-story/")
    ET.parse(out / "sitemap.xml")  # well formed
    dest = publish.copy_root_rss(settings, out)
    assert dest == tmp_path / "rss.xml" and dest.exists()


def test_privacy_page(tmp_path):
    settings = _settings(tmp_path)
    publish.save_article(settings, _article(settings))
    out = publish.build_site(settings, tmp_path / "site")
    page = (out / "privacy.html").read_text()
    # Meta's data deletion instructions URL points at this anchor.
    assert 'id="data-deletion"' in page
    # No email address anywhere: not in the markup and not in the visible text.
    email = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    text = re.sub(r"<[^>]+>", "", page.split("<body>", 1)[1])
    assert not email.search(page) and not email.search(text)
    footer = (out / "index.html").read_text().split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert 'href="./privacy.html"' in footer
    article = (out / "articles" / "rain-story" / "index.html").read_text()
    assert 'href="../../privacy.html"' in article
    assert f"{settings.site_url}/privacy.html</loc>" in (out / "sitemap.xml").read_text()


def test_build_empty_site(tmp_path):
    settings = _settings(tmp_path)
    out = publish.build_site(settings, tmp_path / "site")
    assert "first edition" in (out / "index.html").read_text()
    ET.parse(out / "rss.xml")
