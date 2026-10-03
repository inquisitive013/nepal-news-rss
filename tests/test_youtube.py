import dataclasses
import json
import urllib.parse

import httpx
import pytest

from newsroom import social, youtube
from newsroom.config import load_settings
from newsroom.models import Article

ENV = {"YOUTUBE_CLIENT_ID": "123-abc.apps.googleusercontent.com", "YOUTUBE_CLIENT_SECRET": "GOCSPX-fakesecret", "YOUTUBE_REFRESH_TOKEN": "1//0fakerefresh"}
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=xyz"


def _graph(channel="Nepal Wire", privacy="public", fail=None):
    """Google as YouTube's uploader meets it: the token endpoint, the channel, the resumable upload."""
    fail = fail or {}
    calls = []

    def handler(request):
        url = request.url
        if url.host == "oauth2.googleapis.com":
            calls.append(("token", dict(urllib.parse.parse_qsl(request.content.decode()))))
            if "token" in fail:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret words"})
            return httpx.Response(200, json={"access_token": "ya29.fake", "expires_in": 3599})
        if url.path.endswith("/youtube/v3/channels"):
            calls.append(("channels", request.headers["Authorization"]))
            return httpx.Response(200, json={"items": [{"snippet": {"title": channel}}] if channel else []})
        if request.method == "POST" and url.path.endswith("/upload/youtube/v3/videos"):
            calls.append(("open", dict(url.params), json.loads(request.content), request.headers["X-Upload-Content-Length"], request.headers["X-Upload-Content-Type"]))
            if "open" in fail:
                return httpx.Response(403, json={"error": {"code": 403, "message": "secret words", "errors": [{"reason": "quotaExceeded", "message": "secret words"}]}})
            return httpx.Response(200, headers={"Location": SESSION})
        if request.method == "PUT" and str(url) == SESSION:
            calls.append(("send", len(request.content), request.headers["Content-Type"]))
            return httpx.Response(200, json={"id": "yt1", "status": {"privacyStatus": privacy, "uploadStatus": "uploaded"}})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _article(**kw):
    base = dict(id="2026-10-03-story", slug="story", story_id="s1", headline="Orphans get help, 933 missing go unmentioned", dek="d", body_markdown="b", language="en", tags=["bhotekoshi-flood", "orphans", "Nepal"])
    base.update(kw)
    return Article(**base)


def test_an_upload_signs_in_checks_the_channel_and_sends_the_file_in_one_piece(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 2048)
    client, calls = _graph()

    video_id, url, privacy = youtube.upload_short(client, ENV, video, title="शीर्षक", description="विवरण", tags=["Nepal Wire"], channel="Nepal Wire")

    assert (video_id, url, privacy) == ("yt1", "https://www.youtube.com/shorts/yt1", "public")
    assert [c[0] for c in calls] == ["token", "channels", "open", "send"]
    assert calls[0][1]["grant_type"] == "refresh_token" and calls[0][1]["refresh_token"] == ENV["YOUTUBE_REFRESH_TOKEN"]
    assert calls[1][1] == "Bearer ya29.fake"
    _, params, meta, length, kind = calls[2]
    assert params == {"uploadType": "resumable", "part": "snippet,status"} and length == "2048" and kind == "video/mp4"
    assert meta["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "embeddable": True}
    assert meta["snippet"]["title"] == "शीर्षक" and meta["snippet"]["categoryId"] == "25" and meta["snippet"]["defaultAudioLanguage"] == "ne"
    assert calls[3] == ("send", 2048, "video/mp4")


def test_a_token_for_another_channel_uploads_nothing_and_never_names_it(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 10)
    client, calls = _graph(channel="Someone's own vlog")
    with pytest.raises(social.SocialError) as caught:
        youtube.upload_short(client, ENV, video, title="t", description="d", tags=[], channel="Nepal Wire")
    assert "not named Nepal Wire" in str(caught.value) and "vlog" not in str(caught.value)
    assert [c[0] for c in calls] == ["token", "channels"]

    empty, _ = _graph(channel="")
    with pytest.raises(social.SocialError, match="no channel yet"):
        youtube.upload_short(empty, ENV, video, title="t", description="d", tags=[], channel="Nepal Wire")


def test_google_errors_carry_the_reason_never_the_message(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 10)
    refused, _ = _graph(fail={"token": True})
    with pytest.raises(social.SocialError) as caught:
        youtube.upload_short(refused, ENV, video, title="t", description="d", tags=[], channel="Nepal Wire")
    assert str(caught.value) == "YouTube sign in: HTTP 400 invalid_grant"

    over, _ = _graph(fail={"open": True})
    with pytest.raises(social.SocialError) as caught:
        youtube.upload_short(over, ENV, video, title="t", description="d", tags=[], channel="Nepal Wire")
    assert str(caught.value) == "YouTube upload: HTTP 403 quotaExceeded"
    assert "secret" not in str(caught.value) and ENV["YOUTUBE_REFRESH_TOKEN"] not in str(caught.value)


def test_the_title_is_the_checked_nepali_headline_cut_to_youtubes_limit():
    checked = {"checked": True, "held": False, "headline": "बाढीपीडित <बालबालिका> लाई सहयोग"}
    assert youtube.title_for(_article(nepali=checked)) == "बाढीपीडित बालबालिका लाई सहयोग"  # YouTube refuses angle brackets
    assert youtube.title_for(_article(nepali={**checked, "held": True})) == "Orphans get help, 933 missing go unmentioned"
    long = youtube.title_for(_article(headline="word " * 40))
    assert len(long) <= youtube.TITLE_LIMIT and long.endswith("…") and not long.endswith(" …")


def test_the_description_keeps_the_link_and_the_tags_fit():
    text = youtube.description_for("क" * 3000, "https://example.org/a.html", "Nepal Wire")
    assert text.endswith("\n\nNepal Wire: https://example.org/a.html") and len(text.encode("utf-8")) <= youtube.DESCRIPTION_LIMIT
    tags = youtube.tags_for(_article(tags=["nepal", "bhotekoshi-flood"] + [f"tag-{i}" for i in range(100)]), "Nepal Wire")
    assert tags[:4] == ["Nepal Wire", "Nepal", "Nepali news", "bhotekoshi flood"]  # "nepal" is already there
    assert len(", ".join(tags)) <= youtube.TAGS_LIMIT


def test_the_account_check_names_the_channel_only_when_it_is_nepal_wires(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    client, _ = _graph()
    rows = {r["network"]: r for r in social.check_networks(settings, ENV, client=client)}
    assert rows["youtube"] == {"network": "youtube", "configured": "yes", "ok": "yes", "account": "the Nepal Wire channel", "note": "Reels only"}

    other, _ = _graph(channel="A person's channel")
    rows = {r["network"]: r for r in social.check_networks(settings, ENV, client=other)}
    assert rows["youtube"]["ok"] == "no" and "person" not in rows["youtube"]["note"]

    rows = {r["network"]: r for r in social.check_networks(settings, {}, client=other)}
    assert rows["youtube"]["configured"] == "no" and "YOUTUBE_REFRESH_TOKEN" in rows["youtube"]["note"]
