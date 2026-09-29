import dataclasses
import io
import json

from PIL import Image

from newsroom import images
from newsroom.config import load_settings
from newsroom.llm import MockLLM, UsageMeter
from newsroom.models import Article, ImageCredit
from tests.conftest import FIXTURES

ALLOWED = ["cc0", "public domain", "pdm", "cc by", "cc-by", "by-sa", "cc by-sa"]


def _png(w=1200, h=800, color=(200, 30, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_license_allowed():
    assert images.license_allowed("CC BY-SA 4.0", ALLOWED)
    assert images.license_allowed("CC0", ALLOWED)
    assert images.license_allowed("Public domain", ALLOWED)
    assert not images.license_allowed("CC BY-NC-ND 2.0", ALLOWED)
    assert not images.license_allowed("CC BY-NC 2.0", ALLOWED)
    assert not images.license_allowed("", ALLOWED)
    assert not images.license_allowed("Copyrighted, fair use", ALLOWED)


def test_parse_commons_filters_and_credits():
    data = json.loads((FIXTURES / "images" / "commons.json").read_text())
    cands = images.parse_commons(data, ALLOWED)
    assert len(cands) == 1  # svg and NC entries dropped
    c = cands[0]
    assert c.provider == "Wikimedia Commons"
    assert c.author == "Example Photographer"  # HTML stripped
    assert c.license == "CC BY-SA 4.0"
    assert c.license_url.startswith("https://creativecommons.org/licenses/by-sa/4.0")
    assert c.page_url.endswith("Kathmandu_Durbar_Square_2019.jpg")
    assert c.url.startswith("https://upload.wikimedia.org/wikipedia/commons/thumb/")


def test_parse_openverse_filters_and_credits():
    data = json.loads((FIXTURES / "images" / "openverse.json").read_text())
    cands = images.parse_openverse(data, ALLOWED)
    assert len(cands) == 1
    c = cands[0]
    assert c.license == "CC BY 2.0"
    assert c.author == "Some Photographer"
    assert c.page_url == "https://www.flickr.com/photos/someone/123456"
    assert c.provider.startswith("Openverse")


def test_cover_card_is_png_of_requested_size():
    data = images.cover_card("Heavy rain puts Bagmati settlements at risk", "Nepal Wire", "26 September 2026")
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.size == (1600, 900)
    data_ne = images.cover_card("काठमाडौंमा भारी वर्षा", "Nepal Wire", "26 September 2026")
    assert Image.open(io.BytesIO(data_ne)).size == (1600, 900)


def test_store_image_downscales_and_burns_credit(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    credit = ImageCredit(kind="found", title="Durbar Square", author="Example Photographer", source="Wikimedia Commons", license="CC BY-SA 4.0")
    asset = images.store_image(_png(3200, 1600), "2026-09-26-test", settings, credit, alt="Durbar Square")
    assert asset.path == "data/images/2026-09-26-test.jpg"
    out = tmp_path / asset.path
    assert out.exists()
    img = Image.open(out)
    assert img.width == 1600 and img.height == 800
    assert asset.width == 1600
    assert "Example Photographer" in credit.line() and "CC BY-SA 4.0" in credit.line()
    assert credit.line().startswith("File photo:")
    assert ImageCredit(kind="found").line() == "File photo: source unknown"


def test_pick_image_with_mock_chooses_first():
    settings = load_settings(mock=True)
    llm = MockLLM(settings, UsageMeter(10))
    art = Article(id="a", slug="a", story_id="s", headline="Kathmandu rain", dek="d", body_markdown="", language="en")
    cands = [
        images.ImageCandidate("Wikimedia Commons", "https://x/full.jpg", "https://x/thumb.jpg", "https://x/page", title="Rain", author="A", license="CC BY 4.0"),
        images.ImageCandidate("Openverse", "https://y/full.jpg", "https://y/thumb.jpg", "https://y/page", title="Other", author="B", license="CC0"),
    ]
    picked = images.pick_image(llm, art, cands, fetch=lambda url: (_png(600, 400), "image/png"))
    assert picked is not None
    cand, alt = picked
    assert cand.title == "Rain" and alt


def test_make_image_for_article_found_path(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    llm = MockLLM(settings, UsageMeter(10))
    art = Article(id="2026-09-26-rain", slug="rain", story_id="s", headline="Kathmandu rain", dek="d", body_markdown="", language="en", image_brief={"search_queries": ["Kathmandu"], "generation_prompt": "x", "alt_text": "alt"})
    commons = json.loads((FIXTURES / "images" / "commons.json").read_text())

    def fake_json(url):
        if "commons.wikimedia.org" in url:
            return commons
        raise RuntimeError("openverse down")

    asset = images.make_image_for_article(llm, settings, art, "26 September 2026", fetch_json_fn=fake_json, fetch_bytes_fn=lambda url: (_png(), "image/png"))
    assert asset.credit.kind == "found"
    assert asset.credit.author == "Example Photographer"
    assert (tmp_path / asset.path).exists()


def test_make_image_for_article_falls_back_to_cover_card(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    llm = MockLLM(settings, UsageMeter(10))
    art = Article(id="2026-09-26-x", slug="x", story_id="s", headline="Petrol price cut", dek="d", body_markdown="", language="en")
    asset = images.make_image_for_article(llm, settings, art, "26 September 2026")
    assert asset.credit.kind == "cover_card"
    assert asset.path.endswith(".png")
    assert (tmp_path / asset.path).exists()


def test_make_image_uses_generation_when_available(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    llm = MockLLM(settings, UsageMeter(10))
    art = Article(id="2026-09-26-g", slug="g", story_id="s", headline="Generated", dek="d", body_markdown="", language="en", image_brief={"search_queries": [], "generation_prompt": "a scene", "alt_text": "alt"})
    asset = images.make_image_for_article(llm, settings, art, "26 September 2026", fetch_json_fn=lambda url: {"query": {"pages": {}}}, generate_fn=lambda prompt, s: (_png(1024, 1024, (10, 10, 200)), "test-model"))
    assert asset.credit.kind == "generated" and asset.credit.model == "test-model"
    assert "AI generated" in asset.credit.line()


def test_the_openai_key_check_costs_nothing_and_never_echoes_the_key():
    import httpx
    from newsroom import images
    from newsroom.config import load_settings

    settings = load_settings(mock=True)
    seen = []

    def transport(status, body):
        def handler(request):
            seen.append((request.method, str(request.url), request.headers.get("authorization")))
            return httpx.Response(status, json=body)
        return httpx.Client(transport=httpx.MockTransport(handler))

    ok = images.check_openai_key(settings, {"OPENAI_API_KEY": "  sk-proj-test123\n"}, transport(200, {"id": "gpt-image-1"}))
    assert ok == ("ok", "OpenAI accepted the key and can see gpt-image-1")
    # a model lookup, never a generation; the pasted key is trimmed
    assert seen[-1] == ("GET", "https://api.openai.com/v1/models/gpt-image-1", "Bearer sk-proj-test123")

    bad = {"error": {"message": "Incorrect API key provided: sk-proj-****t123.", "type": "invalid_request_error", "code": "invalid_api_key"}}
    status, note = images.check_openai_key(settings, {"OPENAI_API_KEY": "sk-proj-test123"}, transport(401, bad))
    assert status == "failed" and "invalid_api_key" in note and "replace the OPENAI_API_KEY secret" in note
    assert "sk-" not in note  # OpenAI's message quotes part of the key; the note never does

    status, note = images.check_openai_key(settings, {"OPENAI_API_KEY": "sk-proj-test123"}, transport(404, {"error": {"code": "model_not_found"}}))
    assert status == "failed" and "cannot use gpt-image-1" in note and "model_not_found" in note

    assert images.check_openai_key(settings, {}, transport(200, {}))[0] == "not set"
    raw = dict(settings.raw)
    raw["images"] = dict(raw["images"], generation=dict(raw["images"]["generation"], provider="none"))
    import dataclasses
    assert images.check_openai_key(dataclasses.replace(settings, raw=raw), {"OPENAI_API_KEY": "sk-proj-x"}, transport(200, {}))[0] == "off"


# --------------------------------------------------------------------------- the picture desk


def _page(pageid, name, *, width=4000, height=2667, license="CC BY-SA 4.0", mime="image/jpeg", index=1, description="", categories="", author="A. Photographer"):
    return {
        "pageid": pageid,
        "ns": 6,
        "title": f"File:{name}",
        "index": index,
        "imageinfo": [
            {
                "thumburl": f"https://upload.wikimedia.org/thumb/{pageid}/1600px-{name}",
                "thumbwidth": 1600,
                "thumbheight": int(1600 * height / width),
                "url": f"https://upload.wikimedia.org/{pageid}/{name}",
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
                "width": width,
                "height": height,
                "mime": mime,
                "extmetadata": {
                    "Artist": {"value": f"<a href='//x'>{author}</a>"},
                    "LicenseShortName": {"value": license},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                    "ImageDescription": {"value": description},
                    "Categories": {"value": categories},
                    "DateTimeOriginal": {"value": "2019-05-01"},
                },
            }
        ],
    }


def _pages(*pages):
    return {"query": {"pages": {str(p["pageid"]): p for p in pages}}}


def _library(calls):
    """A fake Wikidata, Commons and Openverse that know one subject: the Supreme Court of Nepal."""

    def fetch(url):
        calls.append(url)
        if "wikidata.org" in url and "wbsearchentities" in url:
            if url.endswith("search=Supreme+Court+of+Nepal"):
                return {"search": [
                    {"id": "Q1", "label": "Supreme Court", "description": "Wikimedia disambiguation page"},
                    {"id": "Q2", "label": "Supreme Court of Nepal", "description": "highest court in Nepal", "match": {"type": "label", "text": "Supreme Court of Nepal"}},
                ]}
            return {"search": []}
        if "wikidata.org" in url and "wbgetentities" in url:
            return {"entities": {"Q2": {"claims": {
                "P18": [{"mainsnak": {"datavalue": {"value": "Supreme Court of Nepal.jpg", "type": "string"}}, "rank": "normal"}],
                "P373": [{"mainsnak": {"datavalue": {"value": "Supreme Court of Nepal", "type": "string"}}, "rank": "normal"}],
            }}}}
        if "commons.wikimedia.org" in url and "titles=File" in url:
            return _pages(_page(10, "Supreme Court of Nepal.jpg", description="The Supreme Court of Nepal, Ramshahpath"))
        if "commons.wikimedia.org" in url and "haswbstatement" in url:
            return _pages(
                _page(10, "Supreme Court of Nepal.jpg", index=1),  # the same file again: counted once
                _page(11, "Supreme Court building at dusk.jpg", index=2, license="CC0"),
            )
        if "commons.wikimedia.org" in url and "categorymembers" in url:
            return _pages(
                _page(12, "Supreme Court of Nepal logo.png", mime="image/png", index=1),  # a logo, never a card photo
                _page(13, "Supreme Court gate small.jpg", width=500, height=333, index=2),  # too small for the card
                _page(14, "Supreme Court courtyard.jpg", index=3),
                _page(15, "Supreme Court used last week.jpg", index=4),
            )
        if "commons.wikimedia.org" in url and "generator=search" in url:
            return _pages(_page(16, "Ramshahpath Kathmandu.jpg"))
        if "openverse" in url:
            return {"results": []}
        raise AssertionError(f"unexpected request {url}")

    return fetch


def test_the_ladder_drops_years_and_shortens_long_queries():
    assert images.clean_subject("Bhotekoshi river flood damage Nepal 2026") == "Bhotekoshi river flood damage Nepal"
    assert images.heads("Supreme Court of Nepal building exterior") == [
        "Supreme Court of Nepal building exterior",
        "Supreme Court of Nepal building",
        "Supreme Court of Nepal",
        "Supreme Court",  # never "Supreme Court of"
    ]
    assert images.heads("Rasuwa District") == ["Rasuwa District"]  # one word names too many things
    assert images.heads("Kathmandu") == ["Kathmandu"]
    assert images.clean_subject("Maharajgunj, Kathmandu (2025)") == "Maharajgunj Kathmandu"


def test_the_entity_must_carry_every_word_and_the_story_country_wins():
    hits = [
        {"id": "Q1", "label": "Supreme Court", "description": "Wikimedia disambiguation page"},
        {"id": "Q3", "label": "Supreme Court of the United States", "description": "highest court of the United States"},
        {"id": "Q2", "label": "Supreme Court of Nepal", "description": "highest court in Nepal"},
    ]
    assert images.pick_entity("Supreme Court", hits, "Nepal")["id"] == "Q2"
    assert images.pick_entity("Supreme Court", hits, "")["id"] == "Q3"
    assert images.pick_entity("Supreme Court of Nepal", hits, "Nepal")["id"] == "Q2"
    assert images.pick_entity("Chief Justice", [{"id": "Q9", "label": "Rana", "description": "family name"}], "Nepal") is None
    # the newer response shape keeps the label under display
    assert images.pick_entity("Bhote Koshi", [{"id": "Q7", "display": {"label": {"value": "Bhote Koshi"}, "description": {"value": "river in Nepal"}}}], "Nepal")["id"] == "Q7"


def test_the_desk_finds_documented_photos_first_and_skips_logos_small_and_recent_ones():
    calls = []
    cands, trail = images.find_candidates(
        ["Supreme Court of Nepal building exterior 2026"],
        ALLOWED,
        _library(calls),
        country="Nepal",
        exclude={"https://commons.wikimedia.org/wiki/File:Supreme Court used last week.jpg"},
    )
    names = [c.file_name for c in cands]
    assert names[:2] == ["Supreme Court of Nepal.jpg", "Supreme Court building at dusk.jpg"]
    assert "Supreme Court of Nepal logo.png" not in names and "Supreme Court gate small.jpg" not in names
    assert "Supreme Court used last week.jpg" not in names  # rotation
    assert names.count("Supreme Court of Nepal.jpg") == 1
    first = cands[0]
    assert first.found_via == "wikidata image" and first.depicts.startswith("Supreme Court of Nepal, highest court in Nepal (Wikidata Q2)")
    # the identity gate sees how the match was made
    assert first.depicts.endswith('matched on the shorter name "Supreme Court of Nepal" while looking for "Supreme Court of Nepal building exterior"')
    assert first.description == "The Supreme Court of Nepal, Ramshahpath" and first.author == "A. Photographer" and first.width == 4000
    assert [c.found_via for c in cands] == ["wikidata image", "depicts", "commons category", "commons search"]
    assert trail == [{"subject": "Supreme Court of Nepal building exterior", "entity": "Q2 Supreme Court of Nepal", "found": {"wikidata image": 1, "depicts": 1, "commons category": 1, "commons search": 1}}]
    # the ladder asked Wikidata for the long forms first and stopped at the first real item
    searched = [u.split("search=")[1] for u in calls if "wbsearchentities" in u]
    assert searched == ["Supreme+Court+of+Nepal+building+exterior", "Supreme+Court+of+Nepal+building", "Supreme+Court+of+Nepal"]


def test_the_desk_stops_at_its_request_budget():
    calls = []
    cands, trail = images.find_candidates(["Supreme Court of Nepal"], ALLOWED, _library(calls), country="Nepal", max_requests=3)
    assert len(calls) == 3
    assert trail[0]["errors"] == ["stopped at 3 library requests"]
    assert [c.found_via for c in cands] == ["wikidata image"]


def test_a_library_outage_is_logged_and_the_desk_moves_on():
    def fetch(url):
        if "wikidata.org" in url:
            raise RuntimeError("wikidata down")
        return _library([])(url)

    cands, trail = images.find_candidates(["Supreme Court of Nepal"], ALLOWED, fetch, country="Nepal")
    assert [c.found_via for c in cands] == ["commons search"]
    assert trail[0]["errors"] == ["wikidata: RuntimeError"]


class _Editor:
    """A picture editor that answers from a script, and keeps what it was shown."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def structured(self, role, instruction, payload, schema, images=None):
        assert role == "image_picker"
        self.seen.append((payload, len(images or [])))
        return self.answers.pop(0)


def _art(**kw):
    return Article(id="2026-09-28-court", slug="court", story_id="s", headline="Supreme Court hearing", dek="d", body_markdown="", language="en", **kw)


def test_the_editor_gets_a_second_round_and_the_documentation():
    cands = [
        images.ImageCandidate("Wikimedia Commons", f"https://x/{i}.jpg", f"https://x/{i}.jpg", f"https://p/{i}", title=f"Court {i}", description=f"desc {i}", depicts="Supreme Court of Nepal", found_via="depicts", subject="Supreme Court of Nepal")
        for i in range(8)
    ]
    editor = _Editor(
        {"chosen_index": -1, "reason": "none names the court", "alt_text": ""},
        {"chosen_index": 1, "reason": "the depicts line names the court", "alt_text": "The court at dusk"},
    )
    pick = images.review_candidates(editor, _art(), cands, fetch=lambda url: (_png(1600, 1000), "image/png"), per_round=6, rounds=2)
    assert pick.candidate is cands[7] and pick.alt == "The court at dusk" and pick.rounds == 2 and pick.shown == 8
    assert pick.data  # the file already downloaded for the check is the one stored
    first, pictures = editor.seen[0]
    assert pictures == 6 and first["round"] == 1 and first["looking_for"] == ["Supreme Court of Nepal"]
    assert first["candidates"][0]["description"] == "desc 0" and first["candidates"][0]["depicts"] == "Supreme Court of Nepal"
    assert editor.seen[1][1] == 2


def test_the_desk_records_why_a_story_carries_an_illustration(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    llm = MockLLM(settings, UsageMeter(10))
    art = _art(image_brief={"search_queries": ["Nowhere in particular"], "generation_prompt": "a scene", "alt_text": "alt"})
    asset = images.make_image_for_article(
        llm, settings, art, "28 September 2026",
        fetch_json_fn=lambda url: {"search": [], "query": {"pages": {}}, "results": []},
        generate_fn=lambda prompt, s: (_png(1536, 1024), "gpt-image-1"),
    )
    assert asset.credit.kind == "generated"
    record = art.review.picture
    assert record["decision"] == "generated" and record["candidates"] == 0 and record["requests"] > 0
    assert record["reason"].startswith("No licensed photo")
    assert record["searched"][0]["subject"] == "Nowhere in particular"


def test_the_desk_records_the_photo_it_chose(tmp_path):
    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    llm = MockLLM(settings, UsageMeter(10))
    art = _art(image_brief={"search_queries": ["Supreme Court of Nepal"], "generation_prompt": "x", "alt_text": "alt"})
    asset = images.make_image_for_article(llm, settings, art, "28 September 2026", fetch_json_fn=_library([]), fetch_bytes_fn=lambda url: (_png(1600, 1066), "image/jpeg"))
    assert asset.credit.kind == "found" and asset.credit.source_url.endswith("File:Supreme Court of Nepal.jpg")
    assert asset.credit.license == "CC BY-SA 4.0" and asset.credit.author == "A. Photographer"
    record = art.review.picture
    assert record["decision"] == "found" and record["chosen"]["found_via"] == "wikidata image" and record["shown"] >= 1


def test_rotation_reads_the_recent_stories(tmp_path):
    from datetime import datetime, timezone

    from newsroom import publish
    from newsroom.models import ImageAsset

    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    credit = ImageCredit(kind="found", source="Wikimedia Commons", source_url="https://commons.wikimedia.org/wiki/File:Used.jpg", license="CC0")
    used = Article(id=f"{today}-used", slug="used", story_id="s", headline="h", dek="d", body_markdown="", language="en", run_date=today, image=ImageAsset(path="data/images/x.jpg", alt="a", width=1, height=1, credit=credit, original_url="https://upload.wikimedia.org/Used.jpg"))
    publish.save_article(settings, used)
    assert images.recent_photo_keys(settings) == {"https://upload.wikimedia.org/Used.jpg", "https://commons.wikimedia.org/wiki/File:Used.jpg"}
    assert images.recent_photo_keys(settings, article_id=used.id) == set()  # a story may keep its own photo


def test_license_rank_prefers_the_simplest_reuse():
    assert images.license_rank("CC0") == 0 and images.license_rank("Public domain") == 0
    assert images.license_rank("CC BY 4.0") == 1
    assert images.license_rank("CC BY-SA 4.0") == 2


def test_the_photos_command_dry_run_prints_the_search(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli
    from newsroom import publish

    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    art = _art(image_brief={"search_queries": ["Supreme Court of Nepal"]}, run_date="2026-09-28")
    art.image = images.store_image(_png(), art.id, settings, ImageCredit(kind="generated", model="gpt-image-1"), "alt")
    publish.save_article(settings, art)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.setattr(images, "fetch_json", _library([]))
    monkeypatch.setattr(images, "fetch_bytes", lambda url: (_png(), "image/jpeg"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert cli.main(["photos", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-28-court (carries: generated)" in out
    assert '"Supreme Court of Nepal": Q2 Supreme Court of Nepal. Found: wikidata image 1, depicts 1, commons category 2, commons search 1.' in out
    assert "[wikidata image] Supreme Court of Nepal.jpg | CC BY-SA 4.0 | 4000x2667" in out
    assert "First picture download: image/jpeg" in out
    # a dry run changes nothing
    assert publish.load_articles(settings)[0].image.credit.kind == "generated"


def test_the_photos_command_gives_a_story_a_real_photo(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli
    from newsroom import publish

    settings = dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")
    art = _art(image_brief={"search_queries": ["Supreme Court of Nepal"]}, run_date="2026-09-28")
    art.image = images.store_image(images.cover_card("h", "Nepal Wire", "d"), art.id, settings, ImageCredit(kind="cover_card"), "alt", prefer_png=True)
    publish.save_article(settings, art)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.setattr(images, "fetch_json", _library([]))
    monkeypatch.setattr(images, "fetch_bytes", lambda url: (_png(), "image/jpeg"))
    from newsroom import llm as llm_mod

    made = []
    real_make = llm_mod.make_llm
    monkeypatch.setattr(llm_mod, "make_llm", lambda s, meter=None: made.append(s.get("llm.batch.enabled")) or real_make(s, meter))
    assert settings.get("llm.batch.enabled") is True  # the edition batches
    assert cli.main(["photos"]) == 0
    assert made == [False]  # a manual photo run never waits in the batch queue
    out = capsys.readouterr().out
    assert "1 given a real photo, 0 kept what they had, 0 failed." in out
    stored = publish.load_articles(settings)[0]
    assert stored.image.credit.kind == "found" and stored.image.path.endswith(".jpg")
    assert stored.review.picture["decision"] == "found"
    assert not (tmp_path / "data/images/2026-09-28-court.png").exists()  # the old cover card is gone


def test_one_round_shows_every_subject_not_eight_shots_of_the_first():
    def cand(subject, via, n):
        return images.ImageCandidate("Wikimedia Commons", f"https://x/{subject}{n}.jpg", f"https://x/{subject}{n}.jpg", f"https://p/{subject}{n}", found_via=via, subject=subject)

    found = [cand("river", "depicts", i) for i in range(4)] + [cand("town", "commons search", 0), cand("town", "wikidata image", 1), cand("road", "openverse", 0)]
    order = [(c.subject, c.found_via) for c in images.interleave(found, ["river", "town", "road"])]
    assert order[:3] == [("river", "depicts"), ("town", "wikidata image"), ("road", "openverse")]
    assert order[3:5] == [("river", "depicts"), ("town", "commons search")]
    assert len(order) == len(found)


def test_openverse_copies_of_commons_files_are_skipped():
    data = {"results": [
        {"title": "Supreme Court of Nepal 03", "url": "https://upload.wikimedia.org/x.jpg", "foreign_landing_url": "https://commons.wikimedia.org/w/index.php?curid=1", "license": "by-sa", "license_version": "4.0", "source": "wikimedia", "provider": "wikimedia"},
        {"title": "Nuwakot relief", "url": "https://live.staticflickr.com/1.jpg", "foreign_landing_url": "https://www.flickr.com/photos/x/1", "license": "by", "license_version": "2.0", "source": "flickr", "provider": "flickr"},
    ]}
    assert [c.title for c in images.parse_openverse(data, ALLOWED)] == ["Nuwakot relief"]


def test_plural_logos_posters_and_maps_are_not_card_photos():
    def named(name):
        return images.ImageCandidate("Wikimedia Commons", "u", "u", "p", file_name=name, width=3000, height=2000)

    for name in ("Nepalese army recruiting posters.jpg", "District maps of Nepal.jpg", "Party logos.jpg", "Coats of arms.jpg", "Flags of Nepal and India.jpg"):
        assert not images.usable(named(name)), name
    for name in ("Photograph of Singha Durbar.jpg", "Mapping volunteers in Kathmandu.jpg", "Iconic Dharahara.jpg"):
        assert images.usable(named(name)), name
