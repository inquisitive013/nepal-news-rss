import dataclasses

from PIL import Image, ImageDraw

from newsroom import graphic
from newsroom.config import load_settings
from newsroom.models import Article, ImageAsset, ImageCredit


def _settings(tmp_path):
    return dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")


def _article(settings, *, kind="found", shade=180, headline="Former chief justice arrested, police will not say why"):
    (settings.data_dir / "images").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), (shade, shade, shade)).save(settings.root / "data/images/2026-09-27-card-story.jpg")
    credit = (
        ImageCredit(kind="found", author="A. Photographer", source="Wikimedia Commons", license="CC BY-SA 4.0")
        if kind == "found"
        else ImageCredit(kind="generated", model="gpt-image-1")
    )
    return Article(
        id="2026-09-27-card-story",
        slug="card-story",
        story_id="s1",
        headline="Former Chief Justice Cholendra Rana arrested from Kathmandu home, complaint undisclosed",
        dek="d",
        body_markdown="b",
        language="en",
        sources=[{"name": "Kathmandu Post", "url": "u1"}, {"name": "Ratopati", "url": "u2"}, {"name": "OnlineKhabar", "url": "u3"}, {"name": "DC Nepal", "url": "u4"}],
        tags=["crime", "judiciary"],
        image_headline=headline,
        theme="GOVERNANCE",
        country="NEPAL",
        image=ImageAsset(path="data/images/2026-09-27-card-story.jpg", alt="a", width=1600, height=900, credit=credit),
        run_date="2026-09-27",
        published_at="2026-09-27T01:34:00+00:00",
    )


def _close(a, b, tol=14):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_card_renders_to_the_locked_frame(tmp_path):
    settings = _settings(tmp_path)
    out = graphic.render_card(settings, _article(settings), tmp_path / "site" / "cards" / "card.jpg")
    im = Image.open(out)
    assert im.size == (graphic.W, graphic.H)
    assert _close(im.getpixel((540, 8)), graphic.CRIMSON)  # header strip
    assert _close(im.getpixel((540, 1392)), graphic.DEEP_RED)  # footer strip
    assert _close(im.getpixel((20, 2)), graphic.DARK_RED)  # top left bracket
    assert _close(im.getpixel((1077, 1380)), graphic.DARK_RED)  # bottom right bracket
    assert _close(im.getpixel((540, 400)), (126, 126, 126), tol=20)  # the photo, darkened to 0.70, above the gradient
    assert im.getpixel((540, 1100))[0] < 90  # the bottom gradient has taken hold


def test_headline_autosizes_and_never_clips():
    draw = ImageDraw.Draw(Image.new("RGB", (graphic.W, graphic.H)))
    fnt, lines = graphic.fit_headline(draw, "ARMY CALLS ITS RESCUE HISTORIC, 5,285 STILL MISSING")
    assert len(lines) == 2 and 52 <= fnt.size <= 72
    assert all(graphic.text_width(draw, line, fnt) <= graphic.HEADLINE_MAX_WIDTH for line in lines)
    fnt, lines = graphic.fit_headline(draw, " ".join(["WORD"] * 40))
    assert fnt.size == 52 and len(lines) == 3  # the last resort: three lines at the floor, never a clipped one


def test_theme_country_credit_and_date(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings)
    assert graphic.theme_for(art) == "GOVERNANCE" and graphic.country_for(art) == "NEPAL"
    art.theme, art.tags = "", ["nepal-floods", "monsoon"]
    assert graphic.theme_for(art) == "DISASTER"
    art.theme = "not a theme"
    assert graphic.theme_for(art) == "DISASTER"
    assert graphic.photo_credit(art, "Nepal Wire") == "File photo: A. Photographer, CC BY-SA 4.0, via Wikimedia Commons. Adapted by Nepal Wire."
    art.image.credit.author = "unknown author"
    assert graphic.photo_credit(art, "Nepal Wire") == "File photo: CC BY-SA 4.0, via Wikimedia Commons. Adapted by Nepal Wire."
    art.image.credit.author = "A. Photographer"
    assert graphic.photo_credit(_article(settings, kind="generated"), "Nepal Wire") == "Illustration: AI generated for Nepal Wire. Not a photograph."
    assert graphic.date_label(settings, art) == "SEP 27, 2026"
    assert graphic.source_names(art) == ["Kathmandu Post", "Ratopati", "OnlineKhabar", "DC Nepal"]  # every outlet, not the first three


def test_dark_photos_are_lifted_not_darkened(tmp_path):
    settings = _settings(tmp_path)
    bright = _article(settings, shade=180)
    assert graphic.brightness_factor(Image.open(settings.root / bright.image.path)) == 0.70
    dark = _article(settings, shade=40)
    assert graphic.brightness_factor(Image.open(settings.root / dark.image.path)) == 1.0
    out = graphic.render_card(settings, dark, tmp_path / "dark.jpg")
    assert Image.open(out).getpixel((540, 300))[0] >= 30  # still identifiable, not crushed to black


def test_the_writers_line_break_gives_two_centred_lines_clear_of_the_underline(tmp_path):
    """The 28 September card drew its second line straight through the gold underline, where it read
    like a strike through "NOW SAY NO", and left both lines hanging from the left."""
    settings = _settings(tmp_path)
    art = _article(settings, shade=20, headline="EX-CJ RANA HELD 12 HOURS\nPOLICE SAY NOT AN ARREST")
    draw = ImageDraw.Draw(Image.new("RGB", (graphic.W, graphic.H)))
    fnt, lines = graphic.headline_lines(draw, art.image_headline)
    assert lines == ["EX-CJ RANA HELD 12 HOURS", "POLICE SAY NOT AN ARREST"] and 52 <= fnt.size <= 72
    im = Image.open(graphic.render_card(settings, art, tmp_path / "card.jpg")).convert("RGB")

    src_y = graphic.H - graphic.FOOTER_H - 8 - 19
    ul_bottom = src_y - 22
    band = [(x, y) for y in range(ul_bottom - 16, ul_bottom + 1) for x in range(0, graphic.W, 2)]
    assert not any(min(im.getpixel(p)) > 200 for p in band)  # no headline white on or just above the underline

    line_h = int(fnt.size * 1.18)
    top = ul_bottom - 4 - 26 - line_h * len(lines)
    for i in range(len(lines)):
        rows = range(top + i * line_h + int(fnt.size * 0.35), top + i * line_h + int(fnt.size * 0.9))
        xs = [x for y in rows for x in range(0, graphic.W) if min(im.getpixel((x, y))) > 200]
        assert xs, f"line {i + 1} not drawn where expected"
        assert abs(min(xs) - (graphic.W - 1 - max(xs))) <= 8, f"line {i + 1} is not centred"


def test_a_break_that_cannot_fit_is_rebalanced_and_one_line_stays_one():
    draw = ImageDraw.Draw(Image.new("RGB", (graphic.W, graphic.H)))
    fnt, lines = graphic.headline_lines(draw, "SHORT\n" + "AN EXTREMELY LONG SECOND LINE THAT WILL NEVER FIT")
    assert len(lines) >= 2 and all(graphic.text_width(draw, line, fnt) <= graphic.HEADLINE_MAX_WIDTH for line in lines)
    fnt, lines = graphic.headline_lines(draw, "Rain")
    assert lines == ["RAIN"]


FLOODS_29_SEP = ["Republica", "Nepal Press", "OnlineKhabar English", "Ratopati"]
RANA_27_SEP = ["Kathmandu Post", "Ratopati", "OnlineKhabar", "DC Nepal", "Nepal Press", "Khabarhub (English)", "Desh Sanchar (English)", "Nagarik News", "Constitution of Nepal 2015, Article 20(3)"]


def test_the_card_names_every_outlet_on_one_line_or_two():
    """A cap of three cut Ratopati from the 29 September floods card, and the style guide's caption for that story opens on its fact."""
    draw = ImageDraw.Draw(Image.new("RGB", (graphic.W, graphic.H)))
    fb, f = graphic.font("mono-bold", 16), graphic.font("mono", 16)
    room = graphic.W - 2 * graphic.MARGIN
    lead = graphic.text_width(draw, graphic.SOURCE_LABEL, fb) + graphic.SOURCE_GAP

    assert graphic.source_rows(draw, FLOODS_29_SEP, fb, f) == ["Republica · Nepal Press · OnlineKhabar English · Ratopati"]
    rows = graphic.source_rows(draw, RANA_27_SEP, fb, f)
    assert len(rows) == 2 and " · ".join(rows).split(" · ") == RANA_27_SEP  # all nine, most cited first
    assert lead + graphic.text_width(draw, rows[0], f) <= room and graphic.text_width(draw, rows[1], f) <= room
    assert graphic.source_rows(draw, [], fb, f) == []


def test_only_more_outlets_than_two_lines_hold_lose_any_the_least_cited_first(caplog):
    draw = ImageDraw.Draw(Image.new("RGB", (graphic.W, graphic.H)))
    fb, f = graphic.font("mono-bold", 16), graphic.font("mono", 16)
    many = [f"Outlet number {i} of a very long list" for i in range(20)]
    with caplog.at_level("WARNING"):
        rows = graphic.source_rows(draw, many, fb, f)
    kept = " · ".join(rows).split(" · ")
    assert len(rows) == 2 and 2 < len(kept) < len(many) and kept == many[: len(kept)]
    assert "left off" in caplog.text and many[-1] in caplog.text


def test_a_second_source_line_lifts_the_underline_and_clears_the_footer(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings, shade=20)
    art.sources = [{"name": name, "url": f"u{i}"} for i, name in enumerate(RANA_27_SEP)]
    im = Image.open(graphic.render_card(settings, art, tmp_path / "card.jpg")).convert("RGB")
    footer_top = graphic.H - graphic.FOOTER_H
    second = footer_top - 8 - 19
    first = second - 25
    xs = range(graphic.MARGIN, graphic.W - graphic.MARGIN)
    for top in (first, second):  # both lines drawn, off white on the dark fade
        assert any(min(im.getpixel((x, y))) > 180 for y in range(top, top + 19) for x in xs)
    assert not any(min(im.getpixel((x, y))) > 180 for y in range(second + 19, footer_top) for x in xs)
    assert _close(im.getpixel((540, first - 22 - 2)), graphic.GOLD)  # the underline sits 22px above the first line


NE_PIECE = {
    "headline": "बाढीपहिरोमा २७ जनाको मृत्यु",
    "body_markdown": "काठमाडौं । देशभर २७ जनाको मृत्यु भएको छ।",
    "image_headline": "बाढीपहिरोमा २७ जनाको मृत्यु\n७४ अर्बको क्षति भदौको बाढीको",
    "caption": {"synopsis": "पहिलो वाक्य।\nदोस्रो वाक्य।"},
}


def test_this_machine_can_shape_devanagari():
    # The Nepali card needs Raqm. If this fails on a runner, the edition falls back to English cards.
    assert graphic.can_shape_devanagari(), "Pillow has no Raqm here, or newsroom/fonts/Mukta-ExtraBold.ttf is missing"


def test_the_nepali_card_carries_the_writers_two_lines_large_with_line_two_in_gold(tmp_path):
    settings = _settings(tmp_path)
    art = _article(settings, shade=40)
    art.nepali = dict(NE_PIECE)
    assert graphic.wants_nepali_card(settings, art) and graphic.nepali_card_lines(art) == ["बाढीपहिरोमा २७ जनाको मृत्यु", "७४ अर्बको क्षति भदौको बाढीको"]
    out = graphic.render_card(settings, art, tmp_path / "site" / "cards" / "ne" / "card.jpg", language="ne")
    im = Image.open(out)
    assert im.size == (graphic.W, graphic.H)
    assert _close(im.getpixel((540, 8)), graphic.CRIMSON) and _close(im.getpixel((540, 1392)), graphic.DEEP_RED)  # the same frame
    draw = ImageDraw.Draw(im)
    fnt, lines = graphic.headline_lines(draw, NE_PIECE["image_headline"], largest=graphic.NE_LARGEST, smallest=graphic.NE_SMALLEST, kind="ne-bold", upper=False)
    assert lines == graphic.nepali_card_lines(art) and fnt.size >= 80  # well above the English card's 62
    # line two is gold: the band just above the underline holds gold letters
    ul_top = graphic.H - graphic.FOOTER_H - 8 - 19 - 22 - 4
    band = im.crop((0, ul_top - 30 - int(fnt.size * 1.22), graphic.W, ul_top - 30)).convert("RGB")
    data = band.tobytes()
    gold = sum(1 for i in range(0, len(data), 3) if _close(data[i : i + 3], graphic.GOLD, tol=40))
    assert gold > 2000


def test_no_nepali_card_without_a_checked_headline_or_without_shaping(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    art = _article(settings)
    target = tmp_path / "site" / "cards" / "ne" / "card.jpg"
    assert not graphic.wants_nepali_card(settings, art) and graphic.render_card(settings, art, target, language="ne") is None
    art.nepali = {**NE_PIECE, "body_markdown": ""}  # not a usable Nepali piece
    assert graphic.render_card(settings, art, target, language="ne") is None
    art.nepali = dict(NE_PIECE)
    monkeypatch.setattr(graphic, "can_shape_devanagari", lambda: False)
    assert graphic.render_card(settings, art, target, language="ne") is None and not target.exists()
    # the English card never depends on any of it
    assert graphic.render_card(settings, art, tmp_path / "site" / "cards" / "card.jpg").exists()
