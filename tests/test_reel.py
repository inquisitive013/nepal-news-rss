import dataclasses
import json
import re
import subprocess
import urllib.parse

import httpx
import pytest
from PIL import Image, ImageDraw

from newsroom import graphic, reel, social
from newsroom.config import load_settings
from newsroom.models import Article, ImageAsset, ImageCredit

ENV = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "EAABfaketoken_1234567890abcdefghijklmnop"}
AID = "2026-09-30-reel-story"


@pytest.fixture(autouse=True)
def _fresh_facebook_token_cache():
    social._PAGE_TOKENS.clear()
    yield


def _settings(tmp_path):
    return dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")


def _article(settings, kind="generated"):
    (settings.data_dir / "images").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1536, 1024), (90, 90, 90)).save(settings.root / f"data/images/{AID}.jpg")
    credit = ImageCredit(kind="generated", model="gpt-image-1") if kind == "generated" else ImageCredit(kind="found", author="A. Photographer", source="Wikimedia Commons", license="CC BY-SA 4.0")
    return Article(
        id=AID, slug="reel-story", story_id="s1", headline="Runner breaks record", dek="d", body_markdown="b", language="en",
        sources=[{"name": "Ratopati", "url": "u1"}, {"name": "Setopati", "url": "u2"}],
        image=ImageAsset(path=f"data/images/{AID}.jpg", alt="a", width=1536, height=1024, credit=credit),
        run_date="2026-09-30", published_at="2026-09-30T01:00:00+00:00",
    )


def _beats(settings, beats=None, caption="मुकेश पालले कीर्तिमान भत्काए"):
    folder = settings.data_dir / "reels"
    folder.mkdir(parents=True, exist_ok=True)
    beats = beats or [{"seconds": 1, "lines": [["५००० मिटरमा पाल", 110, "white"], ["२२ वर्षे कीर्तिमान भत्काए", 100, "gold"]]}, {"seconds": 1, "lines": [["१४:०३.६१", 190, "gold"]]}]
    (folder / f"{AID}.json").write_text(json.dumps({"caption": caption, "beats": beats}, ensure_ascii=False), encoding="utf-8")


def test_a_reel_is_tall_short_and_opens_on_the_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(reel, "FPS", 5)
    monkeypatch.setattr(reel, "END_SECONDS", 1.0)
    settings = _settings(tmp_path)
    art = _article(settings)
    _beats(settings)
    beats, caption = reel.load_beats(settings, AID)
    assert caption == "मुकेश पालले कीर्तिमान भत्काए" and [len(b.lines) for b in beats] == [2, 1]

    out = reel.render_reel(settings, art, beats, tmp_path / "out" / "reel.mp4")
    probe = subprocess.run([reel._ffmpeg(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "1080x1920" in probe and "h264" in probe and "aac" in probe
    seconds = re.search(r"Duration: 00:00:(\d+\.\d+)", probe)
    assert seconds and abs(float(seconds.group(1)) - 3.0) < 0.3  # two one second beats and the end card
    # Instagram's rules: the index before the data, no edit lists, sound at 48 kHz and no more than 128 kbps
    body = out.read_bytes()
    assert 0 <= body.find(b"moov") < body.find(b"mdat") and b"elst" not in body
    assert "48000 Hz" in probe and int(re.search(r"Audio: aac.*?(\d+) kb/s", probe).group(1)) <= 128

    first = tmp_path / "first.png"
    subprocess.run([reel._ffmpeg(), "-loglevel", "error", "-y", "-i", str(out), "-frames:v", "1", str(first)], check=True)
    im = Image.open(first).convert("RGB")
    band = [im.getpixel((x, y)) for y in range(reel.TEXT_TOP, reel.TEXT_BOTTOM, 6) for x in range(60, reel.W - 60, 6)]
    assert any(min(p) > 200 for p in band)  # the hook's white letters are on the very first frame


def test_every_beats_file_makes_a_reel_meta_accepts():
    """Meta publishes Reels of 4 to 60 seconds; each committed beats file must land inside that."""
    settings = load_settings(mock=True)
    files = sorted((settings.data_dir / "reels").glob("*.json"))
    assert files, "no beats files committed"
    for path in files:
        beats, caption = reel.load_beats(settings, path.stem)  # stretched to the narration, as the Reel plays
        reel.load_sound(settings, path.stem)
        seconds = reel.timeline(beats)[-1][1]
        assert 4 <= seconds <= 60 and caption, path.name
        assert all(b.say for b in beats if b.audio is not None), path.name
        assert (settings.data_dir / "articles" / path.name).exists(), f"{path.name} names no stored story"


def test_the_end_card_keeps_outlet_names_whole():
    draw = ImageDraw.Draw(Image.new("RGB", (reel.W, 100)))
    fnt = graphic.font("mono", 30)
    names = ["DC Nepal", "Setopati", "Ratopati", "The Himalayan Times", "OnlineKhabar English News", "World Athletics athlete profile"]
    rows = reel.name_rows(draw, names, fnt, reel.W - 120)
    assert " · ".join(rows).split(" · ") == names and all(graphic.text_width(draw, r, fnt) <= reel.W - 120 for r in rows)
    assert not any(r.startswith("·") for r in rows)


def test_a_story_without_beats_says_what_to_write(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(reel.ReelError, match="data/reels/2026-09-30-reel-story.json"):
        reel.load_beats(settings, AID)


def test_publishing_runs_metas_three_steps(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 1234)
    calls = []

    def handler(request):
        url = request.url
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.host == "rupload.facebook.com":
            calls.append(("upload", url.path, request.headers["Authorization"], request.headers["file_size"], request.headers["offset"], len(request.content)))
            return httpx.Response(200, json={"success": True})
        if url.path.endswith("/111/video_reels"):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            calls.append((form["upload_phase"], form.get("video_id", ""), form.get("video_state", ""), form.get("description", "")))
            return httpx.Response(200, json={"video_id": "v1"} if form["upload_phase"] == "start" else {"success": True})
        if url.path.endswith("/v1"):
            return httpx.Response(200, json={"permalink_url": "/reel/v1/"})
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    video_id, url = reel.publish_reel(client, ENV, video, "कीर्तिमान")
    assert video_id == "v1" and url == "https://www.facebook.com/reel/v1/"
    assert calls[0] == ("start", "", "", "")
    assert calls[1][0] == "upload" and calls[1][1].endswith("/v1") and calls[1][2] == f"OAuth {ENV['FACEBOOK_PAGE_TOKEN']}"
    assert calls[1][3:] == ("1234", "0", 1234)  # the whole file in one request, its size in the header
    assert calls[2] == ("finish", "v1", "PUBLISHED", "कीर्तिमान")


def test_a_refused_upload_raises_and_publishes_nothing(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 10)
    phases = []

    def handler(request):
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if request.url.host == "rupload.facebook.com":
            return httpx.Response(400, json={"error": {"code": 6000, "message": "bad"}})
        phases.append(dict(urllib.parse.parse_qsl(request.content.decode())).get("upload_phase"))
        return httpx.Response(200, json={"video_id": "v1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(social.SocialError, match="upload"):
        reel.publish_reel(client, ENV, video, "x")
    assert phases == ["start"]  # never asked to publish a video that did not arrive


def _tone(path, seconds, hz, volume="0.5"):
    """A test clip: a plain tone of a known length, standing in for a narration clip or the bed."""
    subprocess.run([reel._ffmpeg(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}", "-af", f"volume={volume}", str(path)], check=True)
    return path


def test_narration_stretches_each_beat_and_plays_over_the_bed(tmp_path, monkeypatch):
    monkeypatch.setattr(reel, "FPS", 5)
    monkeypatch.setattr(reel, "END_SECONDS", 1.0)
    settings = _settings(tmp_path)
    art = _article(settings)
    sounds = tmp_path / "data" / "reels" / "audio"
    sounds.mkdir(parents=True)
    _tone(sounds / "say1.wav", 1.5, 440)
    _tone(sounds / "end.wav", 0.4, 660)
    _tone(sounds / "bed.wav", 8, 220, volume="0.3")
    _beats(settings, beats=[
        {"seconds": 1, "lines": [["५००० मिटरमा पाल", 110, "white"]], "say": "पालले कीर्तिमान भत्काए।", "audio": "data/reels/audio/say1.wav"},
        {"seconds": 1, "lines": [["१४:०३.६१", 190, "gold"]]},
    ])
    raw = json.loads(reel.reel_path(settings, AID).read_text(encoding="utf-8"))
    raw.update(end_audio="data/reels/audio/end.wav", end_say="नेपाल वायर।", music="data/reels/audio/bed.wav")
    reel.reel_path(settings, AID).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    beats, _ = reel.load_beats(settings, AID)
    assert beats[0].seconds == pytest.approx(reel.VOICE_LEAD + 1.5 + reel.VOICE_TAIL, abs=0.05)  # stretched to fit its words
    assert beats[1].seconds == 1 and beats[1].audio is None  # a beat without a clip keeps its time
    sound = reel.load_sound(settings, AID)
    assert sound.end.name == "end.wav" and sound.music.name == "bed.wav"

    out = reel.render_reel(settings, art, beats, tmp_path / "out" / "reel.mp4", sound=sound)
    probe = subprocess.run([reel._ffmpeg(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    total = reel.timeline(beats)[-1][1]
    seconds = re.search(r"Duration: 00:00:(\d+\.\d+)", probe)
    assert "aac" in probe and seconds and abs(float(seconds.group(1)) - total) < 0.3

    def rms(start, length):
        report = subprocess.run([reel._ffmpeg(), "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(out), "-vn", "-af", "astats=metadata=0", "-f", "null", "-"], capture_output=True, text=True).stderr
        return float(re.search(r"RMS level dB: (-?[\d.]+|-inf)", report).group(1).replace("-inf", "-200"))

    voiced, between = rms(0.5, 1.0), rms(total - 1.0, 0.3)
    assert voiced > -30  # the words are there
    assert -60 < between < voiced  # the bed plays on under the end card, quieter than the voice
    # Past the last word to the final frame: until 1 October the bed stopped with the voice.
    last_word = reel.timeline(beats)[-1][0] + reel.VOICE_LEAD + 0.4
    assert rms(last_word + 0.05, total - last_word - 0.1) > -60


def test_a_clip_needs_its_words_and_a_named_file_must_exist(tmp_path):
    settings = _settings(tmp_path)
    sounds = tmp_path / "data" / "reels" / "audio"
    sounds.mkdir(parents=True)
    _tone(sounds / "say1.wav", 0.5, 440)
    _beats(settings, beats=[{"seconds": 1, "lines": [["क", 80, "white"]], "audio": "data/reels/audio/say1.wav"}])
    with pytest.raises(reel.ReelError, match="not the words it says"):
        reel.load_beats(settings, AID)
    _beats(settings, beats=[{"seconds": 1, "lines": [["क", 80, "white"]], "say": "क", "audio": "data/reels/audio/missing.mp3"}])
    with pytest.raises(reel.ReelError, match="which is not there"):
        reel.load_beats(settings, AID)


def test_take_down_touches_only_the_named_reel_after_the_new_one_is_up(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    settings = _settings(tmp_path)
    art = _article(settings)
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 100)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.setattr(cli.publish, "load_articles", lambda s: [art])
    monkeypatch.setattr(reel, "load_beats", lambda s, aid: ([reel.Beat([reel.Line("क")], 4.0)], "कीर्तिमान"))
    monkeypatch.setattr(reel, "load_sound", lambda s, aid: reel.Sound())
    monkeypatch.setattr(reel, "render_reel", lambda *a, **k: video)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    social.save_record(settings, social.SocialRecord(article_id=AID, posts=[
        social.Post(network="facebook", status="posted", id="111_photo1"),
        social.Post(network="facebook_reel", status="posted", id="old1"),
    ]))
    calls = []

    def handler(request):
        url = request.url
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.host == "rupload.facebook.com":
            calls.append("upload")
            return httpx.Response(200, json={"success": True})
        if url.path.endswith("/video_reels"):
            phase = dict(urllib.parse.parse_qsl(request.content.decode()))["upload_phase"]
            calls.append(phase)
            return httpx.Response(200, json={"video_id": "new1"} if phase == "start" else {"success": True})
        if request.method == "DELETE":
            calls.append(("delete", url.path.rsplit("/", 1)[-1]))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))

    # a photo post's id is not a Reel of this story: refused before anything is posted
    assert cli.main(["reel", "--article", AID, "--publish", "--again", "--take-down", "111_photo1"]) == 1
    assert calls == [] and "not a live Reel of this story" in capsys.readouterr().out

    # without --again no new cut goes up, so the live Reel stays
    assert cli.main(["reel", "--article", AID, "--publish", "--take-down", "old1"]) == 1
    assert calls == [] and "nothing was taken down. Add --again" in capsys.readouterr().out
    assert {p.id: p.status for p in social.load_record(settings, art).posts}["old1"] == "posted"

    assert cli.main(["reel", "--article", AID, "--publish", "--again", "--take-down", "old1"]) == 0
    assert calls == ["start", "upload", "finish", ("delete", "old1")]  # the new cut is up before the old one comes down
    posts = {p.id: p for p in social.load_record(settings, art).posts}
    assert posts["old1"].status == "removed" and "owner's request, replaced by Reel new1" in posts["old1"].error
    assert posts["new1"].status == "posted" and posts["111_photo1"].status == "posted"


def test_a_held_story_gets_no_reel(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    settings = _settings(tmp_path)
    art = _article(settings)
    art.nepali = {"headline": "शीर्षक", "body_markdown": "बडी", "held": True}
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.setattr(cli.publish, "load_articles", lambda s: [art])
    assert cli.main(["reel", "--article", AID, "--publish"]) == 1
    assert "is held" in capsys.readouterr().out


def test_a_photo_loses_its_burned_credit_bar_in_the_tall_frame(tmp_path):
    """1 October: the Bhotekoshi valley photo showed its credit bar cut off along the Reel's bottom edge."""
    from newsroom import images

    settings = _settings(tmp_path)
    art = _article(settings, kind="found")
    img = Image.new("RGB", (1536, 1024), (90, 90, 90))
    img.paste((255, 0, 0), (0, 1024 - images.credit_bar_height(1024), 1536, 1024))  # where the credit is burned
    img.save(settings.root / art.image.path)
    bg = reel.background(settings, art)
    pixels = list(bg.crop((0, bg.height - 6, bg.width, bg.height)).getdata())
    red = sum(p[0] for p in pixels) / len(pixels)
    green = sum(p[1] for p in pixels) / len(pixels)
    assert red - green < 10


IG = {"INSTAGRAM_USER_ID": "ig1", "INSTAGRAM_ACCESS_TOKEN": "EAABigtoken_1234567890abcdefghijklmnop"}


def _instagram(direct=True, statuses=("FINISHED",)):
    """Instagram's Reels publishing: a container, the file sent to rupload, the status, the publish."""
    calls = []
    states = list(statuses)

    def handler(request):
        url = request.url
        if url.host == "rupload.facebook.com":
            calls.append(("upload", url.path, request.headers["Authorization"], request.headers["offset"], request.headers["file_size"], len(request.content)))
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and url.path.endswith("/ig1/media"):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            calls.append(("open", form))
            if form.get("upload_type") == "resumable" and not direct:
                return httpx.Response(400, json={"error": {"code": 100, "message": "not for this app"}})
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/c1"} if form.get("upload_type") else {"id": "c2"})
        if url.path.endswith(("/c1", "/c2")):
            calls.append(("status", url.params["fields"]))
            return httpx.Response(200, json={"status_code": states.pop(0) if len(states) > 1 else states[0]})
        if url.path.endswith("/ig1/media_publish"):
            calls.append(("publish", dict(urllib.parse.parse_qsl(request.content.decode()))["creation_id"]))
            return httpx.Response(200, json={"id": "m1"})
        if url.path.endswith("/m1"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_an_instagram_reel_goes_up_by_direct_upload_and_waits_for_meta(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 777)
    client, calls = _instagram(statuses=("IN_PROGRESS", "FINISHED"))
    waits = []

    media_id, url = reel.publish_instagram_reel(client, IG, video, "कीर्तिमान", sleep=waits.append)

    assert (media_id, url) == ("m1", "https://www.instagram.com/reel/abc/")
    assert calls[0][0] == "open" and calls[0][1]["media_type"] == "REELS" and calls[0][1]["upload_type"] == "resumable" and calls[0][1]["caption"] == "कीर्तिमान"
    assert "video_url" not in calls[0][1]
    assert calls[1] == ("upload", "/ig-api-upload/v23.0/c1", f"OAuth {IG['INSTAGRAM_ACCESS_TOKEN']}", "0", "777", 777)
    assert [c[0] for c in calls[2:]] == ["status", "status", "publish"] and calls[-1] == ("publish", "c1")
    assert waits == [10.0]  # one wait while Meta processed it


def test_instagram_fetches_the_page_reels_file_when_it_refuses_the_direct_upload(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 10)
    client, calls = _instagram(direct=False)
    asked = []

    def page_file():
        asked.append(True)
        return "https://video.example.fbcdn.net/reel.mp4"

    media_id, _ = reel.publish_instagram_reel(client, IG, video, "x", fallback_url=page_file, sleep=lambda s: None)

    assert media_id == "m1" and asked == [True]
    opens = [c[1] for c in calls if c[0] == "open"]
    assert opens[0]["upload_type"] == "resumable" and opens[1]["video_url"] == "https://video.example.fbcdn.net/reel.mp4" and "upload_type" not in opens[1]
    assert not any(c[0] == "upload" for c in calls) and calls[-1] == ("publish", "c2")

    # with nothing to fall back on, Meta's refusal stands
    client, _ = _instagram(direct=False)
    with pytest.raises(social.SocialError, match="Instagram Reel: HTTP 400"):
        reel.publish_instagram_reel(client, IG, video, "x", sleep=lambda s: None)


def test_the_reel_goes_to_every_connected_place_and_one_failure_stops_none(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    YT = {"YOUTUBE_CLIENT_ID": "123-abc.apps.googleusercontent.com", "YOUTUBE_CLIENT_SECRET": "GOCSPX-fakesecret", "YOUTUBE_REFRESH_TOKEN": "1//0fakerefresh"}
    settings = _settings(tmp_path)
    art = _article(settings)
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 100)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.setattr(cli.publish, "load_articles", lambda s: [art])
    monkeypatch.setattr(reel, "load_beats", lambda s, aid: ([reel.Beat([reel.Line("क")], 4.0)], "कीर्तिमान"))
    monkeypatch.setattr(reel, "load_sound", lambda s, aid: reel.Sound())
    monkeypatch.setattr(reel, "render_reel", lambda *a, **k: video)
    monkeypatch.setattr(reel, "facebook_video_file", lambda client, environ, video_id, **k: "")  # Meta has not processed it yet
    for key, value in {**ENV, **IG, **YT}.items():
        monkeypatch.setenv(key, value)
    instagram_up = {"ok": False}
    seen = []

    def handler(request):
        url = request.url
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.host == "rupload.facebook.com":
            seen.append("ig upload" if "ig-api-upload" in url.path else "fb upload")
            return httpx.Response(200, json={"success": True})
        if url.path.endswith("/111/video_reels"):
            phase = dict(urllib.parse.parse_qsl(request.content.decode()))["upload_phase"]
            seen.append(f"fb {phase}")
            return httpx.Response(200, json={"video_id": "new1"} if phase == "start" else {"success": True})
        if url.path.endswith("/new1"):
            return httpx.Response(200, json={"permalink_url": "/reel/new1/"})
        if url.path.endswith("/ig1/media"):
            seen.append("ig open")
            if not instagram_up["ok"]:
                return httpx.Response(400, json={"error": {"code": 9004, "message": "media fetch failed"}})
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/c1"})
        if url.path.endswith("/c1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if url.path.endswith("/ig1/media_publish"):
            seen.append("ig publish")
            return httpx.Response(200, json={"id": "m1"})
        if url.path.endswith("/m1"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        if url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.fake"})
        if url.path.endswith("/youtube/v3/channels"):
            return httpx.Response(200, json={"items": [{"snippet": {"title": "Nepal Wire"}}]})
        if request.method == "POST" and url.path.endswith("/upload/youtube/v3/videos"):
            seen.append("yt open")
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=1"})
        if request.method == "PUT":
            seen.append("yt send")
            return httpx.Response(200, json={"id": "yt1", "status": {"privacyStatus": "private"}})
        return httpx.Response(404, json={})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))

    # Instagram refuses and has no Page file to fall back on: recorded as failed, and YouTube still gets the Reel
    assert cli.main(["reel", "--article", AID, "--publish"]) == 1
    out = capsys.readouterr().out
    assert seen == ["fb start", "fb upload", "fb finish", "ig open", "yt open", "yt send"]
    assert "did not go up on Instagram" in out and "::warning::YouTube set it private" in out
    posts = {p.network: p for p in social.load_record(settings, art).posts}
    assert posts["facebook_reel"].status == "posted" and posts["facebook_reel"].id == "new1"
    assert posts["instagram_reel"].status == "failed"
    assert posts["youtube_short"].status == "posted" and posts["youtube_short"].url == "https://www.youtube.com/shorts/yt1" and "audit" in posts["youtube_short"].error

    # run again without --again: only the place still missing the Reel gets it
    seen.clear()
    instagram_up["ok"] = True
    assert cli.main(["reel", "--article", AID, "--publish"]) == 0
    assert seen == ["ig open", "ig upload", "ig publish"]
    out = capsys.readouterr().out
    assert "already on the Facebook Page. Add --again" in out and "already on YouTube." in out
    live = [p for p in social.load_record(settings, art).posts if p.status == "posted"]
    assert sorted(p.network for p in live) == ["facebook_reel", "instagram_reel", "youtube_short"]


def test_a_reel_meta_cannot_process_names_metas_reason_never_the_container(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\x00" * 10)

    def handler(request):
        url = request.url
        if url.host == "rupload.facebook.com":  # as on 3 October: the direct upload refused
            return httpx.Response(400, json={"debug_info": {"retriable": False, "type": "ProcessingFailedError", "message": "Request processing failed"}})
        if request.method == "POST" and url.path.endswith("/ig1/media"):
            return httpx.Response(200, json={"id": "18117783133877951"})
        if url.path.endswith("/18117783133877951"):
            assert url.params["fields"] == "status_code,status"
            return httpx.Response(200, json={"id": "18117783133877951", "status_code": "ERROR", "status": "Error: Media download has failed. (2207052)"})
        return httpx.Response(400, json={"error": {"code": 100}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(social.SocialError) as caught:
        reel.publish_instagram_reel(client, IG, video, "x", fallback_url=lambda: "https://video.example/reel.mp4", sleep=lambda s: None)
    assert str(caught.value) == "Instagram Reel: Meta could not process it: Error: Media download has failed. (2207052)"

