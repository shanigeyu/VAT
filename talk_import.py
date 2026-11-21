import re
from dataclasses import dataclass
from pathlib import Path

import planning
import production
from store import script_units, shots_from_plan


TIMECODE = r"([0-9]+:[0-5][0-9]:[0-5][0-9][,.][0-9]{3})"
TIMING_LINE = re.compile(rf"{TIMECODE}[ \t]*-->[ \t]*{TIMECODE}(?:[ \t]+.*)?")


def milliseconds(value):
    h, m, s, ms = map(int, re.split(r"[:,.]", value))
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def read_srt(path):
    raw = Path(path).read_bytes()
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法读取 SRT 编码，请将字幕另存为 UTF-8 后重试。")

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("SRT 文件中没有字幕。")
    cues = []
    for i, block in enumerate(re.split(r"\n(?:[ \t]*\n)+", text), 1):
        lines = block.strip().split("\n")
        if lines[0].strip().isdigit():
            lines = lines[1:]
        timing = TIMING_LINE.fullmatch(lines[0].strip()) if lines else None
        content = "\n".join(lines[1:]).strip()
        if not timing or not content:
            raise ValueError(f"SRT 第 {i} 条的时间码或正文有误。")
        start, end = map(milliseconds, timing.groups())
        if end <= start:
            raise ValueError(f"SRT 第 {i} 条的结束时间须晚于开始时间。")
        cues.append({"start": start, "end": end, "text": content})
    return sorted(cues, key=lambda cue: cue["start"])


@dataclass
class TalkImport:
    path: Path
    cues: list
    orientation: str

    def clip(self, first, stop):
        cues = self.cues[first:stop]
        start = cues[0]["start"]
        end = max(cue["end"] for cue in cues)
        return {"offset": start / 1000, "length": (end - start) / 1000, "srt": cues}


def prepare(srt_path, video_path):
    cues = read_srt(srt_path)
    path = Path(video_path).resolve()
    if not path.is_file() or path.suffix.lower() not in production.VIDEOS:
        raise ValueError("请选择存在的口播视频文件。")
    meta = production.probe(path)
    video = production.media_stream(path, meta, "video")
    production.media_stream(path, meta, "audio")
    duration = round(production.media_seconds(path, meta, "video") * 1000)
    for i, cue in enumerate(cues, 1):
        if cue["start"] >= duration or cue["end"] > duration + 1000 // production.FPS:
            raise ValueError(f"SRT 第 {i} 条超出口播视频时长，请核对字幕和视频是否配套。")
        cue["end"] = min(cue["end"], duration)
    width, height = video["width"], video["height"]
    rotation = next((v["rotation"] for v in video.get("side_data_list", []) if "rotation" in v), 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    orientation = "竖屏 9∶16" if height >= width else "横屏 16∶9"
    return TalkImport(path, cues, orientation)


def match_shots(prefs, key, project, source):
    units = script_units(project["approved"])
    shots = [{"镜头": i, "台词": " ".join(units[s["start"]:s["end"]]),
              "计划真人出镜": s["kind"] == "talk"} for i, s in enumerate(project["shots"], 1)]
    brief = (
        "把已录制口播的字幕段匹配到现有分镜。只有连续字幕完整说出了该镜台词，才为该镜匹配。"
        "允许同义转述，题材相似但意思或细节不同的要跳过。优先使用覆盖整镜又不带多余台词的最短连续范围。"
        "同一镜头和同一条字幕各只能用一次。台词相同时优先匹配计划真人出镜的镜头。"
        '返回 JSON 数组，每项 {"shot":镜头号,"first":首条字幕编号,"last":末条字幕编号}，首尾均包含。'
        "三个字段都用整数，没有匹配则返回空数组。"
    )
    matches = planning.model_list(prefs, key, brief, {
        "分镜": shots, "口播字幕": dict(enumerate((cue["text"] for cue in source.cues), 1)),
    })
    clips, used = {}, set()
    for match in matches:
        if not isinstance(match, dict):
            raise ValueError("模型返回的口播匹配格式有误，请重新匹配。")
        shot, first, last = (match.get(k) for k in ("shot", "first", "last"))
        if not (all(type(n) is int for n in (shot, first, last))
                and 1 <= shot <= len(shots) and 1 <= first <= last <= len(source.cues)):
            raise ValueError("模型返回的镜头或字幕编号超出范围，请重新匹配。")
        indices = range(first - 1, last)
        if shot - 1 in clips or not used.isdisjoint(indices):
            raise ValueError("模型重复使用了镜头或口播字幕，请重新匹配。")
        used.update(indices)
        clips[shot - 1] = source.clip(first - 1, last)
    return dict(sorted(clips.items()))


def build_timeline(project, source):
    script = "\n".join(cue["text"] for cue in source.cues)
    units = script_units(script)
    project.update(draft=script, approved=script, orientation=source.orientation,
                   sections=[{"title": "口播", "end": len(units)}],
                   shots=shots_from_plan(units, [{"end": len(units), "kind": "talk", "visual": "真人出镜"}]))
    project["captions"].update(mode="烧录到成片", edits={})
    return {0: {"offset": 0, "length": 0, "srt": source.cues}}
