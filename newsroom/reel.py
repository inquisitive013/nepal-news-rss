"""The Facebook Reel: the story's picture moving slowly, its Nepali lines one beat at a time, its sources at the end.

1080x1920 at 30 frames a second, H.264 with a silent AAC track, drawn with Pillow and encoded
with ffmpeg. A photo post reaches the people Facebook shows the Page to; on 30 September that
was one or two viewers a post against 10,334 followers. Facebook shows Reels to people who do
not follow the Page, so the Reel is how a cold Page gets seen at all.

The beats come from a file the editor writes, `data/reels/<article id>.json`, holding only lines
the verified record carries: the checked card headline, the sourced numbers, the caption's
question. The end card names every outlet and the picture's credit, as the card does.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter

from . import graphic as g
from .config import Settings
from .models import Article

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
TEXT_WIDTH = 940
# Facebook lays the Page name, the caption and the buttons over the bottom and the right of a Reel,
# so the lines sit between these two heights.
TEXT_TOP, TEXT_BOTTOM = 330, 1250
FADE = 0.3  # seconds each beat takes to appear and to go
END_SECONDS = 4.5
ZOOM = 0.10  # the picture grows by a tenth over the whole Reel
COLOURS = {"white": g.WHITE, "gold": g.GOLD, "off": g.OFF_WHITE}


class ReelError(RuntimeError):
    pass


@dataclass
class Line:
    text: str
    size: int = 84
    colour: str = "white"


@dataclass
class Beat:
    lines: list[Line] = field(default_factory=list)
    seconds: float = 4.0


def reel_path(settings: Settings, article_id: str) -> Path:
    return settings.data_dir / "reels" / f"{article_id}.json"


def load_beats(settings: Settings, article_id: str) -> tuple[list[Beat], str]:
    """The editor's beats for this story and the Reel's caption. Raises when there is no file."""
    path = reel_path(settings, article_id)
    if not path.exists():
        raise ReelError(f"no beats for {article_id}: write {path.relative_to(settings.root)} first")
    raw = json.loads(path.read_text(encoding="utf-8"))
    beats = []
    for b in raw.get("beats") or []:
        lines = [Line(str(t), int(s), str(c)) for t, s, c in b.get("lines") or []]
        if lines:
            beats.append(Beat(lines, float(b.get("seconds") or 4.0)))
    if not beats:
        raise ReelError(f"{path.name} has no beats")
    return beats, str(raw.get("caption") or "").strip()


def _shadowed(layer: Image.Image, xy: tuple[int, int], text: str, fnt, fill) -> None:
    """Text over a soft shadow on a transparent layer, so a bright patch of picture never eats it."""
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((xy[0] + 3, xy[1] + 5), text, font=fnt, fill=(0, 0, 0, 200))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    ImageDraw.Draw(layer).text(xy, text, font=fnt, fill=fill)


def beat_layer(beat: Beat) -> Image.Image:
    """One beat's lines, wrapped to the text width, centred in the band clear of Facebook's overlay."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    rows: list[tuple[str, Any, tuple[int, int, int], int]] = []
    for line in beat.lines:
        fnt = g.font("ne-bold", line.size)
        for part in g.wrap(draw, line.text, fnt, TEXT_WIDTH):
            rows.append((part, fnt, COLOURS.get(line.colour, g.WHITE), int(line.size * 1.3)))
    total = sum(h for *_, h in rows)
    y = TEXT_TOP + max(0, (TEXT_BOTTOM - TEXT_TOP - total) // 2)
    for text, fnt, fill, h in rows:
        _shadowed(layer, ((W - g.text_width(draw, text, fnt)) // 2, y), text, fnt, fill)
        y += h
    return layer


def frame_layer(settings: Settings, article: Article) -> Image.Image:
    """What stays on screen throughout: the brand strip at the top, the picture's credit under the lines."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([0, 0, W, 120], fill=g.CRIMSON)
    x = 48
    mark = g.brand_mark(64)
    if mark is not None:
        layer.alpha_composite(mark, (x, 28))
        x += mark.width + 20
    # The name alone: the card's theme label, VIRAL on 30 September, reads as bait on a Reel.
    f_brand = g.font("sans-bold", 40)
    draw.text((x, 36), settings.site_name.upper(), font=f_brand, fill=g.WHITE)
    credit = g.photo_credit(article, settings.site_name)
    if credit:
        f_credit = g.font("mono", 26)
        for i, part in enumerate(g.wrap(draw, credit, f_credit, W - 96)[:2]):
            draw.text(((W - g.text_width(draw, part, f_credit)) // 2, TEXT_BOTTOM + 40 + i * 34), part, font=f_credit, fill=g.LIGHT_GRAY)
    return layer


def end_layer(settings: Settings, article: Article) -> Image.Image:
    """The last card: the mark, the name, every outlet the story used and the picture's credit."""
    layer = Image.new("RGBA", (W, H), (8, 8, 8, 235))
    draw = ImageDraw.Draw(layer)
    mark = g.brand_mark(200)
    y = 470
    if mark is not None:
        layer.alpha_composite(mark, ((W - mark.width) // 2, y))
        y += mark.height + 50
    f_name = g.font("sans-bold", 72)
    name = settings.site_name.upper()
    draw.text(((W - g.text_width(draw, name, f_name)) // 2, y), name, font=f_name, fill=g.WHITE)
    y += 150
    f_label, f_src = g.font("mono-bold", 30), g.font("mono", 30)
    draw.text(((W - g.text_width(draw, g.SOURCE_LABEL, f_label)) // 2, y), g.SOURCE_LABEL, font=f_label, fill=g.GOLD)
    y += 56
    for part in name_rows(draw, g.source_names(article) or [settings.site_name], f_src, W - 120):
        draw.text(((W - g.text_width(draw, part, f_src)) // 2, y), part, font=f_src, fill=g.OFF_WHITE)
        y += 44
    credit = g.photo_credit(article, settings.site_name)
    if credit:
        y += 40
        f_credit = g.font("mono", 24)
        for part in g.wrap(draw, credit, f_credit, W - 120):
            draw.text(((W - g.text_width(draw, part, f_credit)) // 2, y), part, font=f_credit, fill=g.LIGHT_GRAY)
            y += 34
    return layer


def name_rows(draw: ImageDraw.ImageDraw, names: list[str], fnt, max_width: int) -> list[str]:
    """The outlets joined by middle dots, a new line only between two names, never inside one."""
    rows: list[list[str]] = []
    for name in names:
        if rows and g.text_width(draw, " · ".join(rows[-1] + [name]), fnt) <= max_width:
            rows[-1].append(name)
        else:
            rows.append([name])
    return [" · ".join(row) for row in rows]


def background(settings: Settings, article: Article) -> Image.Image:
    """The story's picture, cropped to cover the tall frame with room to zoom, toned and darkened for the lines."""
    if not article.image:
        raise ReelError(f"{article.id} has no picture")
    path = settings.root / article.image.path
    if not path.exists():
        raise ReelError(f"picture missing: {article.image.path}")
    bw, bh = int(W * (1 + ZOOM)) + 2, int(H * (1 + ZOOM)) + 2
    img = Image.open(path).convert("RGB")
    if article.image.credit.kind == "generated":
        # The illustration model writes its own label along the bottom edge; the card crops it
        # away, the tall Reel frame would show it (30 September, the Mukesh Pal picture).
        img = img.crop((0, 0, img.width, int(img.height * 0.94)))
    img = g.treat(g.cover_crop(img, bw, bh))
    shade = Image.new("RGBA", img.size, (0, 0, 0, 120))
    return Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - no encoder means no Reel, never a failed edition
        raise ReelError("no ffmpeg here: install it or the imageio-ffmpeg package") from exc


def timeline(beats: list[Beat]) -> list[tuple[float, float]]:
    """Each beat's start and end in seconds, then the end card's."""
    spans, t = [], 0.0
    for beat in beats:
        spans.append((t, t + beat.seconds))
        t += beat.seconds
    spans.append((t, t + END_SECONDS))
    return spans


def render_reel(settings: Settings, article: Article, beats: list[Beat], out_path: Path) -> Path:
    """Draw every frame and encode the Reel to out_path (MP4). Returns the path."""
    bg = background(settings, article)
    fixed = frame_layer(settings, article)
    layers = [beat_layer(b) for b in beats] + [end_layer(settings, article)]
    spans = timeline(beats)
    total = spans[-1][1]
    frames = int(round(total * FPS))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for n in range(frames):
            t = n / FPS
            grow = 1 + ZOOM * (t / total)  # the whole oversized picture at the start, the frame's own size at the end
            cw, ch = int(bg.width / grow), int(bg.height / grow)
            left, top = (bg.width - cw) // 2, (bg.height - ch) // 3
            frame = bg.crop((left, top, left + cw, top + ch)).resize((W, H), Image.BILINEAR).convert("RGBA")
            if t < spans[-1][0]:
                frame.alpha_composite(fixed)  # the end card carries its own name and credit
            for i, (layer, (start, end)) in enumerate(zip(layers, spans)):
                if start <= t < end:
                    # The first frame is the one Facebook shows before anyone taps: the hook is on it.
                    fade_in = 1.0 if i == 0 else (t - start) / FADE
                    fade_out = 1.0 if layer is layers[-1] else (end - t) / FADE
                    alpha = min(1.0, fade_in, fade_out)
                    shown = Image.alpha_composite(frame, layer)
                    frame = shown if alpha >= 1.0 else Image.blend(frame, shown, max(0.0, alpha))
                    break
            proc.stdin.write(frame.convert("RGB").tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise ReelError(f"ffmpeg failed: {err.strip()[-400:]}")
    log.info("reel for %s: %.1f seconds, %d frames, %s", article.id, total, frames, out_path)
    return out_path


def publish_reel(client: httpx.Client, environ: Mapping[str, str], video: Path, caption: str) -> tuple[str, str]:
    """Put the Reel on the Page in Meta's three steps: open an upload, send the file, publish it.

    The steps and the limits (9:16, at least 540x960, 23 frames a second or more, 4 to 60
    seconds) are Meta's own, from its Reels Publishing API sample collection on github.com/fbsamples.
    Returns the video id and the Reel's address.
    """
    from . import social

    fb = social.facebook_page(client, environ)
    version = social._graph_version(environ)
    base = f"{social.META_GRAPH}/{version}/{fb.id}/video_reels"
    opened = social._raise_for(client.post(base, data={"upload_phase": "start", "access_token": fb.token}), "Facebook Reel")
    video_id = str(opened.get("video_id") or "")
    if not video_id:
        raise social.SocialError("Facebook Reel: the upload did not open")
    body = Path(video).read_bytes()
    upload_url = str(opened.get("upload_url") or f"https://rupload.facebook.com/video-upload/{version}/{video_id}")
    headers = {"Authorization": f"OAuth {fb.token}", "offset": "0", "file_size": str(len(body)), "Content-Type": "application/octet-stream"}
    social._raise_for(client.post(upload_url, content=body, headers=headers, timeout=300), "Facebook Reel upload")
    done = social._raise_for(client.post(base, data={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED", "description": caption, "access_token": fb.token}), "Facebook Reel")
    if done.get("success") is False:
        raise social.SocialError("Facebook Reel: Meta did not publish it")
    url = ""
    try:  # the address is a courtesy: Meta may still be processing the video
        url = str(social._raise_for(client.get(f"{social.META_GRAPH}/{version}/{video_id}", params={"fields": "permalink_url", "access_token": fb.token}), "Facebook Reel").get("permalink_url") or "")
    except social.SocialError:
        pass
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    return video_id, url
