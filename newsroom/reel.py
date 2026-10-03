"""The Facebook Reel: the story's picture moving slowly, its Nepali lines one beat at a time, its sources at the end.

1080x1920 at 30 frames a second, H.264 with AAC, drawn with Pillow and encoded with ffmpeg. The
sound is the editor's Nepali narration, one clip a beat, over a music bed that ducks under the
voice; a Reel with neither carries a silent track. A photo post reaches the people Facebook shows the Page to; on 30 September that
was one or two viewers a post against 10,334 followers. Facebook shows Reels to people who do
not follow the Page, so the Reel is how a cold Page gets seen at all.

The beats come from a file the editor writes, `data/reels/<article id>.json`, holding only lines
the verified record carries: the checked card headline, the sourced numbers, the caption's
question. Each beat may name its narration clip and the words it says; a beat stretches to fit
its clip. The end card names every outlet and the picture's credit, as the card does. The same
file goes to Instagram as a Reel when the Instagram secrets are set, and to YouTube (`youtube.py`).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter

from . import graphic as g
from . import images
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
VOICE_LEAD, VOICE_TAIL = 0.2, 0.45  # seconds of quiet before a beat's clip and after it
BED_VOLUME = 0.22  # the music under the voice, before it ducks
LOUDNESS = -14  # integrated loudness in LUFS, where phone feeds play
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
    say: str = ""  # the narration's words, kept beside the clip so the record shows what the voice says
    audio: Path | None = None


@dataclass
class Sound:
    """What the Reel sounds like besides each beat's own clip: the sign off over the end card, and the bed."""

    end: Path | None = None
    music: Path | None = None
    music_volume: float = BED_VOLUME


def reel_path(settings: Settings, article_id: str) -> Path:
    return settings.data_dir / "reels" / f"{article_id}.json"


def _raw(settings: Settings, article_id: str) -> tuple[Path, dict[str, Any]]:
    path = reel_path(settings, article_id)
    if not path.exists():
        raise ReelError(f"no beats for {article_id}: write {path.relative_to(settings.root)} first")
    return path, json.loads(path.read_text(encoding="utf-8"))


def _clip(settings: Settings, name: Any, where: str) -> Path | None:
    """A sound file the beats name, relative to the repository root. A named file that is missing stops the Reel."""
    if not name:
        return None
    path = settings.root / str(name)
    if not path.exists():
        raise ReelError(f"{where} names {name}, which is not there")
    return path


def audio_seconds(path: Path) -> float:
    """How long a sound file plays, read from ffmpeg's report."""
    report = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", report)
    if not found:
        raise ReelError(f"cannot read the length of {path.name}")
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def load_beats(settings: Settings, article_id: str) -> tuple[list[Beat], str]:
    """The editor's beats for this story and the Reel's caption. Raises when there is no file.

    A beat with a narration clip lasts at least as long as the clip, with a little quiet on
    either side, so the words never run into the next beat's lines.
    """
    path, raw = _raw(settings, article_id)
    beats = []
    for n, b in enumerate(raw.get("beats") or [], start=1):
        lines = [Line(str(t), int(s), str(c)) for t, s, c in b.get("lines") or []]
        if not lines:
            continue
        beat = Beat(lines, float(b.get("seconds") or 4.0), str(b.get("say") or "").strip(), _clip(settings, b.get("audio"), f"{path.name} beat {n}"))
        if beat.audio is not None:
            if not beat.say:
                raise ReelError(f"{path.name} beat {n} has a clip but not the words it says")
            beat.seconds = max(beat.seconds, round(VOICE_LEAD + audio_seconds(beat.audio) + VOICE_TAIL, 2))
        beats.append(beat)
    if not beats:
        raise ReelError(f"{path.name} has no beats")
    return beats, str(raw.get("caption") or "").strip()


def load_sound(settings: Settings, article_id: str) -> Sound:
    """The sign off and the music bed the beats file names, if any."""
    path, raw = _raw(settings, article_id)
    end = _clip(settings, raw.get("end_audio"), f"{path.name} end card")
    if end is not None and not str(raw.get("end_say") or "").strip():
        raise ReelError(f"{path.name} has an end clip but not the words it says")
    return Sound(end, _clip(settings, raw.get("music"), f"{path.name} music"), float(raw.get("music_volume") or BED_VOLUME))


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
    elif settings.get("images.burn_credit", True) and images.burns_credit(article.image.credit.line()):
        # A stored photo carries its credit burned along the bottom. The tall frame showed the bar
        # cut off at the edge (1 October, the Bhotekoshi valley), and the Reel prints the credit itself.
        img = img.crop((0, 0, img.width, img.height - images.credit_bar_height(img.height)))
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


def sound_args(beats: list[Beat], spans: list[tuple[float, float]], sound: Sound | None) -> tuple[list[str], list[str]]:
    """ffmpeg's audio inputs and its mapping: each clip at its beat's start, the bed ducked under the voice.

    The whole mix is brought to one loudness for phones. With no clip and no bed the track is silent.
    """
    total = spans[-1][1]
    clips = [(b.audio, spans[i][0]) for i, b in enumerate(beats) if b.audio is not None]
    if sound is not None and sound.end is not None:
        clips.append((sound.end, spans[-1][0]))
    music = sound.music if sound is not None else None
    if not clips and music is None:
        return ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"], ["-map", "0:v", "-map", "1:a", "-shortest"]
    inputs: list[str] = []
    graph: list[str] = []
    voices: list[str] = []
    for n, (path, start) in enumerate(clips, start=1):
        inputs += ["-i", str(path)]
        ms = int(round((start + VOICE_LEAD) * 1000))
        graph.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[v{n}]")
        voices.append(f"[v{n}]")
    if voices:
        graph.append(f"{''.join(voices)}amix=inputs={len(voices)}:duration=longest:normalize=0[voice]")
    last = "[voice]"
    if music is not None:
        m = len(clips) + 1
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        volume = sound.music_volume if sound is not None else BED_VOLUME
        graph.append(f"[{m}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume={volume},afade=t=in:d=1,afade=t=out:st={max(0.0, total - 2):.3f}:d=2[bed]")
        if voices:
            # The voice is padded to the full length: the ducking stops when its key ends, and until
            # 1 October the bed stopped with the last word and the end card played in silence.
            graph += [f"[voice]apad=whole_dur={total:.3f},asplit=2[said][key]", "[bed][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[ducked]", "[ducked][said]amix=inputs=2:duration=longest:normalize=0[mix]"]
            last = "[mix]"
        else:
            last = "[bed]"
    graph.append(f"{last}apad,atrim=0:{total:.3f},loudnorm=I={LOUDNESS}:TP=-1.5:LRA=11,aresample=48000[aout]")
    return inputs, ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[aout]", "-t", f"{total:.3f}"]


def render_reel(settings: Settings, article: Article, beats: list[Beat], out_path: Path, sound: Sound | None = None) -> Path:
    """Draw every frame and encode the Reel, with its sound, to out_path (MP4). Returns the path."""
    bg = background(settings, article)
    fixed = frame_layer(settings, article)
    layers = [beat_layer(b) for b in beats] + [end_layer(settings, article)]
    spans = timeline(beats)
    total = spans[-1][1]
    frames = int(round(total * FPS))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_inputs, mapping = sound_args(beats, spans, sound)
    cmd = [
        _ffmpeg(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        *audio_inputs, *mapping,
        # Instagram's Reels rules are the strict ones: the index at the front, no edit lists, AAC at
        # 128 kbps and 48 kHz (Meta's IG User Media reference). On 3 October Instagram refused a Reel
        # Facebook had taken, whose file carried two edit lists and 160 kbps sound. Without B frames
        # the video needs no edit list to start at zero.
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high", "-bf", "0",
        "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", "-use_editlist", "0",
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


def facebook_video_file(client: httpx.Client, environ: Mapping[str, str], video_id: str, *, tries: int = 12, delay: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> str:
    """The address of a Facebook Reel's own video file, once Meta has processed it; empty when it never says.

    Instagram can fetch a Reel from an address when it refuses the direct upload, and this one
    needs no hosting of our own.
    """
    from . import social

    fb = social.facebook_page(client, environ)
    url = f"{social.META_GRAPH}/{social._graph_version(environ)}/{video_id}"
    for attempt in range(tries):
        try:
            source = str(social._raise_for(client.get(url, params={"fields": "source", "access_token": fb.token}), "Facebook Reel").get("source") or "")
        except social.SocialError:
            source = ""
        if source.startswith("https://"):
            return source
        if attempt < tries - 1:
            sleep(delay)
    return ""


def wait_until_served(client: httpx.Client, url: str, timeout_s: float = 180.0, *, interval: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> bool:
    """True once the address answers 200 to a HEAD request, so a video is never downloaded just to see it is there."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if client.head(url).status_code == 200:
                return True
        except httpx.HTTPError as exc:
            log.debug("waiting for %s: %s", url, exc)
        if time.monotonic() >= deadline:
            return False
        sleep(interval)


def publish_instagram_reel(
    client: httpx.Client,
    environ: Mapping[str, str],
    video: Path,
    caption: str,
    *,
    video_url: str = "",
    fallback_url: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Put the Reel on Instagram: open a Reels container, give it the video, publish it when Meta has processed it.

    With `video_url`, the address where the site serves this very file, Instagram fetches it from
    there: Meta's documented way, and the one that works for this app. Without one, the file
    goes up by Meta's resumable upload to rupload.facebook.com, which refused this app's Page token
    on 3 October ("Request processing failed"), and then the container fetches `fallback_url()`,
    the Facebook Reel's own file. The account must be a professional one linked to the Page.
    Returns the media id and the post's address.
    """
    from . import social

    version = social._graph_version(environ)
    base = environ.get("INSTAGRAM_API_BASE", "").strip() or f"{social.META_GRAPH}/{version}"
    user, token = environ["INSTAGRAM_USER_ID"], environ["INSTAGRAM_ACCESS_TOKEN"]
    fields = {"media_type": "REELS", "caption": social.fit(caption, social.LIMITS["instagram"]), "share_to_feed": "true", "access_token": token}
    if video_url:
        opened = social._raise_for(client.post(f"{base}/{user}/media", data={**fields, "video_url": video_url}), "Instagram Reel")
        container = str(opened.get("id") or "")
        if not container:
            raise social.SocialError("Instagram Reel: no container came back")
    else:
        container = _instagram_upload(client, social, base, version, user, token, fields, video, fallback_url)
    # Meta processes a video for up to a few minutes before it can be published.
    social._poll_container(client, f"{base}/{container}", {"fields": "status_code,status", "access_token": token}, what="Instagram Reel", tries=30, delay=10.0, sleep=sleep)
    published = social._raise_for(client.post(f"{base}/{user}/media_publish", data={"creation_id": container, "access_token": token}), "Instagram Reel")
    media_id = str(published.get("id") or "")
    if not media_id:
        raise social.SocialError("Instagram Reel: Meta did not publish it")
    permalink = ""
    try:  # the address is a courtesy
        permalink = str(social._raise_for(client.get(f"{base}/{media_id}", params={"fields": "permalink", "access_token": token}), "Instagram Reel").get("permalink") or "")
    except social.SocialError:
        pass
    return media_id, permalink


def _instagram_upload(client, social, base: str, version: str, user: str, token: str, fields: dict[str, str], video: Path, fallback_url: Callable[[], str] | None) -> str:
    """A container for the file sent by resumable upload, or for the fallback address when Meta refuses that. Returns its id."""
    try:
        opened = social._raise_for(client.post(f"{base}/{user}/media", data={**fields, "upload_type": "resumable"}), "Instagram Reel")
        container = str(opened.get("id") or "")
        if not container:
            raise social.SocialError("Instagram Reel: the upload did not open")
        body = Path(video).read_bytes()
        upload_url = str(opened.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{version}/{container}")
        headers = {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(body)), "Content-Type": "application/octet-stream"}
        social._raise_for(client.post(upload_url, content=body, headers=headers, timeout=300), "Instagram Reel upload")
    except social.SocialError as refused:
        address = fallback_url() if fallback_url else ""
        if not address:
            raise
        log.info("Instagram refused the direct upload (%s); it fetches the Facebook Reel's file instead", str(refused)[:160])
        opened = social._raise_for(client.post(f"{base}/{user}/media", data={**fields, "video_url": address}), "Instagram Reel")
        container = str(opened.get("id") or "")
        if not container:
            raise social.SocialError("Instagram Reel: no container came back") from refused
    return container

