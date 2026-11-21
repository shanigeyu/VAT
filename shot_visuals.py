"""Compose an illustrated voice shot when the editor has no subject or backdrop."""
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from app_paths import get_path

from planning import relevance, terms
from source_visuals import public_topic_images, repo_name, repository_images
from store import script_units


CREAM = "#F5F1E9"
INK = "#172329"
ORANGE = "#D94824"
SLATE = "#4F625F"
MINT = "#C9E7D8"
PUNCT = " \t，,、。；;：:！!？?…"
LIST_SPLIT = re.compile(r"[、，,；;]")


@dataclass(frozen=True)
class IllustrationColors:
    paper: str = CREAM
    text: str = INK
    muted: str = SLATE
    accent: str = ORANGE
    border: str = "#BCC9C0"
    footer: str = "#142025"
    progress: str = "#F47C52"
    panel: str = "#FDFBF6"
    shadow: str = "#CBD3CC"
    tiles: tuple = ((INK, "white"), (MINT, INK), ("#F7DBC9", INK), ("#E7EBDF", INK))


EDITORIAL = IllustrationColors()
EDITORIAL_DARK = replace(EDITORIAL, paper=INK, text=CREAM, muted="#B1C7BC", accent="#FF8358", border="#9EAFA4")
DARK_TECH = IllustrationColors(
    paper="#0C1417", text="#EEF6EF", muted="#A5BCB5", accent="#C5F65D", border="#38534D",
    footer="#091013", progress="#C5F65D", panel="#111F22", shadow="#0B1417",
    tiles=(("#C5F65D", "#17231A"), ("#253937", "#EEF6EF"), ("#1C2F37", "#EEF6EF"), ("#344432", "#EEF6EF")),
)
WARM = IllustrationColors(
    paper="#F6E9D8", text="#342519", muted="#6F533E", accent="#B9502A", border="#CDAE94",
    footer="#2F2119", progress="#D78559", panel="#FFFAEF", shadow="#DFCCB7",
    tiles=(("#764831", "#FFFAEF"), ("#EBD7B9", "#342519"), ("#F2D2BD", "#342519"), ("#E6DDC6", "#342519")),
)
ILLUSTRATION_STYLES = {"简约图解": EDITORIAL, "暗黑科技": DARK_TECH, "暖色纪实": WARM}


@lru_cache(maxsize=2)
def system_font(bold):
    if os.name == "nt":
        folder = Path(os.environ["WINDIR"]) / "Fonts"
        names = ("msyhbd.ttc", "simhei.ttf", "msyh.ttc") if bold else ("msyh.ttc", "simhei.ttf")
        candidates = [folder / name for name in names]
    elif sys.platform == "darwin":
        candidates = [Path("/System/Library/Fonts/PingFang.ttc"),
                      Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
                      Path("/System/Library/Fonts/STHeiti Medium.ttc")]
    else:
        suffix = "Bold" if bold else "Regular"
        candidates = [Path(f"/usr/share/fonts/opentype/noto/NotoSansCJK-{suffix}.ttc"),
                      Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")]
    for path in candidates:
        if path.is_file():
            return str(path)
    # Fontconfig can also locate user-installed Chinese fonts outside system directories.
    fc_list = shutil.which("fc-list")
    if fc_list:
        pattern = ":lang=zh" + (":weight=bold" if bold else "")
        result = subprocess.run([fc_list, pattern, "-f", "%{file}\\n"], capture_output=True, timeout=8)
        for name in result.stdout.decode("utf-8", "replace").splitlines():
            if Path(name).is_file():
                return name
        if bold:
            return system_font(False)
    raise ValueError("未找到中文字体。请在设置中选择支持中文的 TTF、TTC 或 OTF 字体，或安装 Noto Sans CJK。")


@lru_cache(maxsize=100)
def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError as exc:
        raise ValueError(f"无法读取字体「{path}」，请在设置中重新选择。") from exc


def font(size, bold=False):
    return load_font(get_path("font_file") or system_font(bold), size)


def lines_for(text, face, width):
    lines = []
    for para in text.strip().splitlines():
        line = ""
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+#/-]*|\s+|.", para):
            if line and face.getlength(line + token) > width and token not in "，。、！？：；,.!?":
                lines.append(line.rstrip())
                line = ""
            line = (line + token) if line else token.lstrip()
        if line.strip():
            lines.append(line.rstrip())
    return lines


def text_block(draw, point, text, width, size, color, *, limit=2, bold=True):
    while size > 16:
        face = font(size, bold)
        lines = lines_for(text, face, width)
        if len(lines) <= limit and all(face.getlength(line) <= width for line in lines):
            break
        size -= 2
    if len(lines) == 2 and len(lines[-1]) <= 3 and size > 30:
        balanced = lines_for(text, face, min(width, face.getlength(text)*.5 + size))
        if len(balanced) == 2:
            lines = balanced
    lines = lines[:limit]
    if lines and face.getlength(lines[-1]) > width:
        end = lines[-1]
        while end and face.getlength(end + "…") > width:
            end = end[:-1]
        lines[-1] = end + "…"
    x, y = point
    for line in lines:
        draw.text((x, y), line, font=face, fill=color, anchor="lt")
        y += round(size * 1.28)
    return y


def clauses(text):
    return [s.strip(" ，,。；;") for s in re.split(r"[，,；;。！？!?]+", text) if s.strip(" ，,。；;")]


def short_phrase(text):
    text = re.sub(r"^(?:可以看看|你还能|你可以|不管你是|还是想|还有|再到|而是用|它涵盖了)", "", text.strip())
    return text.strip(PUNCT) or text


def scene_heading(text, question=False):
    parts = clauses(text) or [text]
    if question:
        return short_phrase(parts[-1]) + "？"
    specific = [s for s in parts if not re.match(r"^(?:需要的话|它不是|这些方向|全系标配|都可以|都能)", s)]
    return short_phrase((specific or parts)[0])


def list_items(text):
    if text.count("、") < 2:
        return []
    items = [short_phrase(s) for s in LIST_SPLIT.split(text)]
    items = [s for s in items if 1 < len(s) <= 13 and not re.search(r"它都|你还|都能|定稿|这些", s)]
    return list(dict.fromkeys(items))[:8] if len(items) >= 3 else []


def subject_label(item):
    if item.get("kind") == "page":
        return item["label"]
    if item["context"]:
        return item["context"]
    label = item["label"].split("：", 1)[0]
    if len(label) < 4 and "首页" in item["label"]:
        return "项目首页"
    return label


def diagram_panel(size, labels, headline, colors):
    w, h = size
    panel = Image.new("RGB", (w + 12, h + 8), colors.panel)
    draw = ImageDraw.Draw(panel)
    pad = 24
    labels = labels[:8] or [headline]
    if len(labels) >= 3:
        columns = 2 if len(labels) > 3 else 1
        rows = math.ceil(len(labels) / columns)
        gap = 10
        cw = (w - 2 * pad - (columns - 1) * gap) // columns
        ch = max(30, (h - 2 * pad - (rows - 1) * gap) // rows)
        for i, value in enumerate(labels):
            x = pad + (i % columns) * (cw + gap)
            y = pad + (i // columns) * (ch + gap)
            fill, fg = colors.tiles[i % len(colors.tiles)]
            draw.rounded_rectangle((x, y, x + cw, y + ch), radius=11, fill=fill)
            draw.line((x + 15, y + 12, x + 37, y + 12), fill=colors.accent, width=3)
            text_block(draw, (x + 15, y + max(20, int(ch * .35))), value, cw - 30,
                       min(39, max(19, ch // 3)), fg)
    else:
        # A stepped pair of editorial notes keeps the relationship between the clauses visible.
        for i, value in enumerate(labels):
            y = pad + i * ((h - 2 * pad) // 2)
            bh = (h - 2 * pad) // len(labels) - 16
            x = pad + i * 24
            fill, fg = colors.tiles[i]
            draw.rounded_rectangle((x + 8, y + 9, w - pad + 6, y + bh + 8), radius=12, fill=colors.shadow)
            draw.rounded_rectangle((x, y, w - pad, y + bh), radius=12, fill=fill)
            draw.rectangle((x + 17, y + 18, x + 23, y + bh - 18), fill=colors.accent if fill != colors.accent else fg)
            text_block(draw, (x + 39, y + max(22, bh // 4)), value, w - pad - x - 55,
                       min(47, max(25, bh // 4)), fg, limit=3)
    return panel


def source_panel(path, size, detail):
    w, h = size
    with Image.open(path) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
    if not detail and im.width / im.height > 1.65 and h > w*.71:
        board = Image.new("RGB", (w+8, h+6), "#F9F8F4")
        full = ImageOps.contain(im, (w-16, round(h*.56)), Image.Resampling.LANCZOS)
        board.paste(full, ((w-full.width)//2, 8))
        top = full.height + 23
        crop = im.crop((round(im.width*.40), round(im.height*.16), round(im.width*.90), round(im.height*.82)))
        view = ImageOps.fit(crop, (w-16, h-top), Image.Resampling.LANCZOS, centering=(.5,.45))
        board.paste(view, (8, top))
        ImageDraw.Draw(board).line((10, top-7, w-10, top-7), fill="#C3CBC4", width=1)
        return board
    scale = max(w / im.width, h / im.height) * (1.12 if detail else 1)
    if max(im.width * scale / w, im.height * scale / h) > 3.25:
        # Very wide/tall material opens as a whole illustration before the detail cut.
        if not detail:
            whole = Image.new("RGB", (w + 8, h + 6), "#FAF9F5")
            im.thumbnail((w, h), Image.Resampling.LANCZOS)
            whole.paste(im, ((whole.width - im.width) // 2, (whole.height - im.height) // 2))
            return whole
    return im.resize((max(w, math.ceil(im.width * scale)), max(h, math.ceil(im.height * scale))),
                     Image.Resampling.LANCZOS)


class SceneIllustrator:
    def __init__(self, root, project, size, report):
        topic = next((t for t in project["topics"] if t.get("id") == project.get("topic_id")), {})
        self.repo = repo_name(topic.get("url", ""))
        self.topic = topic
        name = self.repo.split("/")[-1] if self.repo else topic.get("title") or project["title"]
        match = re.search(re.escape(name), project["approved"], re.I) if name else None
        self.name = match.group() if match else name
        self.size = size
        self.colors = ILLUSTRATION_STYLES.get(project.get("visual_palette"), EDITORIAL)
        self.total = len(project["shots"])
        self.units = script_units(project["approved"])
        self.images = (repository_images(root, self.repo, report) if self.repo
                       else public_topic_images(root, topic, report))
        self.images += project.get("_library_illustrations", [])
        self.seen = Counter()
        self.previous = ""

    def source(self, text, direction, *, opening=False, previous=None):
        clue = terms(text)
        requested = terms(direction)
        choices = []
        for pic in self.images:
            label = pic["label"] + " " + pic["context"] + " " + pic["url"].split("/")[-1]
            keywords = terms(label)
            score = relevance(keywords, clue) + .35 * relevance(keywords, requested)
            if opening and re.search("首页|overview|screenshot|preview|演示", label, re.I):
                score += 10
            if re.search("题解|核心回答|解析|图解|attention", label, re.I) and re.search("图解|题解|解析|原理|示例|图文", text):
                score += 5
            if pic["context"] and not pic.get("kind") and re.search("图文|图解|图示|示意", text):
                score += 16
            if pic["context"] and re.search("attention|transformer", label, re.I) and re.search("大模型|注意力", text):
                score += 5
            if score and pic.get("kind") == "page":
                score += 3
            if score:
                score = score / (1 + .45 * self.seen[pic["url"]]) - (2 if self.previous == pic["url"] else 0)
                choices.append((score, pic))
        if choices:
            if previous:
                others = [pair for pair in choices if pair[1]["url"] != previous["url"] and pair[0] >= 3]
                if others:
                    choices = others
            return max(choices, key=lambda pair: pair[0])[1]
        return None

    def compose(self, temp, number, beat, heading, labels, picture, *, detail):
        w, h = self.size
        portrait = h > w
        colors = EDITORIAL_DARK if self.colors == EDITORIAL and number % 4 == 1 else self.colors
        bg = Image.new("RGB", self.size, colors.paper)
        draw = ImageDraw.Draw(bg)
        mx = 42 if portrait else 50
        draw.rounded_rectangle((mx, 47, mx+17, 64), radius=4, fill=colors.accent)
        text_block(draw, (mx+28, 44), self.name, w-2*mx-145, 25, colors.text, limit=1)
        draw.text((w-mx, 47), f"{number:02} / {self.total:02}", font=font(22), fill=colors.muted, anchor="rt")
        draw.line((mx, 88, w-mx, 88), fill=colors.muted, width=1)
        if portrait:
            title_end = text_block(draw, (mx, 124), heading, w-2*mx, 62, colors.text, limit=2)
            x, y, pw = mx, int((title_end+47)//2)*2, w-2*mx
            ph = 930 - y
        else:
            x, y, pw, ph = int(w*.41), 130, int(w*.55), int(h*.50)
            text_block(draw, (mx, 162), heading, x-mx-26, 55, colors.text, limit=4)
        draw.rectangle((x-2, y-2, x+pw+1, y+ph+1), fill=colors.border)
        if picture:
            panel = source_panel(picture["path"], (pw, ph), detail)
            caption = subject_label(picture)
            if picture.get("kind") == "page":
                credit = picture["url"].split("://", 1)[-1].rstrip("/")
            elif picture.get("kind") == "library":
                credit = picture["url"]
            elif picture.get("kind") == "commons":
                credit = "Wikimedia Commons  /  " + picture["license"] + "  /  " + picture["label"]
            else:
                credit = self.repo + ("  /  " + caption if caption else "")
        else:
            panel = diagram_panel((pw, ph), labels, heading, colors)
            credit = "据本期定稿整理"
        if number == self.total and self.repo:
            credit = "github.com/" + self.repo
        text_block(draw, (x, y+ph+20), credit, pw, 22 if portrait else 18, colors.muted, limit=2, bold=False)
        floor = int(h * .805)
        draw.rectangle((0, floor, w, h), fill=colors.footer)
        draw.rectangle((0, floor, round(w*number/self.total), floor+3), fill=colors.progress)
        stem = f"art-{number}-{beat}"
        base, content = temp / (stem + "-base.png"), temp / (stem + "-panel.png")
        bg.save(base)
        panel.save(content)
        return base, content, (x, y, pw, ph)

    def plates(self, temp, number, shot, frames, fps):
        text = "".join(self.units[shot["start"]:shot["end"]])
        parts = clauses(text) or [text]
        items = list_items(text)
        wants_two = frames >= 7 * fps and len(parts) > 1
        half = sum(len(part) for part in parts) / 2
        split = min(range(1, len(parts)), key=lambda i: abs(sum(map(len, parts[:i])) - half)) if wants_two else 1
        groups = [parts[:split], parts[split:]] if wants_two else [parts]
        previous_picture = None
        for i, group in enumerate(groups):
            line = "，".join(group)
            question = i == 0 and text.rstrip().endswith(("？", "?"))
            heading = scene_heading(line, question)
            opening = number in (1, 2, self.total) or "开源" in line
            direction = shot["visual"] if shot["visual"] != text and shot["visual"] != "待填写画面说明" else ""
            picture = self.source(line, direction, opening=opening, previous=previous_picture)
            labels = list(dict.fromkeys(short_phrase(v) for v in group))
            if items and i == 0:
                picture, labels = None, items
                heading = ("题库覆盖的方向" if "题库" in (self.topic.get("notes") or "") else "涉及的方向") if "方向" in text else "、".join(items[:3])
            if not picture and i and previous_picture:
                picture = previous_picture
            if not picture and opening and self.images and not (items and i == 0):
                picture = self.images[0]
            if picture:
                if picture.get("kind") == "page" and not question:
                    heading = picture["label"]
                if number == self.total and self.repo:
                    heading = self.name
                self.seen[picture["url"]] += 1
                self.previous = picture["url"]
                previous_picture = picture
            else:
                self.previous = ""
                if labels == [heading]:
                    other = [short_phrase(v) for v in parts if short_phrase(v) != heading]
                    labels = other[:3] or [self.name, heading]
                # A short closing line gets its actual project name and source as a destination.
                if number == self.total and self.repo:
                    labels = [self.name, "github.com/" + self.repo]
            total_chars = sum(len(v) for v in parts)
            split_frame = max(1, min(frames-1, round(frames * sum(map(len, groups[0])) / total_chars)))
            start = split_frame if i else 0
            end = split_frame if i == 0 and len(groups) > 1 else frames
            yield (self.compose(temp, number, i, heading, labels, picture, detail=bool(i)),
                   start, end-start, picture["url"] if picture else "")
