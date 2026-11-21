"""分镜画面预览：把一个镜头的主题层、主体层和动效层叠成一张静帧。

只做预览，不追求和成片逐像素一致：视频取入点处的一帧，原创动效取 60% 处的一帧。
任何一层读不出来都跳过，整镜没有画面时画一张写着画面说明的占位卡。
"""
import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

import generated_images
import production
from store import shot_clips, video_size

PLACEHOLDER = (35, 40, 50)
CANVAS = tuple(int(production.CANVAS_COLOR[i:i + 2], 16) for i in (0, 2, 4))
SVG_MOMENT = 0.6


def preview_size(orientation, short_edge=360):
    return video_size(orientation, short_edge)


def _open(path):
    with Image.open(path) as raw:
        raw.draft("RGB", (1280, 1280))
        return ImageOps.exif_transpose(raw).convert("RGBA")


def _video_frame(path, seconds):
    with tempfile.TemporaryDirectory(prefix="vat-preview-") as scratch:
        target = Path(scratch) / "frame.png"
        for moment in (max(0.0, float(seconds or 0)) + 0.1, 0.0):
            try:
                production.ffmpeg("-ss", f"{moment:.3f}", "-i", path, "-frames:v", "1", "-update", "1", target)
            except Exception:
                continue
            if target.is_file() and target.stat().st_size:
                return _open(target)
    return None


def layer_image(path, offset=0):
    """Read one picture for a clip: an image file or a frame of a video."""
    path = Path(path)
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix in production.IMAGES:
            return _open(path)
        if suffix in production.VIDEOS:
            return _video_frame(path, offset)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return None


def _clip_path(assets, clip):
    info = assets.get(clip.get("id", ""))
    return Path(info["path"]) if info and info.get("enabled", 1) else None


def _first(assets, shot, slot):
    for clip in shot_clips(shot, slot):
        path = _clip_path(assets, clip)
        if path is None:
            continue
        try:
            offset = float(clip.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        picture = layer_image(path, offset)
        if picture is not None:
            return clip, picture
    return None, None


def _cover(picture, size):
    return ImageOps.fit(picture, size, Image.Resampling.LANCZOS)


def _place(board, picture, rect=None):
    left, top, width, height = production.layer_box(board.size, rect)
    fitted = ImageOps.contain(picture, (max(1, width), max(1, height)), Image.Resampling.LANCZOS)
    x = left + (width - fitted.width) // 2
    y = top + (height - fitted.height) // 2
    board.alpha_composite(fitted, (x, y))


def _svg_picture(svg, size):
    from creative_art import Artwork
    art = Artwork(svg)
    data = art.frame(size, SVG_MOMENT if art.animations else 0, art.font_options())
    with Image.open(BytesIO(data)) as raw:
        return raw.convert("RGBA")


def placeholder(size, number, visual, note="自动配图"):
    from shot_visuals import font, lines_for
    board = Image.new("RGBA", size, PLACEHOLDER + (255,))
    draw = ImageDraw.Draw(board)
    w, h = size
    edge = min(w, h)
    pad = max(12, edge // 12)
    draw.rounded_rectangle((pad // 2, pad // 2, w - pad // 2, h - pad // 2), radius=max(6, edge // 24),
                           outline=(84, 94, 112), width=max(1, edge // 180))
    small, body = font(max(11, edge // 20), True), font(max(12, edge // 16))
    draw.text((pad, pad), f"镜头 {number:02d} · {note}", font=small, fill=(150, 162, 182))
    lines = lines_for(" ".join(str(visual or "").split()) or "还没有画面说明", body, w - 2 * pad)
    if len(lines) > 6:
        lines = lines[:6]
        lines[-1] = lines[-1][:-1] + "…"
    step = round(body.size * 1.5)
    y = max(pad * 2 + small.size, (h - step * len(lines)) // 2)
    for line in lines:
        draw.text((pad, y), line, font=body, fill=(226, 231, 239))
        y += step
    return board


def describe(project, assets, shot):
    """Return the text summary of which layers the still uses."""
    art = shot.get("art") or {}
    parts = []
    if shot_clips(shot, "theme"):
        parts.append("主题")
    if shot_clips(shot, "main"):
        parts.append("口播" if shot.get("kind") == "talk" else "主体")
    elif art.get("image_file") and (art.get("layer") == "effect" or not art.get("svg")):
        parts.append("AI 生图")
    if art.get("svg") and (art.get("layer") == "effect" or not shot_clips(shot, "main")):
        parts.append("原创动效")
    if shot_clips(shot, "effect"):
        parts.append("动效")
    return " + ".join(parts)


def shot_still(root, project, assets, index, size=None):
    """Compose a preview still for shot `index`; never raises for missing media."""
    shot = project["shots"][index]
    size = size or preview_size(project.get("orientation", ""))
    board = Image.new("RGBA", size, CANVAS + (255,))
    art = shot.get("art") or {}
    drawn = False

    _, theme = _first(assets, shot, "theme")
    if theme is not None:
        board.alpha_composite(_cover(theme, size))
        drawn = True

    clip, main = _first(assets, shot, "main")
    if main is not None:
        try:
            rect = production.normalized_rect(clip.get("rect"))
        except ValueError:
            rect = None
        _place(board, main, rect)
        drawn = True

    is_effect = art.get("layer") == "effect"
    if art.get("image_file") and main is None and (is_effect or not art.get("svg")):
        try:
            picture = layer_image(generated_images.file_path(root, project["id"], art["image_file"]))
        except ValueError:
            picture = None
        if picture is not None:
            _place(board, picture)
            drawn = True
    if art.get("svg") and (is_effect or main is None):
        try:
            board.alpha_composite(_svg_picture(art["svg"], size))
            drawn = True
        except Exception:
            pass

    _, effect = _first(assets, shot, "effect")
    if effect is not None:
        _place(board, effect)
        drawn = True

    if not drawn:
        note = "真人出镜 · 待选口播视频" if shot.get("kind") == "talk" else "自动配图"
        return placeholder(size, index + 1, shot.get("visual", ""), note).convert("RGB")
    return board.convert("RGB")


def signature(project, assets, index):
    """Cache key that changes when the shot or any referenced file changes."""
    shot = project["shots"][index]
    files = []
    for slot in production.LAYER_ORDER:
        for clip in shot_clips(shot, slot):
            path = _clip_path(assets, clip)
            try:
                stamp = path.stat().st_mtime_ns if path else 0
            except OSError:
                stamp = 0
            files.append([str(path), stamp])
    blob = json.dumps([shot, files, project.get("orientation", "")], ensure_ascii=False, sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
