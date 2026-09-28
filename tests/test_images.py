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
