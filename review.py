import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import production as video
import planning
from store import SHOT_SLOTS, confirmed, script_units, shot_clips


SAMPLE = "preview.mp4"
TIMELINE = "timeline.json"
BRIEF = "制作说明.txt"
CAPTIONS = "captions.srt"
STEMS = {"素材声": "material", "背景声": "background", "AI文案": "ai_voice", "本人文案": "human_voice"}


class MediaCopies:
    def __init__(self, folder):
        self.folder = folder
        (folder / "media").mkdir()
        self.paths = {}
        self.catalog = []

    def keep(self, source, name=None):
        source = Path(source).resolve()
        if source not in self.paths:
            if not source.is_file():
                raise ValueError(f"找不到样片使用的文件「{source}」。")
            relative = f"media/{len(self.paths) + 1:04d}{source.suffix.lower()}"
            copy = self.folder / relative
            shutil.copyfile(source, copy)
            self.paths[source] = copy
            self.catalog.append({"file": relative, "name": name or source.name})
        return self.paths[source]


def freeze_inputs(project, assets, media):
    project = deepcopy(project)
    used = {}
    for n, shot in enumerate(project["shots"], 1):
        for slot in video.LAYER_ORDER:
            for clip in shot_clips(shot, slot):
                asset_id = clip["id"]
                if asset_id not in used:
                    source = video.asset_path(assets, asset_id, n)
                    used[asset_id] = {**assets[asset_id], "path": str(media.keep(source))}
        if video.voice_source(shot) in ("record_file", "ai_file"):
            source = Path(shot.get("voice_file", ""))
            if not source.is_file():
                raise ValueError(f"镜头 {n} 缺少配音文件，请在音频制作中重新选择。")
            shot["voice_file"] = str(media.keep(source))
    for settings, key in ((project, "sfx"), (project["sound"], "background")):
        if settings[key]:
            settings[key] = str(media.keep(settings[key]))
    return project, used


def span(start, frames):
    end = start + frames
    return {"start_frame": start, "end_frame": end, "start_ms": start * 1000 // video.FPS,
            "end_ms": end * 1000 // video.FPS}


def sound_placements(folder, spans, cursor, loop=False):
    result = []
    for source, frames, offset, limit in spans:
        if source is not None:
            seconds = frames / video.FPS
            path = Path(source)
            if limit is None and not loop:
                limit = video.media_seconds(path, video.probe(path), "audio") - offset
            take = seconds if limit is None else min(limit, seconds)
            placement = {**span(cursor, frames), "file": path.relative_to(folder).as_posix(),
                         "source_in_seconds": offset, "take_seconds": take, "loop": loop}
            if not loop:
                placement["source_out_seconds"] = round(offset + take, 9)
            result.append(placement)
        cursor += frames
    return result


def picture_bounds(clip, slot, size, cache):
    x, y, width, height = video.layer_box(size, clip.rect)
    if slot == "theme":
        return {"x": 0, "y": 0, "width": width, "height": height}
    scaling = video.layer_sizing(slot, (width, height), custom_box=clip.rect is not None)
    key = clip.path, clip.offset, clip.loop, scaling
    if key not in cache:
        # Measure the same filter at delivery resolution, including FFmpeg's source auto-rotation.
        header = video.ffmpeg(*video.visual_input(clip.path, clip.offset, clip.loop), "-map", "0:v:0",
                              "-vf", scaling, "-frames:v", 1, "-c:v", "wrapped_avframe", "-f", "framecrc", "-")
        match = re.search(rb"^#dimensions\s+0:\s*(\d+)x(\d+)", header, re.MULTILINE)
        if not match:
            raise ValueError(f"无法读取素材「{clip.path.name}」的画面尺寸。")
        w, h = map(int, match.groups())
        cache[key] = w, h
    w, h = cache[key]
    # The yuv420 overlay places its top-left corner on an even pixel.
    return {"x": x + 2 * ((width - w) // 4), "y": y + 2 * ((height - h) // 4), "width": w, "height": h}


def shot_record(folder, project, units, row, size, bounds):
    shot, start = row["shot"], row["start"]
    pictures = []
    for slot in video.LAYER_ORDER:
        for clip in row["layers"][slot]:
            still = clip.path.suffix.lower() in video.IMAGES
            pictures.append({
                **span(start + clip.start, clip.frames), "layer": slot,
                "file": clip.path.relative_to(folder).as_posix(), "source_in_seconds": 0 if still else clip.offset,
                "take_seconds": clip.source_seconds if clip.source_seconds is not None else clip.frames / video.FPS,
                "playback": "still" if still else ("loop" if clip.loop else "once_hold_last_frame"),
                "rect_px": picture_bounds(clip, slot, size, bounds),
                **({"box_normalized": list(clip.rect)} if clip.rect is not None else {}),
                **({"illustration_source": clip.illustration_source or "本期定稿"} if clip.illustration_source is not None else {}),
            })
    section = next((s["title"] for s in project["sections"] if shot["start"] < s["end"]), "")
    return {
        "number": row["n"], **span(start, row["frames"]), "section": section, "kind": shot["kind"],
        "first_sentence": shot["start"] + 1, "last_sentence": shot["end"],
        "script": "".join(units[shot["start"]:shot["end"]]), "visual_direction": shot["visual"],
        "pictures": pictures, "voice_source": row["source"],
        "voice_track": "本人文案" if row["source"] in ("record_file", "camera") else "AI文案",
        "voice": sound_placements(folder, row["speech"], start),
        "material_audio": [sound_placements(folder, track, start, loop=True) for track in row["ambience"]],
    }


def make_manifest(folder, project, rows, media, cues, sample_size, created, version, stems):
    size = video.picture_size(project["orientation"])
    width, height = size
    bounds = {}
    frames = sum(row["frames"] for row in rows)
    units = script_units(project["approved"])
    layers = {
        slot: {"name": SHOT_SLOTS[slot], "z": z, "anchor": "center", "anchor_px": [width // 2, height // 2],
               "fit": "cover_center_crop" if slot == "theme" else "contain",
               "scale_filter": video.layer_sizing(slot, size), "overlay_xy": ["(W-w)/2", "(H-h)/2"]}
        for z, slot in enumerate(video.LAYER_ORDER, 1)
    }
    sound = project["sound"]
    layers["main"]["placement"] = "Use each picture's rect_px for its final position and size."
    audio = {"mix": "audio/mix.wav", "stems": stems, "sample_rate": 48000, "channels": 2,
             "material_audio_enabled": sound["clip_audio"],
             "gain_percent": {"素材声": project["sfx_volume"], "背景声": sound["bg_gain"],
                              "AI文案": sound["ai_gain"], "本人文案": sound["human_gain"]},
             "narration_loudnorm": "I=-16:TP=-1.5:LRA=11"}
    for key, source, loop in (("background", sound["background"], True), ("extra_effect", project["sfx"], False)):
        audio[key] = sound_placements(folder, [(source, frames, 0, None)], 0, loop) if source else []
    return {
        "schema_version": 1, "title": project["title"], "version": version, "created_at": created.isoformat(),
        "approved_script": project["approved"],
        "canvas": {"width": width, "height": height, "fps": video.FPS, "duration_frames": frames,
                   "duration_ms": frames * 1000 // video.FPS, "background": "#" + video.CANVAS_COLOR,
                   "time_ranges": "[start,end)", "layers": layers},
        "preview": {"file": SAMPLE, "width": sample_size[0], "height": sample_size[1],
                    "captions_burned_in": bool(cues)},
        "media": media.catalog, "shots": [shot_record(folder, project, units, row, size, bounds) for row in rows],
        "audio": audio,
        "captions": {"file": CAPTIONS, "final_mode": project["captions"]["mode"],
                     "style": project["captions"].get("style") or video.SUBTITLE_STYLE,
                     "cues": [{"shot": c["shot"], "start_ms": c["start"], "end_ms": c["end"], "text": c["text"]}
                              for c in cues]},
    }


def write_brief(folder, manifest):
    canvas = manifest["canvas"]
    names = {asset["file"]: asset["name"] for asset in manifest["media"]}
    lines = [
        f"《{manifest['title']}》成片制作说明", "",
        f"请按 timeline.json 的分镜，用包内原始素材制作 {canvas['width']}×{canvas['height']}、"
        f"{canvas['fps']} fps 的 MP4，全片 {canvas['duration_frames']} 帧。",
        "先看 preview.mp4 核对剪辑和声音，再按 shots 中的画面说明和时间轴组接原素材。"
        "media 对照表提供原文件名，所有文件路径均相对于解压目录。",
        "全片帧号从 0 开始，start_frame 包含在片段内，end_frame 为下一段起点。"
        "source_in_seconds 是源文件入点，take_seconds 是取用秒数，loop 表示按需循环。单次播放的音轨在片段末尾补静音。",
        "每个画面的 rect_px 给出成片像素坐标，原点在左上角。按 layer 对应的 z 从小到大叠放，底图等比填满并居中裁切，"
        "主体在各自画框中等比居中，动效在画布中等比居中，保留素材透明度。",
        "直接使用 audio/mix.wav 可还原样片声音。需要重混时，四条分轨均从第 0 帧开始，已包含设置的音量。"
        "各镜 voice 列出实际配音文件及位置，material_audio 列出素材原声。",
        f"按 captions.srt 的文字和时间交付字幕。本版选定的成片字幕形式为「{manifest['captions']['final_mode']}」。",
        "", "分镜",
    ]
    for shot in manifest["shots"]:
        lines.extend([f"{shot['number']:02d}  {video.srt_time(shot['start_ms'])} – {video.srt_time(shot['end_ms'])}  {shot['section']}",
                      "画面　" + shot["visual_direction"], "台词　" + shot["script"]])
        for clip in shot["pictures"]:
            rect = clip["rect_px"]
            lines.append(f"　{SHOT_SLOTS[clip['layer']]}　{names[clip['file']]}（{clip['file']}）　"
                         f"全片第 {clip['start_frame']}–{clip['end_frame']} 帧，源入点 {clip['source_in_seconds']:g} 秒，"
                         f"左上角 ({rect['x']}, {rect['y']})，尺寸 {rect['width']}×{rect['height']}。")
        for clip in shot["voice"]:
            lines.append(f"　{video.VOICE_NAMES[shot['voice_source']]}　{names[clip['file']]}（{clip['file']}）　"
                         f"全片第 {clip['start_frame']}–{clip['end_frame']} 帧，"
                         f"源文件 {clip['source_in_seconds']:g}–{clip['source_out_seconds']:g} 秒。")
        lines.append("")
    (folder / BRIEF).write_text("\n".join(lines), encoding="utf-8-sig")


def project_dir(root, project_id):
    return Path(root).resolve() / "样片" / project_id


def version_dir(root, project_id, version):
    return project_dir(root, project_id) / version


def create_preview(root, project, assets, report, *, voice_profile=""):
    if not confirmed(project) or not project["shots"]:
        raise ValueError("请先确认文案并保存分镜。")
    created = datetime.now().astimezone()
    version = created.strftime("%Y%m%d-%H%M%S-%f")
    dest = version_dir(root, project["id"], version)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".review-", dir=dest.parent) as work:
        temp = Path(work)
        folder = temp / "bundle"
        folder.mkdir()
        media = MediaCopies(folder)
        report("正在保存本版样片使用的素材和声音")
        project, used = freeze_inputs(planning.auto_library_subjects(project, assets, root, report), assets, media)
        rows = video.timeline(root, project, used, temp, report, need_picture=True, need_material_audio=True,
                              voice_profile=voice_profile)
        for row in rows:
            for slot, clips in row["layers"].items():
                row["layers"][slot] = [
                    replace(clip, path=media.keep(clip.path, f"镜头 {row['n']:02d} {SHOT_SLOTS[slot]} {i+1}{clip.path.suffix}"))
                    if clip.illustration_source is not None else clip for i, clip in enumerate(clips)]
            if row["source"] == "synth":
                row["speech"] = [(media.keep(path, f"镜头 {row['n']:02d} 合成配音.wav"), frames, offset, limit)
                                 for path, frames, offset, limit in row["speech"]]
        frames = sum(row["frames"] for row in rows)
        cues = sorted(video.caption_rows(project, rows), key=lambda cue: cue["start"])
        subtitles = folder / CAPTIONS
        video.write_srt(subtitles, cues, frames * 1000 // video.FPS)
        size = video.picture_size(project["orientation"], 360)
        picture = video.render_picture(rows, temp, size, report)
        stems, mix = video.sound_tracks(project, rows, temp, report)
        report("正在合成样片并记录时间轴")
        video.mux_video(picture, mix, folder / SAMPLE, video.CAPTION_MODES[1], subtitles if cues else None,
                        caption_style=project["captions"].get("style"))
        (folder / "audio").mkdir()
        mix.rename(folder / "audio/mix.wav")
        stem_files = {name: f"audio/{STEMS[name]}.wav" for name in stems}
        for name, path in stems.items():
            path.rename(folder / stem_files[name])
        manifest = make_manifest(folder, project, rows, media, cues, size, created, version, stem_files)
        (folder / TIMELINE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        write_brief(folder, manifest)
        folder.rename(dest)
    return {"version": version, "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
            "orientation": project["orientation"], "duration_ms": manifest["canvas"]["duration_ms"], "shots": len(rows)}


def export_package(folder, destination, report):
    folder = Path(folder).resolve()
    destination = Path(destination).resolve()
    if not (folder / TIMELINE).is_file():
        raise ValueError("找不到所选版本的制作记录，请重新合成样片。")
    manifest = json.loads((folder / TIMELINE).read_text(encoding="utf-8"))
    audio = manifest["audio"]
    members = [BRIEF, TIMELINE, manifest["preview"]["file"], manifest["captions"]["file"], audio["mix"],
               *audio["stems"].values(), *(asset["file"] for asset in manifest["media"])]
    members = list(dict.fromkeys(members))
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        ready = Path(tmp) / "package.zip"
        with ZipFile(ready, "w") as archive:
            for i, name in enumerate(members, 1):
                source = (folder / name).resolve()
                if not source.is_relative_to(folder) or Path(name).is_absolute() or not source.is_file():
                    raise ValueError(f"样片文件「{name}」缺失或路径无效，请重新合成。")
                if source == destination:
                    raise ValueError("请选择新的 ZIP 文件名。")
                report(f"正在打包 {i} / {len(members)} · {source.name}")
                compression = ZIP_DEFLATED if source.suffix.lower() in (".wav", ".txt", ".srt", ".json") else ZIP_STORED
                archive.write(source, source.relative_to(folder).as_posix(), compress_type=compression, compresslevel=1)
        os.replace(ready, destination)
    return str(destination)
