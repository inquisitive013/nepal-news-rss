from datetime import datetime, timezone

from newsroom import discovery
from newsroom.config import load_settings
from tests.conftest import FIXTURES

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)
FEEDS = FIXTURES / "feeds"


def test_normalize_url_strips_tracking_and_www():
    a = discovery.normalize_url("https://www.example.com/a/b/?utm_source=rss&x=1#frag")
    b = discovery.normalize_url("https://example.com/a/b?x=1")
    assert a == b == "https://example.com/a/b?x=1"


def test_detect_language():
    assert discovery.detect_language("काठमाडौंमा भारी वर्षा") == "ne"
    assert discovery.detect_language("Heavy rain in Kathmandu") == "en"
    assert discovery.detect_language("") == "unknown"


def test_split_google_title():
    assert discovery.split_google_title("Nepal signs deal - The Himalayan Times") == ("Nepal signs deal", "The Himalayan Times")
    assert discovery.split_google_title("No publisher here") == ("No publisher here", "")


def test_parse_feed_window_and_dates():
    raw = (FEEDS / "the-kathmandu-post.xml").read_bytes()
    cands, total = discovery.parse_feed(raw, "The Kathmandu Post", "kathmandupost.com", "rss", "en", NOW, 24)
    assert total == 5
    titles = [c.title for c in cands]
    assert "Future dated item that should be dropped" not in titles
    assert "Undated item about a Pokhara airport audit" in titles  # kept, flagged undated
    undated = next(c for c in cands if c.title.startswith("Undated"))
    assert undated.published is None
    rain = next(c for c in cands if c.title.startswith("Heavy rain"))
    # 04:05 +0545 is 22:20 UTC the day before
    assert rain.published == "2026-09-25T22:20:00+00:00"
    assert rain.language == "en"


def test_parse_feed_drops_old_items():
    raw = (FEEDS / "onlinekhabar.xml").read_bytes()
    cands, total = discovery.parse_feed(raw, "OnlineKhabar", "onlinekhabar.com", "rss", "ne", NOW, 24)
    assert total == 3
    assert len(cands) == 2
    assert all(c.language == "ne" for c in cands)
    assert all("पुरानो" not in c.title for c in cands)


def test_google_news_parsing_uses_source_publisher():
    raw = (FEEDS / "setopati.gnews.xml").read_bytes()
    cands, _ = discovery.parse_feed(raw, "Setopati", "setopati.com", "google_news_fallback", "ne", NOW, 24)
    assert len(cands) == 1
    c = cands[0]
    assert c.source == "Setopati"
    assert c.source_domain == "setopati.com"
    assert not c.title.endswith("- Setopati")
    assert c.summary == ""  # boilerplate summary removed


def test_dedupe_by_url_and_title():
    raw = (FEEDS / "the-kathmandu-post.xml").read_bytes()
    cands, _ = discovery.parse_feed(raw, "The Kathmandu Post", "kathmandupost.com", "rss", "en", NOW, 24)
    graw = (FEEDS / "gnews-0.xml").read_bytes()
    gcands, _ = discovery.parse_feed(graw, "Google News", "news.google.com", "google_news", "en", NOW, 24)
    merged = discovery.dedupe(cands + gcands)
    titles = [c.title for c in merged]
    assert titles.count("Nepal Oil Corporation cuts petrol price by Rs 5 a litre") == 1
    assert titles.count("Heavy rain puts Bagmati riverside settlements at risk in Kathmandu") == 1
    rain = next(c for c in merged if c.title.startswith("Heavy rain"))
    assert rain.via == "rss"  # native feed wins over Google News copy
    assert "Nepal and India sign 400 MW power trade agreement" in titles
    # dated items first, undated last
    assert merged[-1].published is None


def test_discover_with_fixtures_and_fallback():
    settings = load_settings()
    sources = [s for s in settings.sources if s["name"] in ("OnlineKhabar", "The Kathmandu Post", "Setopati")]
    cands, health = discovery.discover(
        sources,
        settings.google_news,
        window_hours=24,
        max_per_source=20,
        max_total=120,
        fixtures_dir=FEEDS,
        now=NOW,
    )
    by_source = {h.source: h for h in health if h.kind == "rss"}
    assert by_source["OnlineKhabar"].ok and by_source["OnlineKhabar"].in_window == 2
    # Setopati has no native fixture, so the Google News fallback must have been used.
    fallback = [h for h in health if h.source == "Setopati" and h.kind == "google_news_fallback"]
    assert fallback and fallback[0].ok and fallback[0].in_window == 1
    assert any(c.source == "Setopati" for c in cands)
    assert any(c.source == "BBC News नेपाली" for c in cands)
    assert all(c.id.startswith("c_") for c in cands)
    assert len({c.id for c in cands}) == len(cands)


def test_cap_per_source():
    cands = [
        discovery.Candidate(f"c{i}", f"t{i}", "", f"https://x.com/{i}", "X", "x.com", None, "en", "rss") for i in range(5)
    ]
    assert len(discovery.cap_per_source(cands, 2, 10)) == 2
    assert len(discovery.cap_per_source(cands, 10, 3)) == 3


def test_find_feed_links():
    page = """<html><head>
    <link rel="alternate" type="application/rss+xml" title="Feed" href="/feed/">
    <link rel='alternate' type='application/atom+xml' href='https://example.com/atom.xml'>
    <link rel="stylesheet" href="/style.css">
    </head></html>"""
    assert discovery.find_feed_links(page, "https://example.com/") == ["https://example.com/feed/", "https://example.com/atom.xml"]
