import dataclasses
import json

import httpx

from newsroom import publish, social
from newsroom.config import load_settings
from newsroom.models import Article, ImageAsset, ImageCredit

SITE = "https://example.github.io/nepal-news-rss"


def _settings(tmp_path):
    s = load_settings(mock=True)
    raw = dict(s.raw)
    raw["site"] = dict(raw["site"], url=SITE)
    return dataclasses.replace(s, raw=raw, root=tmp_path, data_dir=tmp_path / "data")


def _article(settings, slug="bagmati-floods-140-households", with_image=True):
    art = Article(
        id=f"2026-09-26-{slug}",
        slug=slug,
        story_id="s_1",
        headline="Bagmati floods move 140 households overnight",
        dek="Police say the river rose faster than the warning system.",
        body_markdown="Body.",
        language="en",
        tags=["nepal-floods", "bagmati", "kathmandu", "नेपाल"],
        sources=[{"name": "Kathmandu Post", "url": "https://kathmandupost.com/x", "used_for": "toll"}, {"name": "OnlineKhabar", "url": "https://onlinekhabar.com/y", "used_for": "quotes"}],
        social_hook="140 households moved in one night. Police say the warning came too late.",
        run_date="2026-09-26",
        published_at="2026-09-26T06:30:00+00:00",
    )
    if with_image:
        art.image = ImageAsset(path=f"data/images/2026-09-26-{slug}.jpg", alt="River", width=1600, height=1000, credit=ImageCredit(kind="found", title="Bagmati", author="A", source="Wikimedia Commons", license="CC BY-SA 4.0"))
    return art


ALL_ENV = {
    "X_API_KEY": "k", "X_API_SECRET": "s", "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts",
    "FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "fbtok",
    "INSTAGRAM_USER_ID": "222", "INSTAGRAM_ACCESS_TOKEN": "igtok",
    "THREADS_USER_ID": "333", "THREADS_ACCESS_TOKEN": "thtok",
    "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "@nepalwire",
    "BLUESKY_HANDLE": "nepalwire.bsky.social", "BLUESKY_APP_PASSWORD": "app-pass",
    "MASTODON_BASE_URL": "https://mastodon.social", "MASTODON_ACCESS_TOKEN": "mtok",
}


def test_oauth1_signature_matches_the_published_x_example():
    # The worked example from X's "Creating a signature" documentation.
    header = social.oauth1_authorization(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        params={"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp=1318622958,
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
    assert header.startswith("OAuth ")


def test_hashtags_and_fit():
    assert social.hashtags(["nepal-floods", "bagmati", "nepal floods", "नेपाल"], 8) == ["#NepalFloods", "#Bagmati", "#नेपाल"]
    assert social.hashtags(["a"], 0) == []
    assert social.fit("short", 10) == "short"
    long = "one two three four five six"
    assert social.fit(long, 12).endswith("…") and len(social.fit(long, 12)) <= 12


def test_compose_respects_each_network(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings)
    art.social_hook = "x" * 400  # far too long for X
    x = social.compose("x", art, settings)
    assert f"{SITE}/articles/{art.slug}/" in x
    assert len(x) - len(f"{SITE}/articles/{art.slug}/") + social.X_URL_LENGTH <= 280
    assert x.endswith("#NepalFloods #Bagmati")
    ig = social.compose("instagram", art, settings)
    assert "link in our bio" in ig and "#NepalFloods" in ig and len(ig) <= 2200
    tg = social.compose("telegram", art, settings)
    assert tg.startswith("<b>") and 'href="' in tg and len(tg) <= 1024
    bs = social.compose("bluesky", art, settings)
    assert len(bs) <= 300
    th = social.compose("threads", art, settings)
    assert len(th) <= 500
    fb = social.compose("facebook", art, settings)
    assert fb.startswith(art.headline) and "Full story with links:" in fb and "Sources: " in fb
    art_no_image = _article(settings, with_image=False)
    fb_link = social.compose("facebook", art_no_image, settings)
    assert fb_link.startswith(art_no_image.headline) and f"{SITE}/articles/" in fb_link and "Sources:" not in fb_link


def test_plain_text_strips_markdown():
    md = "## A heading\n\nSome **bold** and *italic* text with a [link](https://x.y).\n\n- one\n- two"
    out = social.plain_text(md)
    assert out.startswith("A heading\n\nSome bold and italic text with a link.")
    # a heading after a paragraph keeps its blank line
    assert social.plain_text("Para one.\n\n## Heading\n\nPara two.") == "Para one.\n\nHeading\n\nPara two."
    assert "• one\n• two" in out and "**" not in out and "](" not in out


def test_configured_networks_need_every_secret(tmp_path):
    settings = _settings(tmp_path)
    assert social.configured_networks(settings, {}) == []
    assert social.configured_networks(settings, {"TELEGRAM_BOT_TOKEN": "a"}) == []
    assert social.configured_networks(settings, {"TELEGRAM_BOT_TOKEN": "a", "TELEGRAM_CHAT_ID": "b", "X_API_KEY": "k"}) == ["telegram"]
    assert social.configured_networks(settings, ALL_ENV) == list(social.NETWORKS)


def _fake_network(calls):
    """A transport that plays every network's API and records the requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(request)
        if url.startswith(SITE):
            if url.endswith(".jpg"):
                return httpx.Response(200, content=b"\xff\xd8jpegbytes", headers={"content-type": "image/jpeg"})
            return httpx.Response(200, text="<html>ok</html>")
        if url == "https://api.x.com/2/tweets":
            assert request.headers["Authorization"].startswith("OAuth ")
            assert json.loads(request.content)["text"]
            return httpx.Response(201, json={"data": {"id": "1001", "text": "t"}})
        if "graph.facebook.com" in url and url.endswith("/111/photos"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["url"].endswith(".jpg") and body["access_token"] == "fbtok"
            assert "Full story with links: " + SITE in body["caption"] and "Sources:" in body["caption"]
            return httpx.Response(200, json={"id": "90", "post_id": "111_2002"})
        if "graph.facebook.com" in url and url.endswith("/111/feed"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["link"].startswith(SITE) and body["access_token"] == "fbtok"
            return httpx.Response(200, json={"id": "111_2002"})
        if "graph.facebook.com" in url and url.endswith("/222/media"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["image_url"].endswith(".jpg") and "link in our bio" in body["caption"]
            return httpx.Response(200, json={"id": "c3003"})
        if "graph.facebook.com" in url and "/c3003" in url:
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if "graph.facebook.com" in url and url.endswith("/222/media_publish"):
            return httpx.Response(200, json={"id": "3003"})
        if "graph.facebook.com" in url and "/3003" in url:
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/abc/"})
        if url.startswith("https://graph.threads.net/v1.0/333/threads?") or url == "https://graph.threads.net/v1.0/333/threads":
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["media_type"] == "IMAGE" and body["image_url"].endswith(".jpg")
            return httpx.Response(200, json={"id": "c4004"})
        if url.startswith("https://graph.threads.net/v1.0/c4004"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if url.startswith("https://graph.threads.net/v1.0/333/threads_publish"):
            return httpx.Response(200, json={"id": "4004"})
        if url.startswith("https://graph.threads.net/v1.0/4004"):
            return httpx.Response(200, json={"permalink": "https://www.threads.net/@nepalwire/post/xyz"})
        if url.startswith("https://api.telegram.org/bot123:abc/sendPhoto"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["parse_mode"] == "HTML" and body["photo"].endswith(".jpg")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 55, "chat": {"username": "nepalwire"}}})
        if url.endswith("/xrpc/com.atproto.server.createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:abc", "handle": "nepalwire.bsky.social"})
        if url.endswith("/xrpc/com.atproto.repo.uploadBlob"):
            assert request.headers["Authorization"] == "Bearer jwt"
            return httpx.Response(200, json={"blob": {"$type": "blob", "ref": {"$link": "bafy"}, "mimeType": "image/jpeg", "size": 12}})
        if url.endswith("/xrpc/com.atproto.repo.createRecord"):
            body = json.loads(request.content)
            rec = body["record"]
            assert body["repo"] == "did:plc:abc" and rec["embed"]["external"]["thumb"]["ref"]["$link"] == "bafy"
            facet = rec["facets"][0]
            start, end = facet["index"]["byteStart"], facet["index"]["byteEnd"]
            assert rec["text"].encode("utf-8")[start:end].decode() == facet["features"][0]["uri"]
            return httpx.Response(200, json={"uri": "at://did:plc:abc/app.bsky.feed.post/rk1", "cid": "c"})
        if url == "https://mastodon.social/api/v1/statuses":
            assert request.headers["Authorization"] == "Bearer mtok"
            return httpx.Response(200, json={"id": "77", "url": "https://mastodon.social/@nepalwire/77"})
        return httpx.Response(404, json={"error": f"unexpected {url}"})

    return httpx.MockTransport(handler)


def _save_run(settings, art):
    publish.save_article(settings, art)
    runs = settings.data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-09-26.json").write_text(json.dumps({"run_date": "2026-09-26", "published": [art.id]}))


def test_post_articles_hits_every_network_and_records_it(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings)
    _save_run(settings, art)
    calls = []
    client = httpx.Client(transport=_fake_network(calls))
    records = social.post_articles(settings, ALL_ENV, client=client, sleep=lambda s: None, max_age_hours=10**6)
    assert len(records) == 1
    by_net = {p.network: p for p in records[0].posts}
    assert set(by_net) == set(social.NETWORKS)
    assert all(p.status == "posted" for p in by_net.values()), {k: v.error for k, v in by_net.items() if v.status != "posted"}
    assert by_net["x"].url == "https://x.com/i/web/status/1001"
    assert by_net["facebook"].url == "https://www.facebook.com/111_2002"
    assert by_net["instagram"].url == "https://www.instagram.com/p/abc/"
    assert by_net["threads"].url.startswith("https://www.threads.net/")
    assert by_net["telegram"].url == "https://t.me/nepalwire/55"
    assert by_net["bluesky"].url == "https://bsky.app/profile/nepalwire.bsky.social/post/rk1"
    assert by_net["mastodon"].url == "https://mastodon.social/@nepalwire/77"
    # the article page was checked before anything was posted
    assert str(calls[0].url) == f"{SITE}/articles/{art.slug}/"
    saved = json.loads(social.record_path(settings, art.id).read_text())
    assert len(saved["posts"]) == 7

    # a second run posts nothing again
    before = len(calls)
    records2 = social.post_articles(settings, ALL_ENV, client=client, sleep=lambda s: None, max_age_hours=10**6, wait_seconds=0)
    assert before == len(calls)
    assert all(p.status == "posted" for p in records2[0].posts) and len(records2[0].posts) == 7


def test_failures_are_recorded_not_raised(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings, with_image=False)
    _save_run(settings, art)

    def handler(request):
        if str(request.url).startswith(SITE):
            return httpx.Response(200, text="ok")
        return httpx.Response(403, json={"error": {"message": "token expired", "code": 190}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    env = {k: v for k, v in ALL_ENV.items() if k.startswith(("FACEBOOK", "INSTAGRAM"))}
    records = social.post_articles(settings, env, client=client, sleep=lambda s: None, max_age_hours=10**6)
    posts = {p.network: p for p in records[0].posts}
    assert posts["facebook"].status == "failed" and "token expired" in posts["facebook"].error
    assert posts["instagram"].status == "failed" and "needs a picture" in posts["instagram"].error


def test_dry_run_and_age_window(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings)
    _save_run(settings, art)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    # published in 2026; anything with a small age window is left alone
    assert social.post_articles(settings, ALL_ENV, client=client, max_age_hours=0.0001) == []
    records = social.post_articles(settings, ALL_ENV, client=client, dry_run=True, max_age_hours=10**6)
    assert records and all(p.status == "skipped" and p.text for p in records[0].posts)
    assert not social.record_path(settings, art.id).exists()


def test_check_networks_reports_missing_and_broken(tmp_path):
    settings = _settings(tmp_path)

    def handler(request):
        url = str(request.url)
        if "getMe" in url:
            return httpx.Response(200, json={"ok": True, "result": {"username": "nepalwire_bot"}})
        if "getChat" in url:
            return httpx.Response(200, json={"ok": True, "result": {"title": "Nepal Wire"}})
        return httpx.Response(401, json={"errors": [{"message": "Unauthorized"}]})

    env = {k: v for k, v in ALL_ENV.items() if k.startswith(("TELEGRAM", "X_"))}
    rows = {r["network"]: r for r in social.check_networks(settings, env, client=httpx.Client(transport=httpx.MockTransport(handler)))}
    assert rows["telegram"]["ok"] == "yes" and "Nepal Wire" in rows["telegram"]["account"]
    assert rows["x"]["ok"] == "no" and "401" in rows["x"]["note"]
    assert rows["facebook"]["configured"] == "no" and "FACEBOOK_PAGE_ID" in rows["facebook"]["note"]
