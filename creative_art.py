"""Render self-contained, model-authored SVG artwork on the shot's own clock."""

import math
import re
import subprocess
import render_progress
import task_control
import tempfile
import xml.etree.ElementTree as ET
from bisect import bisect_right
from dataclasses import dataclass

from PIL import ImageColor
from resvg_py import svg_to_bytes
from svg.path import parse_path


SVG_NS = "http://www.w3.org/2000/svg"
ELEMENTS = set(("svg g defs title desc style rect circle ellipse line polyline polygon path text tspan textPath "
                "linearGradient radialGradient stop clipPath mask pattern use symbol marker filter feGaussianBlur "
                "feOffset feColorMatrix feMerge feMergeNode feDropShadow feComposite feFlood feBlend "
                "animate animateTransform animateMotion").split())
ANIMATED = set(("x y x1 y1 x2 y2 cx cy r rx ry width height opacity fill-opacity stroke-opacity "
                "stroke-width stroke-dashoffset stroke-dasharray points d fill stroke offset font-size "
                "stop-color stop-opacity stdDeviation dx dy").split())
NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")
REFERENCE = re.compile(r"""url\(\s*['"]?#[\w.:-]+['"]?\s*\)""", re.I)

AUTHORING = (
    "可以原创插画、道具、场景、图形叙事、手绘路径、文字排版或动态叠层。按镜头内容自行构图、配色和安排节奏。"
    "要绘制时输出 svg 字段，值是完整的内联 SVG 字符串，包含 viewBox。支持 path、基本形状、文字、渐变、蒙版和分组。"
    "动效写 SVG animate、animateTransform 或带 path 的 animateMotion，支持路径运动、颜色、透明度、描边和变换。"
    "用 from/to 或 values、keyTimes、keySplines、begin、dur、repeatCount、fill。1s 对应整镜时长。"
    "只引用 SVG 内部的 #id。独立画面自行绘制背景，与实拍或生成图片组合时用透明底。底部为字幕留空间。"
)


def clock(value):
    number = re.fullmatch(r"(\d*\.?\d+)\s*(ms|s)?", value.strip())
    if not number:
        raise ValueError("动画时间请使用秒或毫秒。")
    return float(number[1]) / (1000 if number[2] == "ms" else 1)


def interpolate(left, right, amount, attribute):
    if amount <= 0 or left == right:
        return left
    if amount >= 1:
        return right
    if attribute in {"fill", "stroke", "stop-color"}:
        try:
            a, b = ImageColor.getrgb(left), ImageColor.getrgb(right)
            return "#" + "".join(f"{round(x + (y-x)*amount):02x}" for x, y in zip(a, b))
        except ValueError:
            return left
    # Preserve units, path commands and point separators. Matching SVG paths can morph.
    if NUM.split(left) != NUM.split(right):
        return left
    other = iter(NUM.findall(right))
    def tween(match):
        a, b = float(match[0]), float(next(other))
        return f"{a + (b-a)*amount:.6g}"
    return NUM.sub(tween, left)


def ease(amount, control):
    x1, y1, x2, y2 = control
    def cubic(t, a, b):
        return 3*(1-t)**2*t*a + 3*(1-t)*t*t*b + t**3
    lo, hi = 0., 1.
    for _ in range(15):
        t = (lo + hi) * .5
        if cubic(t, x1, x2) < amount:
            lo = t
        else:
            hi = t
    return cubic((lo+hi)*.5, y1, y2)


@dataclass
class Animation:
    node: ET.Element
    attribute: str
    transform: str
    values: list
    times: list
    begin: float
    duration: float
    repeats: float
    freeze: bool
    discrete: bool
    additive: bool
    splines: list
    path: object
    rotation: str

    @classmethod
    def read(cls, parent, element):
        motion_path = None
        rotation = element.get("rotate", "0")
        transform = element.get("type", "") if element.tag == "animateTransform" else ""
        attribute = element.get("attributeName", "transform" if transform else "")
        if element.tag == "animateMotion":
            attribute = "transform"
            try:
                motion_path = parse_path(element.get("path", ""))
                if not motion_path or not 0 < motion_path.length(error=.01) < 1e7:
                    raise ValueError
                if rotation not in {"auto", "auto-reverse"} and not math.isfinite(float(rotation)):
                    raise ValueError
            except (ValueError, IndexError, ArithmeticError):
                raise ValueError("animateMotion 需要有效的运动路径。") from None
        elif element.tag == "animateTransform":
            if attribute != "transform" or transform not in {"translate", "scale", "rotate", "skewX", "skewY"}:
                raise ValueError("不支持这个 SVG 变换类型。")
        elif attribute not in ANIMATED:
            raise ValueError("不支持这个 SVG 动画属性：" + attribute)
        if motion_path is not None:
            values = element.get("keyPoints", "0;1").strip(" ;").split(";")
            if not all(0 <= float(v) <= 1 for v in values):
                raise ValueError("运动路径的 keyPoints 需在 0 到 1 之间。")
        elif "values" in element.attrib:
            values = [v.strip() for v in element.get("values").strip(" ;").split(";")]
        else:
            values = [element.get("from", parent.get(attribute, "")), element.get("to", "")]
        if not 2 <= len(values) <= 128 or not all(values):
            raise ValueError("SVG 动画需要 from/to 或至少两个关键帧值。")
        times = ([float(v) for v in element.get("keyTimes").strip(" ;").split(";")] if "keyTimes" in element.attrib
                 else [i / (len(values)-1) for i in range(len(values))])
        if (len(times) != len(values) or times[0] != 0 or times[-1] != 1
                or any(not 0 <= a < b <= 1 for a, b in zip(times, times[1:]))):
            raise ValueError("SVG 的 keyTimes 必须从 0 递增到 1 并对应每个关键帧。")
        dur = clock(element.get("dur", "1s"))
        repeat = element.get("repeatCount", "1")
        repeats = math.inf if repeat == "indefinite" else float(repeat)
        if dur <= 0 or not math.isfinite(dur) or not repeats > 0:
            raise ValueError("SVG 动画的时长和循环次数必须大于零。")
        splines = []
        if element.get("calcMode") == "spline":
            for curve in element.get("keySplines", "").strip(" ;").split(";"):
                points = [float(v) for v in curve.replace(",", " ").split()]
                if len(points) != 4 or not all(0 <= v <= 1 for v in points):
                    raise ValueError("每段 keySplines 需要四个 0 到 1 的控制值。")
                splines.append(points)
            if len(splines) != len(values)-1:
                raise ValueError("keySplines 应与关键帧区间一一对应。")
        return cls(parent, attribute, transform, values, times, clock(element.get("begin", "0s")),
                   dur, repeats, element.get("fill") == "freeze", element.get("calcMode") == "discrete",
                   element.get("additive") == "sum", splines, motion_path, rotation)

    def apply(self, t):
        elapsed = (t - self.begin) / self.duration
        if elapsed < 0 or (elapsed >= self.repeats and not self.freeze):
            return
        phase = (self.repeats % 1 or 1) if elapsed >= self.repeats else elapsed % 1
        i = min(len(self.times)-2, bisect_right(self.times, phase)-1)
        k = (phase-self.times[i]) / (self.times[i+1]-self.times[i])
        if self.splines and 0 < k < 1:
            k = ease(k, self.splines[i])
        value = interpolate(self.values[i], self.values[i+1], 0 if self.discrete and k < 1 else k, self.attribute)
        if self.path is not None:
            fraction = float(value)
            point = self.path.point(fraction)
            angle = self.rotation
            if angle in {"auto", "auto-reverse"}:
                delta = self.path.point(min(1, fraction+.0001)) - self.path.point(max(0, fraction-.0001))
                angle = math.degrees(math.atan2(delta.imag, delta.real)) + (180 if angle == "auto-reverse" else 0)
            value = self.node.get("transform", "") + f" translate({point.real:.5f} {point.imag:.5f}) rotate({angle})"
        elif self.transform:
            prefix = self.node.get("transform", "") + " " if self.additive else ""
            value = prefix + f"{self.transform}({value})"
        self.node.set(self.attribute, value)


class Artwork:
    def __init__(self, svg):
        if not isinstance(svg, str) or len(svg) > 100_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", svg, re.I):
            raise ValueError("原创画面需要一份自包含的 SVG。")
        try:
            self.root = ET.fromstring(svg)
        except ET.ParseError as error:
            raise ValueError(f"原创画面的 SVG 格式有误：{error}") from None
        self.animations = []
        self.original = {}
        nodes = list(self.root.iter())
        if len(nodes) > 600:
            raise ValueError("请将原创画面精简到 600 个 SVG 元素以内。")
        for node in nodes:
            node.tag = node.tag.removeprefix("{" + SVG_NS + "}")
            if node.tag not in ELEMENTS:
                raise ValueError("原创画面含有不支持的 SVG 元素：" + node.tag)
            if node.tag == "style" and re.search(r"@|url\s*\(|\\", REFERENCE.sub("", node.text or ""), re.I):
                raise ValueError("请将动画写成 animate 或 animateTransform，并使用内联样式。")
            for name, value in node.attrib.items():
                attr = name.rsplit("}", 1)[-1].lower()
                if (attr.startswith("on") or attr in {"base", "src"} or (attr == "href" and not value.startswith("#"))
                        or re.search(r"url\s*\(|@import|javascript:|data:|://", REFERENCE.sub("", value), re.I)):
                    raise ValueError("原创 SVG 只能引用内部图形。")
        if self.root.tag != "svg":
            raise ValueError("原创画面的根元素须为 svg。")
        if not any(n.tag in {"path","rect","circle","ellipse","line","polyline","polygon","text","use"} for n in nodes):
            raise ValueError("原创 SVG 缺少可绘制的内容。")
        self.has_text = any(n.tag == "text" for n in nodes)
        for parent in nodes:
            for child in list(parent):
                if child.tag in {"animate", "animateTransform", "animateMotion"}:
                    motion = Animation.read(parent, child)
                    self.original[parent, motion.attribute] = parent.get(motion.attribute)
                    self.animations.append(motion)
                    parent.remove(child)
        self.root.set("xmlns", SVG_NS)

    def frame(self, size, t, fonts):
        for (node, attr), value in self.original.items():
            if value is None:
                node.attrib.pop(attr, None)
            else:
                node.set(attr, value)
        for motion in self.animations:
            motion.apply(t)
        if "viewBox" not in self.root.attrib:
            dims = [self.root.get(attr, "").removesuffix("px") for attr in ("width", "height")]
            dims = dims if all(NUM.fullmatch(v) and float(v) > 0 for v in dims) else list(map(str, size))
            self.root.set("viewBox", "0 0 " + " ".join(dims))
        self.root.set("width", str(size[0]))
        self.root.set("height", str(size[1]))
        return svg_to_bytes(svg_string=ET.tostring(self.root, encoding="unicode"), skip_system_fonts=True, **fonts)


def render_artwork(svg, temp, number, size, frames, fps, encoder):
    # Cache the artwork as a single completed handoff, including its frame loop.
    art = Artwork(svg)
    result = temp / (f"original-{number}.png" if not art.animations or frames == 1 else f"original-{number}.mov")
    with render_progress.output(result, ["artwork-v1", svg, size, frames, fps, encoder]) as ready:
        if not ready:
            _render_artwork(svg, temp, number, size, frames, fps, encoder)
    return result


def _render_artwork(svg, temp, number, size, frames, fps, encoder):
    art = Artwork(svg)
    fonts = {}
    if art.has_text:
        from shot_visuals import font
        normal, bold = font(24), font(24, True)
        family = normal.getname()[0]
        fonts = {"font_files": list(dict.fromkeys([str(normal.path), str(bold.path)])),
                 "font_family": family, "sans_serif_family": family}
    if not art.animations or frames == 1:
        result = temp / f"original-{number}.png"
        result.write_bytes(art.frame(size, 0, fonts))
        return result
    result = temp / f"original-{number}.mov"
    args = [encoder, "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(fps),
            "-c:v", "png", "-i", "pipe:0", "-an", "-c:v", "qtrle", "-pix_fmt", "argb", str(result)]
    with tempfile.TemporaryFile() as errors, task_control.process(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        try:
            for n in range(frames):
                task_control.checkpoint()
                process.stdin.write(art.frame(size, n / (frames-1), fonts))
            process.stdin.close()
            process.wait(timeout=90)
        except BrokenPipeError:
            process.wait(timeout=15)
        except BaseException:
            process.kill()
            raise
        if process.returncode:
            errors.seek(0)
            raise ValueError("原创动效编码失败。\n" + errors.read()[-600:].decode("utf-8", "replace"))
    return result
