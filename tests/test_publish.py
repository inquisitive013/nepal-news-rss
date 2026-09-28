import copy
import dataclasses
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

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
    assert (out / "cards" / "2026-09-26-rain-story.jpg").exists()  # the Facebook card, rendered at build time
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


def test_custom_domain_sets_every_address_and_writes_the_cname(tmp_path, monkeypatch):
    monkeypatch.delenv("SITE_URL", raising=False)
    monkeypatch.setenv("PAGES_URL", "https://someone.github.io/some-repo")
    settings = _settings(tmp_path)
    assert settings.site_url == "https://someone.github.io/some-repo"  # the workflow's default wins over settings.yaml
    raw = copy.deepcopy(settings.raw)
    raw["site"]["custom_domain"] = "https://NepalWire.com/"
    settings = dataclasses.replace(settings, raw=raw)
    assert settings.custom_domain == "nepalwire.com" and settings.site_url == "https://nepalwire.com"
    publish.save_article(settings, _article(settings))
    out = publish.build_site(settings, tmp_path / "site")
    assert (out / "CNAME").read_text() == "nepalwire.com\n"
    assert "https://nepalwire.com/articles/rain-story/" in (out / "sitemap.xml").read_text()
    assert 'href="https://nepalwire.com/articles/rain-story/"' in (out / "articles" / "rain-story" / "index.html").read_text()
    monkeypatch.setenv("SITE_URL", "https://staging.example.org")
    assert settings.site_url == "https://staging.example.org"  # a repository variable overrides everything


def test_the_money_and_trust_layer_builds(tmp_path):
    settings = _settings(tmp_path)
    raw = copy.deepcopy(settings.raw)
    raw["site"].update({"contact_email": "desk@example.org", "google_site_verification": "tok-123", "facebook_followers": "10,300 followers as of September 2026"})
    raw["newsletter"] = {"signup_url": "https://example.beehiiv.com/subscribe", "embed_html": "", "members_url": "https://example.beehiiv.com/upgrade"}
    raw["ads"] = {"adsense_client": "ca-pub-1234567890123456"}
    settings = dataclasses.replace(settings, raw=raw)
    art = _article(settings)
    art.take = "Nobody has said who signed off on the late warning."
    art.investigation = {
        "summary": "s",
        "angles": [
            {"kind": "record", "claim": "The district office promised embankment repairs in 2024.", "evidence": [{"url": "https://example.org/minutes", "source": "District minutes", "fact": "f"}]},
            {"kind": "numbers", "claim": "No evidence here.", "evidence": []},
        ],
        "unanswered": [{"question": "Who approved the delay?", "who_could_answer": "The CDO"}],
    }
    art.published_at = datetime.now(timezone.utc).isoformat()  # the news sitemap keeps 48 hours
    publish.save_article(settings, art)
    publish.save_article(settings, _article(settings, slug="petrol", headline="Petrol drops Rs 5 a litre"))
    out = publish.build_site(settings, tmp_path / "site")
    index = (out / "index.html").read_text()
    assert 'name="google-site-verification" content="tok-123"' in index
    assert "adsbygoogle.js?client=ca-pub-1234567890123456" in index
    assert "cards/2026-09-26-rain-story.jpg" in index and "Nobody has said who signed off" in index
    assert "https://example.beehiiv.com/subscribe" in index and "https://example.beehiiv.com/upgrade" in index
    assert "The district office promised embankment repairs" in index  # the lead's missed angle
    inv = (out / "investigations.html").read_text()
    assert "The district office promised embankment repairs" in inv and "No evidence here." not in inv
    assert "Who approved the delay?" in inv and "Petrol drops" not in inv
    assert "desk@example.org" in (out / "sponsor.html").read_text() and "10,300 followers" in (out / "sponsor.html").read_text()
    assert "Sponsors never see a story before publication" in (out / "standards.html").read_text()
    assert "Subscribe free" in (out / "newsletter.html").read_text()
    page = (out / "articles" / "rain-story" / "index.html").read_text()
    assert page.count('class="adsbygoogle"') == 2 and "Get the daily wire" in page
    assert (out / "ads.txt").read_text() == "google.com, pub-1234567890123456, DIRECT, f08c47fec0942fa0\n"
    news = (out / "news-sitemap.xml").read_text()
    assert "news:publication_date" in news and "/articles/rain-story/" in news
    assert "news-sitemap.xml" in (out / "robots.txt").read_text()
    assert "/investigations.html" in (out / "sitemap.xml").read_text()
    # nothing configured: no ads, no verification tag, no ads.txt, the sign up box still renders a fallback
    bare = publish.build_site(_settings(tmp_path), tmp_path / "site2")
    bare_index = (bare / "index.html").read_text()
    assert "adsbygoogle" not in bare_index and "google-site-verification" not in bare_index and not (bare / "ads.txt").exists()
    assert "Sign up opens soon" in bare_index


NEPALI = {
    "headline": "काठमाडौंमा भारी वर्षाले १४० घरधुरी सारियो",
    "dek": "प्रहरीले परिवारलाई रातारात विद्यालयमा सारे।",
    "take": "अधिकारीहरूले १४० घरधुरी सारिएको पुष्टि गरे। ढिलो चेतावनीमा कसले हस्ताक्षर गर्‍यो, कसैले भनेको छैन।",
    "body_markdown": "प्रहरीले **१४० घरधुरी** सारे।\n\n## किन महत्त्वपूर्ण छ\n\nनदी छिटो बढ्यो।",
    "image_headline": "१४० घरधुरी रातारात सारियो",
    "social_hook": "१४० घरधुरी एकै रातमा सारिए।",
    "caption": {"hook": "ह", "body": "श", "trigger": "ट"},
    "checked": True,
    "judge": "Faithful and natural.",
    "problems_fixed": 0,
}


def test_the_nepali_edition_builds_beside_the_english(tmp_path):
    settings = _settings(tmp_path)
    site_url = settings.site_url
    art = _article(settings)
    art.nepali = dict(NEPALI)
    art.published_at = datetime.now(timezone.utc).isoformat()
    publish.save_article(settings, art)
    publish.save_article(settings, _article(settings, slug="petrol", headline="Petrol drops Rs 5 a litre"))
    out = publish.build_site(settings, tmp_path / "site")

    ne_index = (out / "ne" / "index.html").read_text()
    assert '<html lang="ne">' in ne_index and "काठमाडौंमा भारी वर्षाले" in ne_index and "ne/articles/rain-story/" in ne_index
    assert "नेपाल वायरको टिप्पणी" in ne_index and "ढिलो चेतावनीमा" in ne_index
    # a story without a Nepali edition still shows on the Nepali front, in English, pointing at its English page
    assert "Petrol drops Rs 5 a litre" in ne_index and "articles/petrol/" in ne_index and "ne/articles/petrol/" not in ne_index
    assert "· English" in ne_index  # the marker on the story that has no Nepali edition
    assert "logo.png" in ne_index and "favicon.png" in ne_index and (out / "logo.png").exists() and (out / "favicon.png").exists() and (out / "apple-touch-icon.png").exists()
    assert not (out / "logo-mark.png").exists()

    ne_page = (out / "ne" / "articles" / "rain-story" / "index.html").read_text()
    assert "<h1>काठमाडौंमा भारी वर्षाले १४० घरधुरी सारियो</h1>" in ne_page and "<strong>१४० घरधुरी</strong>" in ne_page
    assert "नेपाल वायरको टिप्पणी" in ne_page and "फाइल तस्बिर" in ne_page and "Photographer" in ne_page
    assert f'hreflang="en" href="{site_url}/articles/rain-story/"' in ne_page
    assert f'hreflang="ne" href="{site_url}/ne/articles/rain-story/"' in ne_page
    assert f'hreflang="x-default" href="{site_url}/articles/rain-story/"' in ne_page
    assert f'<link rel="canonical" href="{site_url}/ne/articles/rain-story/">' in ne_page
    assert '"inLanguage": "ne"' in ne_page and "स्रोतहरू" in ne_page and "The Kathmandu Post" in ne_page
    assert "Key facts" not in ne_page and "Editorial review record" not in ne_page and "अंग्रेजी पृष्ठमा" in ne_page
    # the Nepali is written, not translated, and the site never says otherwise
    assert "अनुवाद" not in ne_page and "अनुवाद" not in ne_index
    assert "translator model" not in (out / "about.html").read_text() and "is a translation of" not in (out / "standards.html").read_text()
    assert "The Nepali is not a translation." in (out / "standards.html").read_text()
    assert 'class="lang" href="../../../articles/rain-story/"' in ne_page  # the switch leads to the English twin

    en_page = (out / "articles" / "rain-story" / "index.html").read_text()
    # both pages show the card, which carries the source line, with the credit under it
    assert 'class="card-figure"' in en_page and 'src="../../cards/2026-09-26-rain-story.jpg"' in en_page and "File photo:" in en_page
    assert 'class="card-figure"' in ne_page and 'src="../../../cards/2026-09-26-rain-story.jpg"' in ne_page and "फाइल तस्बिर" in ne_page
    assert f'hreflang="ne" href="{site_url}/ne/articles/rain-story/"' in en_page and 'hreflang="x-default"' in en_page
    assert 'href="../../ne/articles/rain-story/" lang="ne"' in en_page and "यो समाचार नेपालीमा पढ्नुहोस्" in en_page
    assert 'class="lang" href="../../ne/articles/rain-story/"' in en_page
    petrol = (out / "articles" / "petrol" / "index.html").read_text()
    assert "नेपालीमा पढ्नुहोस्" not in petrol and 'class="lang" href="../../ne/"' in petrol
    assert f'hreflang="ne" href="{site_url}/ne/articles/petrol/"' not in petrol
    assert not (out / "ne" / "articles" / "petrol").exists()
    index = (out / "index.html").read_text()
    assert "नेपालीमा पढ्नुहोस्" in index and 'class="lang" href="./ne/"' in index and f'hreflang="ne" href="{site_url}/ne/"' in index

    feed = (out / "ne" / "rss.xml").read_text()
    assert "<language>ne</language>" in feed and "<title>नेपाल वायर</title>" in feed and "काठमाडौंमा" in feed
    assert "Petrol drops" not in feed and f"{site_url}/ne/articles/rain-story/" in feed and "स्रोतहरू:" in feed
    ET.fromstring(feed.encode("utf-8"))
    en_feed = (out / "rss.xml").read_text()
    assert "<language>en</language>" in en_feed and "काठमाडौंमा" not in en_feed and f"{site_url}/articles/petrol/" in en_feed
    sitemap = (out / "sitemap.xml").read_text()
    assert f"{site_url}/ne/</loc>" in sitemap and f"{site_url}/ne/articles/rain-story/" in sitemap and "/ne/articles/petrol/" not in sitemap
    news = (out / "news-sitemap.xml").read_text()
    assert news.count("<news:language>ne</news:language>") == 1 and "/ne/articles/rain-story/" in news
    assert "काठमाडौंमा भारी वर्षाले" in news


def test_the_nepali_front_page_without_any_nepali_edition(tmp_path):
    settings = _settings(tmp_path)
    publish.save_article(settings, _article(settings))
    out = publish.build_site(settings, tmp_path / "site")
    ne_index = (out / "ne" / "index.html").read_text()
    assert "Heavy rain moves 140 households" in ne_index and "अंग्रेजीमा पढ्नुहोस्" in ne_index
    feed = (out / "ne" / "rss.xml").read_text()
    assert "<item>" not in feed
    ET.fromstring(feed.encode("utf-8"))
    empty = publish.build_site(_settings(tmp_path / "none"), tmp_path / "site2")
    assert "पहिलो संस्करण आउँदैछ" in (empty / "ne" / "index.html").read_text()


def test_repository_links_appear_only_when_configured(tmp_path):
    settings = _settings(tmp_path)
    assert not settings.get("site.repo_url")
    publish.save_article(settings, _article(settings))
    out = publish.build_site(settings, tmp_path / "site")
    pages = {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.html")}
    for name, html in pages.items():
        assert "github.com/inquisitive013" not in html and "/issues" not in html and "/tree/main/data" not in html, name
    assert "Spotted an error? Message the Nepal Wire Facebook Page." in pages["index.html"]
    assert "kept by the newsroom" in pages["articles/rain-story/index.html"]
    assert "Report an error through the Nepal Wire Facebook Page." in pages["standards.html"]

    raw = copy.deepcopy(settings.raw)
    raw["site"].update({"repo_url": "https://github.com/example/newsroom/", "facebook_url": "https://www.facebook.com/nepalwire"})
    out2 = publish.build_site(dataclasses.replace(settings, raw=raw), tmp_path / "site2")
    index = (out2 / "index.html").read_text()
    assert 'href="https://github.com/example/newsroom"' in index and 'href="https://github.com/example/newsroom/issues"' in index
    assert 'href="https://www.facebook.com/nepalwire">Facebook</a>' in index
    assert "/tree/main/data" in (out2 / "articles" / "rain-story" / "index.html").read_text()

    raw["site"].update({"repo_url": "", "contact_email": "desk@example.org"})
    out3 = publish.build_site(dataclasses.replace(settings, raw=raw), tmp_path / "site3")
    assert "Spotted an error? Email <a href=\"mailto:desk@example.org\">desk@example.org</a>." in (out3 / "index.html").read_text()
    assert "Report an error by email to" in (out3 / "standards.html").read_text()


def test_a_dated_update_shows_under_the_headline_in_both_languages(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings)
    art.nepali = dict(NEPALI)
    art.published_at = datetime.now(timezone.utc).isoformat()
    art.updates = [{
        "date": "2026-09-28T16:00:00+00:00",
        "kind": "update",
        "text": "Police later said it was not an arrest, the Statesman reported.",
        "text_ne": "प्रहरीले पछि पक्राउ नभएको बतायो, द स्टेट्स्म्यानले जनाएको छ।",
        "link": "articles/petrol/",
    }]
    publish.save_article(settings, art)
    publish.save_article(settings, _article(settings, slug="petrol", headline="Petrol drops Rs 5 a litre"))
    out = publish.build_site(settings, tmp_path / "site")

    en = (out / "articles" / "rain-story" / "index.html").read_text()
    assert '<aside class="update"><strong>Update, 28 September 2026.</strong> Police later said it was not an arrest, the Statesman reported.' in en
    assert '<a href="../../articles/petrol/">Read the follow-up</a>' in en
    assert en.index('class="update"') < en.index('class="take"')  # the reader meets it before anything else
    ne = (out / "ne" / "articles" / "rain-story" / "index.html").read_text()
    assert '<aside class="update"><strong>अपडेट, २८ सेप्टेम्बर २०२६।</strong> प्रहरीले पछि पक्राउ नभएको बतायो' in ne
    assert '<a href="../../../ne/articles/petrol/">थप पढ्नुहोस्</a>' in ne
    assert 'class="update"' not in (out / "articles" / "petrol" / "index.html").read_text()  # no update, no box
    reloaded = [a for a in publish.load_articles(settings) if a.id == art.id][0]
    assert reloaded.updates == art.updates
