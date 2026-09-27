import dataclasses
import json

import httpx
import pytest

from newsroom import publish, social
from newsroom.config import load_settings
from newsroom.models import Article, ImageAsset, ImageCredit

SITE = "https://example.github.io/nepal-news-rss"


@pytest.fixture(autouse=True)
def _fresh_facebook_token_cache():
    social._PAGE_TOKENS.clear()
    yield


def _settings(tmp_path):
    s = load_settings(mock=True)
    raw = dict(s.raw)
    raw["site"] = dict(raw["site"], url=SITE)
    # Tests start with nothing paused, whatever the live config pauses today.
    raw["social"] = dict(raw.get("social") or {}, paused=[])
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
    "FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "EAABfaketoken_1234567890abcdefghijklmnop",
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
        if "graph.facebook.com" in url and "/me?" in url:
            return httpx.Response(200, json={"id": "111"})
        if "graph.facebook.com" in url and url.endswith("/111/photos"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["url"].endswith(".jpg") and body["access_token"] == "EAABfaketoken_1234567890abcdefghijklmnop"
            assert "Full story with links: " + SITE in body["caption"] and "Sources:" in body["caption"]
            return httpx.Response(200, json={"id": "90", "post_id": "111_2002"})
        if "graph.facebook.com" in url and url.endswith("/111/feed"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["link"].startswith(SITE) and body["access_token"] == "EAABfaketoken_1234567890abcdefghijklmnop"
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


def test_articles_to_post_uses_the_age_window_not_the_run_record(tmp_path):
    settings = _settings(tmp_path)
    first = _article(settings, slug="first-story")
    second = _article(settings, slug="second-story")
    second.run_date = "2026-09-27"
    second.id = "2026-09-27-second-story"
    publish.save_article(settings, first)
    publish.save_article(settings, second)
    both = {a.id for a in social.articles_to_post(settings, max_age_hours=10**6)}
    assert both == {first.id, second.id}
    dated = social.articles_to_post(settings, run_date="2026-09-27", max_age_hours=10**6)
    assert [a.id for a in dated] == [second.id]
    assert social.articles_to_post(settings, run_date="2026-01-01", max_age_hours=10**6) == []
    # too old for the window, but named outright
    assert social.articles_to_post(settings, max_age_hours=0.0001) == []
    assert [a.id for a in social.articles_to_post(settings, max_age_hours=0.0001, article_ids=[first.id])] == [first.id]


def _ranked(settings, slug, run_date, rank):
    art = _article(settings, slug=slug)
    art.run_date = run_date
    art.id = f"{run_date}-{slug}"
    art.review.ranking = [{"judge": "ranking_judge_1", "rank": 3, "score": 70, "reason": ""}, {"judge": "ranking_judge_2", "rank": rank, "score": 70, "reason": ""}]
    return art


def test_facebook_slots_spread_the_day_best_story_first(tmp_path):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    settings = _settings(tmp_path)  # slots from settings.yaml: now, 12:30, 18:30 Kathmandu time
    a1 = _ranked(settings, "top", "2026-09-27", 1)
    a2 = _ranked(settings, "second", "2026-09-27", 2)
    a3 = _ranked(settings, "third", "2026-09-27", 3)
    old = _ranked(settings, "yesterday", "2026-09-26", 1)
    now = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)  # 06:45 in Kathmandu
    plan = social.plan_facebook_slots([old, a3, a1, a2], settings, now)
    ktm = ZoneInfo("Asia/Kathmandu")
    assert plan[a1.id] is None  # the best story goes out at once
    assert datetime.fromtimestamp(plan[a2.id], ktm).strftime("%H:%M") == "12:30"
    assert datetime.fromtimestamp(plan[a3.id], ktm).strftime("%H:%M") == "18:30"
    assert plan[old.id] is None  # beyond the slots: at once
    # late in the day the clock slots are past, so everything posts at once
    evening = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)  # 18:45 in Kathmandu
    assert all(v is None for v in social.plan_facebook_slots([a1, a2, a3], settings, evening).values())
    # a slot under ten minutes away posts at once too
    close = datetime(2026, 9, 27, 6, 42, tzinfo=timezone.utc)  # 12:27 in Kathmandu
    assert social.plan_facebook_slots([a1, a2], settings, close)[a2.id] is None
    assert social.article_rank(_article(settings)) == 99


def test_post_articles_schedules_the_second_facebook_post(tmp_path):
    from datetime import datetime, timezone

    settings = _settings(tmp_path)
    a1 = _ranked(settings, "top", "2026-09-27", 1)
    a2 = _ranked(settings, "second", "2026-09-27", 2)
    for a in (a1, a2):
        publish.save_article(settings, a)
    seen = {}

    def handler(request):
        url = str(request.url)
        if url.startswith(SITE):
            return httpx.Response(200, text="ok")
        if "/me?" in url:
            return httpx.Response(200, json={"id": "111"})
        if url.endswith("/111/photos"):
            body = dict(httpx.QueryParams(request.content.decode()))
            seen[body["url"]] = body  # the picture URL carries the slug, so one entry per article
            return httpx.Response(200, json={"id": "9", "post_id": "111_" + str(len(seen))})
        return httpx.Response(404, json={"error": "unexpected " + url})

    env = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "EAABfaketoken_1234567890abcdefghijklmnop"}
    now = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)
    records = {r.article_id: r for r in social.post_articles(settings, env, client=httpx.Client(transport=httpx.MockTransport(handler)), max_age_hours=10**6, now=now, sleep=lambda s: None)}
    first, second = records[a1.id].posts[0], records[a2.id].posts[0]
    assert first.status == "posted" and first.scheduled_for == ""
    assert second.status == "posted" and second.scheduled_for.startswith("2026-09-27T12:30")
    bodies = list(seen.values())
    scheduled = [b for b in bodies if "scheduled_publish_time" in b]
    assert len(scheduled) == 1 and scheduled[0]["published"] == "false"
    # a rerun neither reposts nor reshuffles
    again = social.post_articles(settings, env, client=httpx.Client(transport=httpx.MockTransport(handler)), max_age_hours=10**6, now=now, sleep=lambda s: None, wait_seconds=0)
    assert len(seen) == 2 and all(p.status == "posted" for r in again for p in r.posts)


USER_TOKEN = "EAABfaketoken_1234567890abcdefghijklmnop"
PAGE_TOKEN = "EAAPAGEtoken_1234567890abcdefghijklmnopq"


def _facebook_check(me_id, *, page_node=True, token_is_page=False, accounts_has_page=True):
    def handler(request):
        url = str(request.url)
        fields = request.url.params.get("fields", "")
        token = request.url.params.get("access_token", "")
        if "/me/accounts" in url:
            if token_is_page:
                return httpx.Response(400, json={"error": {"message": "(#100) Tried accessing nonexisting field (accounts) on node type (Page)", "code": 100}})
            data = [{"id": "111", "access_token": PAGE_TOKEN}] if accounts_has_page else [{"id": "555", "access_token": "EAAOTHERpage_1234567890abcdefghijklmnop"}]
            return httpx.Response(200, json={"data": data, "paging": {}})
        if "/me?" in url:
            return httpx.Response(200, json={"id": me_id, "name": "Ruby D. Parajuli"})
        # everything about the Page itself must be asked with the Page's token
        if token != (USER_TOKEN if me_id == "111" else PAGE_TOKEN):
            return httpx.Response(400, json={"error": {"message": "wrong token for a Page call", "code": 190}})
        if "/111?" in url and fields == "name,category":
            if not page_node:
                return httpx.Response(400, json={"error": {"message": "(#100) Tried accessing nonexisting field (category) on node type (User)", "code": 100}})
            return httpx.Response(200, json={"id": "111", "name": "Ruby D. Parajuli", "category": "Public figure"})
        if "/111?" in url and fields == "followers_count,fan_count":
            return httpx.Response(200, json={"id": "111", "followers_count": 10423, "fan_count": 9870})
        return httpx.Response(404, json={"error": {"message": f"unexpected {url}"}})

    env = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": USER_TOKEN}
    return social.check_facebook(httpx.Client(transport=httpx.MockTransport(handler)), env)


def test_facebook_check_confirms_a_page_with_its_followers():
    assert _facebook_check("111") == "Ruby D. Parajuli (Page, Public figure, 10,423 followers)"


def test_facebook_check_derives_the_page_token_from_a_user_token():
    label = _facebook_check("999")
    assert label.startswith("Ruby D. Parajuli (Page, Public figure, 10,423 followers)")
    assert "derived from your user token" in label and "60 days" in label
    assert "999" not in label and PAGE_TOKEN not in label


def test_facebook_check_user_token_that_does_not_manage_the_page():
    with pytest.raises(social.SocialError, match="none of the Pages it manages") as err:
        _facebook_check("999", accounts_has_page=False)
    # public logs: the message must not carry the token owner's name or any ID
    assert "Ruby" not in str(err.value) and "999" not in str(err.value) and "555" not in str(err.value) and "111" not in str(err.value)


def test_posting_with_a_user_token_uses_the_derived_page_token():
    posted = {}

    def handler(request):
        url = str(request.url)
        if "/me/accounts" in url:
            return httpx.Response(200, json={"data": [{"id": "111", "access_token": PAGE_TOKEN}]})
        if "/me?" in url:
            return httpx.Response(200, json={"id": "999"})
        if url.endswith("/111/photos"):
            posted.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"id": "9", "post_id": "111_77"})
        return httpx.Response(404, json={"error": {"message": f"unexpected {url}"}})

    env = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": USER_TOKEN}
    post_id, link = social.post_facebook(httpx.Client(transport=httpx.MockTransport(handler)), env, "caption", SITE + "/articles/x/", SITE + "/images/x.jpg")
    assert post_id == "111_77" and link.endswith("/111_77")
    assert posted["access_token"] == PAGE_TOKEN


def test_facebook_check_names_a_page_token_for_another_page():
    with pytest.raises(social.SocialError, match="Page token, but for a different Page") as err:
        _facebook_check("999", token_is_page=True)
    assert "Ruby" not in str(err.value) and "999" not in str(err.value) and "111" not in str(err.value)


def test_facebook_check_rejects_a_personal_profile():
    import pytest

    with pytest.raises(social.SocialError, match="personal profile, not a Page"):
        _facebook_check("111", page_node=False)


def test_paused_networks_are_checked_but_never_posted(tmp_path):
    settings = _settings(tmp_path)
    raw = dict(settings.raw)
    raw["social"] = dict(raw.get("social") or {}, paused=["facebook"])
    settings = dataclasses.replace(settings, raw=raw)
    env = {k: v for k, v in ALL_ENV.items() if k.startswith(("FACEBOOK", "TELEGRAM"))}
    assert social.configured_networks(settings, env) == ["telegram"]
    art = _article(settings)
    _save_run(settings, art)
    calls = []
    records = social.post_articles(settings, env, client=httpx.Client(transport=_fake_network(calls)), sleep=lambda s: None, max_age_hours=10**6)
    assert [p.network for p in records[0].posts] == ["telegram"]
    assert not any("graph.facebook.com" in str(r.url) for r in calls)

    def handler(request):
        url = str(request.url)
        if "/me?" in url:
            return httpx.Response(200, json={"id": "111", "name": "Nepal Wire"})
        if "fields=name%2Ccategory" in url or "fields=name,category" in url:
            return httpx.Response(200, json={"id": "111", "name": "Nepal Wire", "category": "News & media website"})
        return httpx.Response(200, json={"id": "111", "followers_count": 10000})

    rows = {r["network"]: r for r in social.check_networks(settings, {k: v for k, v in env.items() if k.startswith("FACEBOOK")}, client=httpx.Client(transport=httpx.MockTransport(handler)))}
    assert rows["facebook"]["ok"] == "yes" and rows["facebook"]["note"] == "paused in settings, not posting"


def test_secrets_survive_sloppy_pasting_and_bad_shapes_are_named():
    assert social.secret({"FACEBOOK_PAGE_TOKEN": '  "EAABsbCS1iHgBO7ZCZCZBqZBw_abcdefghijklmnop"\n'}, "FACEBOOK_PAGE_TOKEN") == "EAABsbCS1iHgBO7ZCZCZBqZBw_abcdefghijklmnop"
    assert social.secret({"FACEBOOK_PAGE_ID": "id: 123456789 "}, "FACEBOOK_PAGE_ID") == "123456789"
    assert social.secret({"FACEBOOK_PAGE_TOKEN": "access_token=EAABtokenvalue_1234567890"}, "FACEBOOK_PAGE_TOKEN") == "EAABtokenvalue_1234567890"
    # a token copied across a wrapped line carries a break in the middle
    assert social.secret({"FACEBOOK_PAGE_TOKEN": "EAABsbCS1iHgBO7\nZCZCZBqZBw_abcd efghijklmnop\r\n"}, "FACEBOOK_PAGE_TOKEN") == "EAABsbCS1iHgBO7ZCZCZBqZBw_abcdefghijklmnop"
    assert social.secret_shape_problem("123", kind="digits") == ""
    assert "digits" in social.secret_shape_problem("12a3", kind="digits")
    assert "short" in social.secret_shape_problem("EAAB", kind="token")
    assert "space" in social.secret_shape_problem("EAAB tokenvalue_1234567890abcdef", kind="token")
    assert "characters" in social.secret_shape_problem("EAAB{tokenvalue_1234567890abcdef}", kind="token")
    assert social.secret_shape_problem("EAABsbCS1iHgBO7ZCZCZBqZBw_abcdefghijklmnop", kind="token") == ""


def test_facebook_check_names_a_broken_token_without_echoing_it():
    import pytest

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "The access token could not be decrypted", "type": "OAuthException", "code": 190}})

    env = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "EAABsbCS1iHgBO7ZCZCZBqZBw_abcdefghijklmnop"}
    with pytest.raises(social.SocialError, match="not one Facebook accepts") as err:
        social.check_facebook(httpx.Client(transport=httpx.MockTransport(handler)), env)
    assert "EAAB" not in str(err.value)
    with pytest.raises(social.SocialError, match="FACEBOOK_PAGE_ID should be digits only"):
        social.check_facebook(httpx.Client(transport=httpx.MockTransport(handler)), {"FACEBOOK_PAGE_ID": "NepalWire", "FACEBOOK_PAGE_TOKEN": env["FACEBOOK_PAGE_TOKEN"]})


def test_the_take_leads_the_long_form_posts(tmp_path):
    s = _settings(tmp_path)
    art = _article(s)
    art.take = "Police moved 140 households and nobody has said who delayed the siren. That answer decides whether this was weather or negligence."
    fb = social.compose("facebook", art, s)
    assert fb.startswith("Nepal Wire's take: Police moved 140 households")
    assert fb.index("Nepal Wire's take") < fb.index(art.headline) < fb.index("Full story with links:")
    ig = social.compose("instagram", art, s)
    assert ig.startswith("Nepal Wire's take:") and art.social_hook not in ig
    tg = social.compose("telegram", art, s)
    assert "Nepal Wire's take:" in tg and tg.index("<b>") < tg.index("Nepal Wire's take") < tg.index("<a href")
    # The short networks keep the one line hook.
    assert "take:" not in social.compose("x", art, s)
    # Without a take the headline leads, as before.
    art.take = ""
    assert social.compose("facebook", art, s).startswith(art.headline)
    assert social.compose("instagram", art, s).startswith(art.headline)
