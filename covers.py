"""Compose the cover image that ships with the finished video."""

import json
import math
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

import generated_images
import production
import review
from shot_visuals import font, lines_for
from store import uid


SIZES = {"竖屏 9∶16": (1080, 1920), "竖屏 3∶4": (1080, 1440), "横屏 16∶9": (1920, 1080)}
STYLE_NAMES = ("黑金冲击", "简约大字", "暗黑科技", "暖色纪实")
COVER_FILES = re.compile(r"cover-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\.png")
BACKGROUND_FILES = re.compile(r"bg-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\.jpg")
PROJECT_IDS = re.compile(r"[\w-]+")
MAX_BACKGROUND_PIXELS = 40_000_000


@dataclass(frozen=True)
class CoverStyle:
    paper: str
    accent: str
    title: str
    muted: str
    scrim: str


STYLES = {
    "黑金冲击": CoverStyle("#0B0804", "#FFD033", "#FFF3DC", "#C9B383", "#0B0804"),
    "简约大字": CoverStyle("#14171A", "#F2C14E", "#F8F8F6", "#C3C9CC", "#0F1113"),
    "暗黑科技": CoverStyle("#0C1417", "#C5F65D", "#EEF6EF", "#A5BCB5", "#07100F"),
    "暖色纪实": CoverStyle("#3A2A1E", "#E8A75C", "#FFF7EA", "#E3CDB6", "#291B11"),
}
TONE = {
    "黑金冲击": "高对比度的近黑色背景，少量琥珀色光斑点缀，像知识类短视频的封面底图",
    "简约大字": "干净克制的现代平面质感，深灰底色、柔和渐变与大面积留白",
    "暗黑科技": "暗黑科技质感，深青黑底色、荧光黄绿色微光与细密网格",
    "暖色纪实": "暖色调的摄影质感，纸张、木纹与侧逆光，生活纪实氛围",
}


def dimensions(size_name):
    if size_name not in SIZES:
        raise ValueError("请选择封面画幅。")
    return SIZES[size_name]


def style_of(style_name):
    if style_name not in STYLES:
        raise ValueError("请选择封面风格。")
    return STYLES[style_name]


def rgb(value):
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def project_dir(root, project_id):
    if not PROJECT_IDS.fullmatch(project_id or ""):
        raise ValueError("项目引用无效。")
    return Path(root).resolve() / "封面" / project_id


def file_path(root, project_id, filename):
    if not COVER_FILES.fullmatch(filename or ""):
        raise ValueError("封面文件引用无效。")
    return project_dir(root, project_id) / filename


def background_path(root, project_id, filename):
    if not BACKGROUND_FILES.fullmatch(filename or ""):
        raise ValueError("封面背景引用无效。")
    return project_dir(root, project_id) / filename


SCHEMES = ("主体偏左，右侧留出干净的暗部", "主体居中，上下留出干净的暗部", "主体偏右，左侧留出干净的暗部")


def background_prompt(title, label="", style_name=STYLE_NAMES[0]):
    subject = "、".join(part.strip() for part in (label, title) if part and part.strip()) or "这一期视频"
    tone = TONE.get(style_name, TONE[STYLE_NAMES[0]])
    return (f"为一条中文短视频绘制封面背景，主题是「{subject}」。{tone}。"
            "画面里不要出现任何文字、数字、字母、字幕、水印或软件界面元素；"
            "主体不要占满画面，留出大块干净的深色区域，用来叠加封面标题。")


def background_prompts(title, label="", style_name=STYLE_NAMES[0], direction=""):
    """Three compositions per request so the editor can pick a frame that fits the title."""
    base = background_prompt(title, label, style_name)
    direction = " ".join(str(direction or "").split())[:1000]
    if direction:
        base += f"补充画面要求：{direction}。"
    return [f"{base}构图：{scheme}。" for scheme in SCHEMES]


def video_candidates(root, project):
    """Return finished and sample videos, newest first, for frame grabbing."""
    found = []
    for record in reversed(project.get("exports") or []):
        path = Path(record.get("path", ""))
        if record.get("kind") in ("完整成片", "无字幕画面") and path.suffix.lower() in production.VIDEOS:
            if path.is_file():
                found.append({"kind": "成片", "path": path, "label": f"成片 · {path.name}"})
    for record in reversed(project.get("previews") or []):
        version = str(record.get("version", ""))
        if not PROJECT_IDS.fullmatch(version):
            continue
        path = review.version_dir(root, project["id"], version) / review.SAMPLE
        if path.is_file():
            found.append({"kind": "样片", "path": path, "label": f"样片 · {record.get('created_at', version)}"})
    unique, seen = [], set()
    for item in found:
        key = str(item["path"].resolve())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def extract_frame(video, seconds, destination):
    """Write one frame of a video to a lossless PNG."""
    video = Path(video)
    if not video.is_file():
        raise ValueError("找不到取帧的视频，请先合成样片或导出成片。")
    try:
        position = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("请填写取帧位置的秒数。") from None
    if not math.isfinite(position) or position < 0:
        raise ValueError("取帧位置须为非负秒数。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    production.ffmpeg("-ss", f"{position:.3f}", "-i", video, "-frames:v", "1", "-update", "1", destination)
    if not destination.is_file() or not destination.stat().st_size:
        raise ValueError("这一时间点没有画面，请换一个时间点。")
    return destination


def keep_background(root, project, source, size_name):
    """Copy a background into the project folder, capped at cover resolution."""
    source = Path(source)
    if not source.is_file():
        raise ValueError("找不到这张背景图片，可能已经移动。")
    width, height = dimensions(size_name)
    try:
        with Image.open(source) as raw:
            picture = ImageOps.exif_transpose(raw).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError(f"无法读取背景图片「{source.name}」。") from error
    if picture.width * picture.height > MAX_BACKGROUND_PIXELS:
        raise ValueError("背景图片超过 4000 万像素，请先缩小再选。")
    folder = project_dir(root, project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    name = f"bg-{datetime.now():%Y%m%d-%H%M%S}-{uid()[:4]}.jpg"
    limit = round(max(width, height) * 1.6)
    picture.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    picture.save(folder / name, "JPEG", quality=91, optimize=True)
    return name


def frame_background(root, project, video, seconds, size_name):
    folder = project_dir(root, project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".frame-", dir=folder) as scratch:
        frame = extract_frame(video, seconds, Path(scratch) / "frame.png")
        return keep_background(root, project, frame, size_name)


def ai_backgrounds(root, project, settings, key, title, label, style_name, size_name, report=None, *,
                   direction=""):
    """Draw the candidate backgrounds; generated files stay in the project's 原创素材."""
    prompts = background_prompts(title, label, style_name, direction)
    paths, errors = [], []
    for index, prompt in enumerate(prompts, 1):
        if report:
            report(f"正在绘制第 {index} / {len(prompts)} 张封面背景")
        try:
            filename = generated_images.create(root, {**project, "orientation": size_name}, prompt, settings, key)
        except ValueError as error:
            errors.append(str(error))
            continue
        paths.append(generated_images.file_path(root, project["id"], filename))
    if not paths:
        raise ValueError(errors[0] if errors else "生图接口没有返回图片。")
    if errors and report:
        report(f"有 {len(errors)} 张没画出来，先看已生成的 {len(paths)} 张。")
    return paths


def _photo(background, width, height):
    path = Path(background)
    if not path.is_file():
        raise ValueError("找不到封面背景，可能已经移动；请重新取帧或选图。")
    try:
        with Image.open(path) as raw:
            picture = ImageOps.exif_transpose(raw).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError(f"无法读取背景图片「{path.name}」。") from error
    fitted = ImageOps.fit(picture, (width, height), Image.Resampling.LANCZOS, centering=(.5, .44))
    return fitted.convert("RGBA")


def _palette(width, height, style_name, design):
    layer = Image.new("RGBA", (width, height), design.paper)
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    accent = rgb(design.accent)
    short = min(width, height)
    inset = round(short * .045)
    hairline = max(1, short // 900)
    if style_name == "暗黑科技":
        step = max(28, short // 11)
        for x in range(step, width, step):
            draw.line((x, 0, x, height), fill=(255, 255, 255, 9))
        for y in range(step, height, step):
            draw.line((0, y, width, y), fill=(255, 255, 255, 9))
        radius = round(short * .52)
        draw.ellipse((width - radius, -radius // 2, width + radius // 3, radius), fill=(*accent, 20))
        tick = round(short * .07)
        for x, y, dx, dy in ((inset, inset, 1, 1), (width - inset, inset, -1, 1),
                             (inset, height - inset, 1, -1), (width - inset, height - inset, -1, -1)):
            draw.line((x, y, x + dx * tick, y), fill=(*accent, 150), width=max(2, short // 360))
            draw.line((x, y, x, y + dy * tick), fill=(*accent, 150), width=max(2, short // 360))
    elif style_name == "简约大字":
        draw.rectangle((0, 0, round(width * .34), max(8, round(short * .012))), fill=(*accent, 235))
        draw.ellipse((width - short * .30, height - short * .50, width + short * .10, height + short * .20),
                     fill=(255, 255, 255, 12))
    elif style_name == "暖色纪实":
        radius = round(short * .55)
        draw.ellipse((-radius // 2, -radius // 2, radius, radius), fill=(*accent, 26))
        draw.line((0, round(height * .82), width, round(height * .82)), fill=(*accent, 30), width=1)
    else:
        radius = round(short * .46)
        draw.ellipse((width - radius, -radius // 3, width + radius // 4, radius), fill=(*accent, 26))
        draw.line((0, round(height * .18), width, round(height * .18)), fill=(*accent, 40),
                  width=max(2, short // 540))
        draw.rectangle((inset, inset, width - inset, height - inset), outline=(*accent, 42), width=hairline)
    glow = glow.filter(ImageFilter.GaussianBlur(round(short * .07)))
    layer.alpha_composite(glow)
    return layer


def _scrim(width, height, design, block_top, peak):
    fade = max(0, round(block_top - height * .11))
    solid = max(fade + 1, round(block_top - height * .01))
    base = rgb(design.scrim)
    column = Image.new("RGBA", (1, height - fade))
    pixels = column.load()
    span = solid - fade
    for y in range(height - fade):
        if y < span:
            position = y / span
            alpha = peak * position * position * (3 - 2 * position)
        else:
            alpha = peak
        pixels[0, y] = (*base, round(alpha))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    layer.alpha_composite(column.resize((width, height - fade), Image.Resampling.BILINEAR), (0, fade))
    return layer


def _balanced(text, face, width, limit):
    lines = lines_for(text, face, width)
    if 1 < len(lines) <= limit and len(lines[-1].strip()) <= 2:
        target = min(width, face.getlength(text) / len(lines) * 1.15)
        balanced = lines_for(text, face, target)
        if len(balanced) == len(lines) and len(balanced[-1].strip()) > 2:
            return balanced
    return lines


def _title(text, width, short, limit, ratio):
    size = round(short * ratio)
    while True:
        face = font(size, bold=True)
        lines = _balanced(text, face, width, limit)
        if (lines and len(lines) <= limit and all(face.getlength(line) <= width for line in lines)) or size <= 20:
            return face, lines
        size = max(20, round(size * .93))


def _clip(lines, face, width, limit):
    """Keep at most ``limit`` lines, marking a clipped or overlong tail."""
    if not lines:
        return lines
    if len(lines) <= limit and face.getlength(lines[-1]) <= width:
        return lines
    lines = lines[:limit]
    last = lines[-1]
    while last and face.getlength(last + "…") > width:
        last = last[:-1]
    lines[-1] = last + "…"
    return lines


def _write(draw, lines, face, x, y, line_height, color, shadow=None, offset=(0, 0)):
    for line in lines:
        if shadow and offset != (0, 0):
            draw.text((x + offset[0], y + offset[1]), line, font=face, fill=shadow, anchor="lt")
        draw.text((x, y), line, font=face, fill=color, anchor="lt")
        y += line_height
    return y


def _layout(width, height, title, subtitle, label):
    margin = round(width * .07)
    text_width = width - 2 * margin
    short = min(width, height)
    limit = 3 if height >= width else 2
    face, lines = _title(title, text_width, short, limit, .15 if height >= width else .128)
    lines = _clip(lines, face, text_width, limit)
    title_size = face.size
    line_height = round(title_size * 1.22)
    subtitle = subtitle.strip()
    sub_face = font(max(18, round(title_size * .30)), bold=False)
    sub_lines = _clip(lines_for(subtitle, sub_face, text_width), sub_face, text_width, 2) if subtitle else []
    badge = label.strip()[:10]
    badge_face = font(max(16, round(title_size * .26)), bold=True)
    badge_height = round(badge_face.size * 1.9) if badge else 0
    accent_height = max(4, round(title_size * .09))
    accent_gap = round(title_size * .45)
    gap = round(title_size * .42)
    sub_height = len(sub_lines) * round(sub_face.size * 1.45)
    block = (badge_height + accent_height + accent_gap + len(lines) * line_height
             + (gap + sub_height if sub_lines else 0))
    return {"margin": margin, "x": margin, "y": height - margin - block, "face": face, "lines": lines,
            "line_height": line_height, "sub_face": sub_face, "sub_lines": sub_lines, "sub_gap": gap,
            "badge": badge, "badge_face": badge_face, "badge_height": badge_height,
            "accent_height": accent_height, "accent_gap": accent_gap, "title_size": title_size}


def _text(board, layout, design):
    draw = ImageDraw.Draw(board)
    x, y = layout["x"], layout["y"]
    title_size = layout["title_size"]
    if layout["badge"]:
        face = layout["badge_face"]
        side = max(9, round(face.size * .52))
        top = y + round(face.size * .22)
        draw.rectangle((x, top, x + side, top + side), fill=design.accent)
        draw.text((x + side + round(face.size * .38), y + round(face.size * .06)),
                  layout["badge"], font=face, fill=design.accent, anchor="lt")
        y += layout["badge_height"]
    draw.rectangle((x, y, x + max(48, round(title_size * 1.15)), y + layout["accent_height"]),
                   fill=design.accent)
    y += layout["accent_height"] + layout["accent_gap"]
    y = _write(draw, layout["lines"], layout["face"], x, y, layout["line_height"], design.title,
               shadow=(0, 0, 0, 130), offset=(max(1, title_size // 40), max(2, title_size // 26)))
    if layout["sub_lines"]:
        face = layout["sub_face"]
        y += layout["sub_gap"]
        _write(draw, layout["sub_lines"], face, x, y, round(face.size * 1.45), design.muted,
               shadow=(0, 0, 0, 90), offset=(0, max(1, face.size // 30)))


def compose(background, size_name, title, subtitle="", label="", style_name=STYLE_NAMES[0]):
    """Draw the title block over a photo, or over a matching colour plate."""
    width, height = dimensions(size_name)
    design = style_of(style_name)
    title = " ".join(title.split())
    if not title:
        raise ValueError("请填写封面标题。")
    board = Image.new("RGBA", (width, height), design.paper)
    photo = bool(background)
    board.alpha_composite(_photo(background, width, height) if photo
                          else _palette(width, height, style_name, design))
    layout = _layout(width, height, title, subtitle, label)
    board.alpha_composite(_scrim(width, height, design, layout["y"], 246 if photo else 150))
    _text(board, layout, design)
    return board.convert("RGB")


def save(root, project, image, fields):
    folder = project_dir(root, project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    created = datetime.now().astimezone()
    stem = f"cover-{created:%Y%m%d-%H%M%S}-{uid()[:4]}"
    path = folder / (stem + ".png")
    image.save(path, "PNG", optimize=True)
    record = {
        "file": stem + ".png", "background": fields.get("background", ""),
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        "size": fields["size"], "width": image.width, "height": image.height,
        "title": fields["title"], "subtitle": fields.get("subtitle", ""), "label": fields.get("label", ""),
        "style": fields["style"], "source": fields.get("source") or "纯色",
    }
    (folder / (stem + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
