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
    assert graphic.source_names(art) == ["Kathmandu Post", "Ratopati", "OnlineKhabar"]


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
