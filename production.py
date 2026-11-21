import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from app_paths import get_path

import sovits
import planning
import speech_api
import generated_images
import render_progress
import task_control
from creative_art import render_artwork
from shot_visuals import SceneIllustrator, font, lines_for
from store import IMAGES, VIDEOS, SHOT_SLOTS, confirmed, script_units, shot_clips, uid
from store import video_size as picture_size


FPS = 25
LAYER_ORDER = ("theme", "main", "effect")
CANVAS_COLOR = "171a21"
SUBTITLE_STYLE = "系统或自选字体，白字深色描边，底部居中，每屏最多两行"
CAPTION_STYLES = ("标准描边", "紧凑底栏", "白字微阴影", "纯白字幕")
NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tool(name):
    directory = get_path("ffmpeg_dir")
    command = str(Path(directory) / (name + (".exe" if os.name == "nt" else ""))) if directory else name
    found = shutil.which(command)
    if found:
        return found
    raise ValueError(f"找不到 {name}。请在设置中指定同时包含 ffmpeg 和 ffprobe 的目录，或将其加入 PATH。")


def run(args, *, env=None, timeout=900, cwd=None):
    try:
        p = task_control.run_process(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                     env=env, cwd=cwd, creationflags=NOWINDOW)
    except subprocess.TimeoutExpired:
        raise ValueError("处理超时，请缩短素材或分段制作。") from None
    if p.returncode:
        detail = p.stderr.decode("utf-8", "replace").strip()[-750:]
        raise ValueError("处理失败。\n" + detail)
    return p.stdout


def ffmpeg(*args, cwd=None):
    command = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)]
    output = Path(str(args[-1]))
    # Pipes and calls outside a resumable render retain their usual behavior.
    with render_progress.output(output, render_progress.command_identity(command)) as ready:
        if ready:
            return b""
        return run(command, cwd=cwd)


def probe(path):
    args = [tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    return json.loads(run(args, timeout=30))


def media_stream(path, metadata, kind):
    for stream in metadata["streams"]:
        if stream["codec_type"] == kind and not stream.get("disposition", {}).get("attached_pic"):
            return stream
    track = "音频" if kind == "audio" else "视频"
    raise ValueError(f"「{path.name}」缺少所需的{track}轨。")


def media_seconds(path, metadata, kind):
    stream = media_stream(path, metadata, kind)
    for value in (stream.get("duration"), metadata.get("format", {}).get("duration")):
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    raise ValueError(f"无法读取「{path.name}」的时长。")


def output_dir(root, p):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", p["title"]).rstrip(". ")[:48] or "新一期"
    d = root / "导出" / (safe + "_" + p["id"][:6])
    d.mkdir(parents=True, exist_ok=True)
    return d


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uid()[:4]


def synthesize_api(text, path, profile, key):
    if isinstance(profile, speech_api.AudioModel):
        request_wav = speech_api.request_model_wav
        source = "大语言模型"
    else:
        request_wav = speech_api.request_wav
        source = "语音 API"
    response = path.with_name("speech-response.wav")
    response.write_bytes(request_wav(text, profile, key))
    # Streaming WAV headers may lack a final length. Write a standard PCM WAV for the timeline.
    try:
        ffmpeg("-f", "wav", "-i", response, "-map", "0:a:0", "-ar", 48000, "-ac", 1, "-c:a", "pcm_s16le", path)
    except ValueError:
        raise ValueError(f"{source} 返回的 WAV 无法解码。请检查服务或重试。") from None
    with wave.open(str(path), "rb") as audio:
        if not any(chunk.strip(b"\x00") for chunk in iter(lambda: audio.readframes(65536), b"")):
            raise ValueError(f"{source} 返回了空音频或静音，请检查文案和音色。")
        return audio.getnframes() / audio.getframerate()


def speech_files(root, units, temp, report, *, voice_profile=""):
    api = voice_profile.validated() if isinstance(voice_profile, (speech_api.Profile, speech_api.AudioModel)) else None
    if api is None:
        profile = sovits.load_profile(voice_profile)
        cache = root / "语音缓存" if profile["model_id"] else temp
        identity = ["gpt-sovits-v1", profile]
        source = "本机 GPT-SoVITS"
    else:
        cache = root / "语音缓存"
        if isinstance(api, speech_api.AudioModel):
            protocol = f"audio-model-{api.provider}-v1"
            source = "大语言模型"
        elif api.provider == "elevenlabs":
            protocol = "elevenlabs-speech-wav24000-v1"
            source = "ElevenLabs"
        else:
            protocol = "openai-speech-v1"
            source = "语音 API"
        identity = [protocol, api.endpoint, api.model, api.voice, api.encrypted_key]
    cache.mkdir(exist_ok=True, parents=True)
    signature = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    paths = [cache / (hashlib.sha256((signature + "\n" + text).encode("utf-8")).hexdigest() + ".wav")
             for text in units]
    missing = {path: text for path, text in zip(paths, units) if not path.is_file()}
    if not missing:
        return paths

    if api is None:
        sovits.ensure_service(profile, root, report)
        synthesize = partial(sovits.synthesize, profile=profile)
    else:
        tool("ffmpeg")
        synthesize = partial(synthesize_api, profile=api, key=api.credential())
    with tempfile.TemporaryDirectory(prefix="speech-", dir=cache) as scratch:
        ready = Path(scratch) / "voice.wav"
        for index, (path, text) in enumerate(missing.items(), 1):
            task_control.checkpoint()
            report(f"{source} 配音 {index} / {len(missing)}")
            try:
                synthesize(text, ready)
            except (ValueError, RuntimeError) as exc:
                raise ValueError(f"配音失败「{text[:36]}」\n{exc}") from exc
            os.replace(ready, path)
    return paths


def join_wav(files, output):
    if not files:
        raise ValueError("没有可以拼接的语音。")
    with wave.open(str(output), "wb") as out:
        for i, p in enumerate(files):
            with wave.open(str(p), "rb") as w:
                if i == 0:
                    out.setparams(w.getparams())
                elif w.getparams()[:3] != out.getparams()[:3]:
                    raise ValueError("音频采样格式不一致。")
                while chunk := w.readframes(65536):
                    task_control.checkpoint()
                    out.writeframesraw(chunk)
    with wave.open(str(output), "rb") as w:
        return w.getnframes() / w.getframerate()


def export_voice(root, project, report, *, voice_profile=""):
    if not confirmed(project):
        raise ValueError("请先确认当前文案。")
    folder = output_dir(root, project)
    output = folder / ("定稿配音_" + stamp() + ".wav")
    with render_progress.workspace(root,project,{},"voice",voice_profile) as tmp:
        temp = Path(tmp)
        files = speech_files(root, script_units(project["approved"]), temp, report, voice_profile=voice_profile)
        ready = temp / "voice.wav"
        join_wav(files, ready)
        os.replace(ready, output)
    return [("定稿配音", str(output))]


def asset_path(assets, asset_id, n):
    if not asset_id:
        return None
    info = assets.get(asset_id)
    if info and not info.get("enabled", 1):
        raise ValueError(f"镜头 {n} 的素材「{info['name']}」已禁用，请替换或重新启用。")
    if not info or not Path(info["path"]).is_file():
        raise ValueError(f"镜头 {n} 的素材文件已移动，请重新关联。")
    return Path(info["path"])


def visual_input(path, offset=0, loop=True):
    if path.suffix.lower() in IMAGES:
        return ["-loop", "1", "-framerate", str(FPS), "-i", str(path)]
    args = ["-stream_loop", "-1"] if loop else []
    if offset:
        args += ["-ss", str(offset)]
    return [*args, "-i", str(path)]


@dataclass(frozen=True)
class VisualClip:
    path: Path
    start: int
    frames: int
    offset: float
    loop: bool = True
    source_seconds: float | None = None
    illustration_source: str | None = None
    rect: tuple[float, float, float, float] | None = None


def normalized_rect(values):
    """Validate a main clip's optional box in normalized canvas coordinates."""
    if values is None:
        return None
    message = "主体画框需填写左、上、宽、高四个 0 到 1 的数，宽高须大于 0，画框须在画布内。"
    if not isinstance(values, (tuple, list)) or len(values) != 4:
        raise ValueError(message)
    try:
        x, y, w, h = map(float, values)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(message) from None
    if (not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y, w, h))
            or not w or not h or x + w > 1 + 1e-12 or y + h > 1 + 1e-12):
        raise ValueError(message)
    return x, y, w, h


def layer_box(size, rect):
    width, height = size
    if rect is None:
        return 0, 0, width, height
    x, y, w, h = rect
    # Quantize both edges to the even pixel grid used by the 4:2:0 output.
    left = min(width - 2, 2 * round(x * width / 2))
    top = min(height - 2, 2 * round(y * height / 2))
    right = min(width, max(left + 2, 2 * round((x + w) * width / 2)))
    bottom = min(height, max(top + 2, 2 * round((y + h) * height / 2)))
    return left, top, right - left, bottom - top


def visual_sequence(assets, frames, n, slot):
    count = len(assets)
    if count > frames:
        raise ValueError(f"镜头 {n} 只有 {frames} 帧，请减少{SHOT_SLOTS[slot]}的素材数量或延长镜头。")
    sequence = []
    for index, (clip, path) in enumerate(assets):
        start = index * frames // count
        end = (index + 1) * frames // count
        sequence.append(VisualClip(path, start, end - start, clip.get("offset", 0),
                                   rect=normalized_rect(clip.get("rect")) if slot == "main" else None))
    return sequence


def illustrate_shot(illustrator, temp, number, shot, frames):
    clips = []
    for index, (plate, start, length, source) in enumerate(illustrator.plates(temp, number, shot, frames, FPS)):
        base, detail, (x, y, width, height) = plate
        seconds = max(1, length - 1) / FPS
        ease = f"(1-cos(PI*min(t/{seconds:.6f},1)))/2"
        horizontal = f"(iw-ow)*(.08+.66*{ease})" if index == 0 else f"(iw-ow)*(.50+.45*{ease})"
        vertical = f"(ih-oh)*(.04+.38*{ease})" if index == 0 else f"(ih-oh)*(.42+.51*{ease})"
        filt = (f"[1:v]crop={width}:{height}:x='{horizontal}':y='{vertical}',setsar=1[detail];"
                f"[0:v][detail]overlay={x}:{y},format=yuv420p[out]")
        path = temp / f"illustration-{number:02}-{index+1}.mp4"
        ffmpeg("-filter_complex_threads", 2, *visual_input(base), *visual_input(detail),
               "-filter_complex", filt, "-map", "[out]", "-an", "-frames:v", length, "-c:v", "libx264",
               "-crf", 17, "-preset", "veryfast", "-threads", 3, path)
        clips.append(VisualClip(path, start, length, 0, loop=False, illustration_source=source))
    return clips


def layer_sizing(slot, size, *, custom_box=False):
    w, h = size
    if slot == "theme":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if custom_box:
        # Contain the displayed aspect ratio. One chroma sample is the smallest
        # representable size on either axis of the delivery canvas.
        aspect = "if(gt(sar,0),dar,a)"
        return (f"scale=w='max(2,2*trunc(min({w},{h}*{aspect})/2))':"
                f"h='max(2,2*trunc(min({h},{w}/{aspect})/2))'")
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2"


def encode_picture(out, size, frames, layers):
    w, h = size
    args = ["-filter_complex_threads", "2", "-f", "lavfi", "-i", f"color=c=0x{CANVAS_COLOR}:s={w}x{h}:r={FPS}"]
    filters = ["[0:v]setsar=1,format=yuv420p[base0]"]
    layer = 0
    for slot in LAYER_ORDER:
        for clip in layers[slot]:
            layer += 1
            x, y, box_w, box_h = layer_box(size, clip.rect)
            sizing = layer_sizing(slot, (box_w, box_h), custom_box=clip.rect is not None)
            args.extend(visual_input(clip.path, clip.offset, loop=clip.loop))
            filters.append(f"[{layer}:V:0]fps={FPS},trim=end_frame={clip.frames},"
                           f"setpts=PTS-STARTPTS+{clip.start},{sizing},setsar=1,format=rgba[fg{layer}]")
            # The overlay keeps the clip's last frame through its own allotted time.
            enable = (f":enable='between(round(t*{FPS}),{clip.start},{clip.start+clip.frames-1})'"
                      if clip.frames < frames else "")
            filters.append(f"[base{layer-1}][fg{layer}]overlay={x}+({box_w}-w)/2:{y}+({box_h}-h)/2:"
                           f"eof_action=repeat{enable}[base{layer}]")
    ffmpeg(*args, "-filter_complex", ";".join(filters), "-map", f"[base{layer}]", "-an", "-frames:v", frames,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", 20, "-pix_fmt", "yuv420p", "-threads", 4, out)


AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
CAPTION_MODES = ("单独导出 SRT", "烧录到成片", "可开关字幕轨")
VOICE_NAMES = {"synth":"自动合成配音", "ai_file":"导入的 AI 配音", "record_file":"本人录音文件", "camera":"本人出镜原声"}


def voice_source(shot):
    return shot.get("voice_source") or ("camera" if shot["kind"]=="talk" else "synth")


def wav_seconds(path):
    with wave.open(str(path),"rb") as w:
        return w.getnframes()/w.getframerate()


def timeline(root,p,assets,temp,report,need_picture=False,need_material_audio=False,*,voice_profile=""):
    if not confirmed(p) or not p["shots"]:
        raise ValueError("请先确认定稿、划分段落并保存分镜。")
    auto_state = p.get("auto_video", {})
    needs_original = (auto_state.get("request", {}).get("visual_tier")
                      or auto_state.get("next_stage") in {"plan", "script", "shots", "assets", "images"})
    units=script_units(p["approved"])
    synth_indices=sorted({i for s in p["shots"] if voice_source(s)=="synth" for i in range(s["start"],s["end"])})
    waves={}
    wave_lengths = {}
    pace = 1.0
    if synth_indices:
        files=speech_files(root,[units[i] for i in synth_indices],temp,report,voice_profile=voice_profile)
        waves=dict(zip(synth_indices,files))
        wave_lengths = {i: wav_seconds(path) for i, path in waves.items()}
        if p.get("auto_video") and p.get("target_seconds") and all(voice_source(s) == "synth" for s in p["shots"]):
            ratio = sum(wave_lengths.values()) / p["target_seconds"]
            if abs(ratio - 1) > .04:
                pace = max(.8, min(1.25, ratio))
                report("正在按目标时长调整旁白语速")
    info={}
    def metadata(path):
        if path not in info:
            info[path]=probe(path)
        return info[path]
    def duration(path,typ):
        return media_seconds(path, metadata(path), typ)
    rows=[]
    cursor=0
    illustrator = None
    clip_audio = need_material_audio and p["sound"]["clip_audio"]
    for n,s in enumerate(p["shots"],1):
        task_control.checkpoint()
        src = voice_source(s)
        needs_camera = s["kind"] == "talk" or src == "camera"
        required = set(SHOT_SLOTS) if need_picture else {"main", "effect"} if clip_audio else set()
        if needs_camera:
            required.add("main")
        selected = {slot: [(clip, asset_path(assets, clip["id"], n)) for clip in shot_clips(s, slot)]
                    if slot in required else [] for slot in SHOT_SLOTS}
        camera = []
        camera_audio = []
        camera_frames = 0
        if needs_camera:
            if not selected["main"] or any(assets[clip["id"]]["category"] != "talk" for clip, _ in selected["main"]):
                raise ValueError(f"请先为镜头 {n} 选择本期口播视频。")
            for clip, path in selected["main"]:
                offset = clip.get("offset", 0)
                available = duration(path, "video") - offset
                seconds = min(clip.get("length") or available, available)
                if seconds <= 0:
                    raise ValueError(f"镜头 {n} 的口播入点超出「{path.name}」的时长。")
                frames = max(1, math.ceil(round(seconds * FPS, 9)))
                camera.append(VisualClip(path, camera_frames, frames, offset, loop=False, source_seconds=seconds,
                                         rect=normalized_rect(clip.get("rect"))))
                camera_frames += frames
                if src == "camera":
                    duration(path, "audio")
                    camera_audio.append((path, frames, offset, seconds))
        indices=range(s["start"],s["end"])
        weights=[max(1,len(units[i].strip())) for i in indices]
        audio_start=0
        if src=="synth":
            speech=temp/f"voice-{n}.wav"
            seconds=join_wav([waves[i] for i in indices],speech)
            if pace != 1.0:
                adjusted = temp / f"paced-{n}.wav"
                ffmpeg("-i", speech, "-af", f"atempo={pace:.5f}", "-c:a", "pcm_s16le", adjusted)
                speech, seconds = adjusted, wav_seconds(adjusted)
            weights=[wave_lengths[i] for i in indices]
        elif src in ("ai_file","record_file"):
            speech=Path(s.get("voice_file",""))
            if not speech.is_file():
                raise ValueError(f"镜头 {n} 缺少{VOICE_NAMES[src]}，请在音频制作中选择文件。")
            audio_start=float(s.get("voice_offset",0))
            available=duration(speech,"audio")-audio_start
            seconds=min(s.get("voice_length",0) or available,available)
            if seconds<=0:
                raise ValueError(f"镜头 {n} 的录音入点超出音频时长。")
        elif src != "camera":
            raise ValueError(f"镜头 {n} 的文案声音来源无效。")
        frames = camera_frames or max(1, math.ceil(round(seconds * FPS, 9)))
        speech_parts = camera_audio if src == "camera" else [(speech, frames, audio_start, seconds)]
        layers = {slot: camera if slot == "main" and needs_camera else visual_sequence(selected[slot], frames, n, slot)
                  for slot in SHOT_SLOTS}
        art = s.get("art") or {}
        overlay = None
        if need_picture and art:
            is_effect = art.get("layer") == "effect"
            if art.get("image_file") and not layers["main"] and (is_effect or not art.get("svg")):
                path = generated_images.file_path(root, p["id"], art["image_file"])
                if not path.is_file():
                    raise ValueError(f"第 {n} 镜的原创图片已移动，请恢复项目的原创素材或重选主体。")
                layers["main"] = [VisualClip(path, 0, frames, 0, illustration_source="AI 生图")]
            if art.get("svg") and (is_effect or not layers["main"]):
                report(f"正在绘制第 {n} 镜的原创画面与动效")
                path = render_artwork(art["svg"], temp, n, picture_size(p["orientation"]), frames, FPS, tool("ffmpeg"))
                clip = VisualClip(path, 0, frames, 0, loop=False, illustration_source="AI 原创图形")
                if is_effect:
                    overlay = clip
                else:
                    layers["main"] = [clip]
        if need_picture and not (layers["main"] or layers["theme"]):
            if needs_original:
                raise ValueError(f"第 {n} 镜尚无可渲染的主体。请在「一键生成视频」里续做动效，或手动补齐画面。")
            if illustrator is None:
                illustrator = SceneIllustrator(root, p, picture_size(p["orientation"]), report)
            report(f"正在按第 {n} 镜台词制作主体画面")
            layers["main"] = illustrate_shot(illustrator, temp, n, s, frames)
        if layers["effect"] and not (needs_camera or shot_clips(s, "main") or shot_clips(s, "theme")):
            # On illustrated shots, a transparent accent plays once at its assigned cut.
            accents = []
            for clip in layers["effect"]:
                if clip.path.suffix.lower() in IMAGES:
                    accents.append(clip)
                    continue
                remaining = duration(clip.path, "video") - clip.offset
                if remaining <= 0:
                    raise ValueError(f"镜头 {n} 的动效入点超出「{clip.path.name}」的时长。")
                length = min(clip.frames, max(1, math.ceil(remaining * FPS)), 2 * FPS)
                accents.append(VisualClip(clip.path, clip.start, length, clip.offset, loop=False))
            layers["effect"] = accents
        if overlay is not None:
            layers["effect"].append(overlay)
        ambience = []
        if clip_audio:
            for slot in (("effect",) if needs_camera else ("main", "effect")):
                spans = []
                sound_cursor = 0
                for clip in layers[slot]:
                    if clip.illustration_source is not None:
                        continue
                    if clip.start > sound_cursor:
                        spans.append((None, clip.start-sound_cursor, 0, None))
                    has_audio = clip.illustration_source is None and clip.path.suffix.lower() in VIDEOS and any(
                        stream["codec_type"] == "audio" for stream in metadata(clip.path)["streams"])
                    spans.append((clip.path if has_audio else None, clip.frames, clip.offset, None))
                    sound_cursor = clip.start + clip.frames
                if sound_cursor < frames:
                    spans.append((None, frames - sound_cursor, 0, None))
                if any(path is not None for path, _, _, _ in spans):
                    ambience.append(spans)
        rows.append(dict(n=n, shot=s, layers=layers, start=cursor, frames=frames,
                         speech=speech_parts, source=src, weights=weights, ambience=ambience))
        cursor+=frames
    return rows


def srt_clips(shot):
    if shot["kind"] != "talk":
        return []
    return [(i, clip) for i, clip in enumerate(shot_clips(shot, "main")) if clip.get("srt")]


def imported_caption_rows(shot, number, row, clips):
    for index, clip in clips:
        source_start = round(1000 * clip.get("offset", 0))
        length = round(1000 * clip.get("length", 0)) or None
        placed_start = 0 if number == 1 and index == 0 else None
        if row is not None:
            visual = row["layers"]["main"][index]
            placed_start = (row["start"] + visual.start) * 1000 // FPS
            frame_length = visual.frames * 1000 // FPS
            length = min(length or frame_length, frame_length)
        for i, cue in enumerate(clip["srt"]):
            start = max(0, cue["start"] - source_start)
            end = cue["end"] - source_start
            if length is not None:
                end = min(end, length)
            if end <= start:
                continue
            key = f"{shot['start']}:srt:{clip['id']}:{cue['start']}:{i}"
            yield {"i": key, "shot": number, "text": cue["text"],
                   "start": placed_start + start if placed_start is not None else None,
                   "end": placed_start + end if placed_start is not None else None}


def caption_rows(p, rows=None, apply_edits=True):
    units = script_units(p["approved"])
    if rows is None and not p["shots"]:
        return [{"i": i, "shot": "—", "start": None, "end": None, "text": text} for i, text in enumerate(units)]
    result = []
    for n, shot in enumerate(p["shots"], 1):
        row = rows[n - 1] if rows is not None else None
        clips = srt_clips(shot)
        if clips:
            result.extend(imported_caption_rows(shot, n, row, clips))
            continue
        indices = range(shot["start"], shot["end"])
        weights = row["weights"] if row is not None else [0] * len(indices)
        total = sum(weights)
        acc = 0
        for i, weight in zip(indices, weights):
            start = end = None
            if row is not None:
                start = round((row["start"] + row["frames"] * acc / total) * 1000 / FPS)
                acc += weight
                end = round((row["start"] + row["frames"] * acc / total) * 1000 / FPS)
            result.append({"i": i, "shot": n, "start": start, "end": end, "text": units[i]})
    if apply_edits:
        for cue in result:
            cue.update(p["captions"]["edits"].get(str(cue["i"]), {}))
    return result


def srt_time(ms):
    h,rem=divmod(ms,3600000)
    m,rem=divmod(rem,60000)
    s,ms=divmod(rem,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(path,cues,end_ms):
    lines=[]
    for n,c in enumerate(sorted(cues, key=lambda cue: cue["start"]),1):
        if not 0<=c["start"]<c["end"]<=end_ms or not c["text"].strip():
            raise ValueError(f"字幕 {n} 的时间或正文有误，请在字幕制作中调整。")
        lines.append(f"{n}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text'].strip()}\n")
    path.write_text("\n".join(lines),encoding="utf-8-sig")


def silent(path,frames):
    samples=frames*(48000//FPS)
    with wave.open(str(path),"wb") as w:
        w.setparams((2,2,48000,0,"NONE","not compressed"))
        while samples:
            task_control.checkpoint()
            n=min(48000,samples)
            w.writeframesraw(b"\0"*4*n)
            samples-=n


def audio_part(source,out,frames,gain,start=0,limit=None,loop=False,norm=False):
    args=["-stream_loop","-1"] if loop else []
    if start:
        args+=["-ss",str(start)]
    duration=frames/FPS
    af=f"atrim=duration={min(limit or duration,duration):.7f},asetpts=PTS-STARTPTS,"
    if norm:
        af+="loudnorm=I=-16:TP=-1.5:LRA=11,"
    samples=frames*(48000//FPS)
    af+=f"aresample=48000,volume={gain/100},apad=whole_len={samples},atrim=end_sample={samples},asetpts=N/SR/TB"
    ffmpeg(*args,"-i",source,"-map","0:a:0","-af",af,"-ar",48000,"-ac",2,"-c:a","pcm_s16le",out)


def mix_wav(parts,out):
    args=[]
    for path in parts:
        args+=["-i",path]
    # Source WAVs have already been trimmed/padded to the exact timeline length.
    filt="".join(f"[{i}:a]" for i in range(len(parts)))+f"amix=inputs={len(parts)}:duration=first:normalize=0,alimiter=limit=0.96:level=0:latency=1[m]"
    ffmpeg(*args,"-filter_complex",filt,"-map","[m]","-c:a","pcm_s16le",out)


def audio_sequence(spans, temp, stem, gain, *, loop=False, norm=False):
    parts = []
    for i, (path, frames, offset, limit) in enumerate(spans):
        part = temp / f"{stem}-{i}.wav"
        if path is None:
            silent(part, frames)
        else:
            audio_part(path, part, frames, gain, offset, limit, loop=loop, norm=norm)
        parts.append(part)
    if len(parts) == 1:
        return parts[0]
    joined = temp / f"{stem}.wav"
    join_wav(parts, joined)
    return joined


def sound_tracks(p,rows,temp,report):
    cfg=p["sound"]
    pieces={"AI文案":[],"本人文案":[],"素材声":[]}
    report("正在对齐素材声、背景声和两类文案声")
    for r in rows:
        n,f=r["n"],r["frames"]
        mute=temp/f"sil-{n}.wav"
        silent(mute,f)
        person=r["source"] in ("camera","record_file")
        voice = audio_sequence(r["speech"], temp, f"spoken-{n}", cfg["human_gain" if person else "ai_gain"], norm=True)
        pieces["AI文案"].append(mute if person else voice)
        pieces["本人文案"].append(voice if person else mute)
        sources = [audio_sequence(spans, temp, f"ambient-{n}-{j}", p["sfx_volume"], loop=True)
                   for j, spans in enumerate(r["ambience"])]
        if len(sources)>1:
            combined=temp/f"ambientmix-{n}.wav"
            mix_wav(sources,combined)
        else:
            combined=sources[0] if sources else mute
        pieces["素材声"].append(combined)
    stems={}
    for i,(name,parts) in enumerate(pieces.items()):
        stems[name]=temp/f"track-{i}.wav"
        join_wav(parts,stems[name])
    frames=sum(r["frames"] for r in rows)
    if p["sfx"]:
        extra=temp/"source-fx.wav"
        audio_part(Path(p["sfx"]),extra,frames,p["sfx_volume"])
        ready=temp/"material-combined.wav"
        mix_wav([stems["素材声"],extra],ready)
        stems["素材声"]=ready
    bg=temp/"background.wav"
    if cfg["background"]:
        audio_part(Path(cfg["background"]),bg,frames,cfg["bg_gain"],loop=True)
    else:
        silent(bg,frames)
    stems["背景声"]=bg
    mix=temp/"master.wav"
    mix_wav(list(stems.values()),mix)
    return stems,mix


def caption_preview(root,p,assets,report,*,voice_profile=""):
    with render_progress.workspace(root,p,assets,"captions",voice_profile) as d:
        rows=timeline(root,p,assets,Path(d),report,voice_profile=voice_profile)
        return caption_rows(p,rows),sum(r["frames"] for r in rows)*1000//FPS


def export_tracks(root,p,assets,report,*,voice_profile=""):
    folder,version=output_dir(root,p),stamp()
    p = planning.auto_library_subjects(p, assets, root, report)
    with render_progress.workspace(root,p,assets,"tracks",voice_profile) as d:
        temp=Path(d)
        rows=timeline(root,p,assets,temp,report,need_material_audio=True,voice_profile=voice_profile)
        stems,mix=sound_tracks(p,rows,temp,report)
        return publish(folder,version,{**stems,"混音":mix})


def export_subtitles(root,p,assets,report,*,voice_profile=""):
    cues,end=caption_preview(root,p,assets,report,voice_profile=voice_profile)
    folder=output_dir(root,p)
    with tempfile.TemporaryDirectory(dir=folder) as d:
        path=Path(d)/"captions.srt"
        write_srt(path,cues,end)
        return publish(folder,stamp(),{"字幕":path})


def publish(folder,version,files, *, matching_sidecar=False):
    published=[]
    for name,path in files.items():
        basename = "完整成片" if name == "字幕" and matching_sidecar else name
        dest=folder/(basename+"_"+version+path.suffix)
        os.replace(path,dest)
        published.append((name,str(dest)))
    return published


def render_picture(rows, temp, size, report):
    parts = []
    for row in rows:
        n = row["n"]
        report(f"正在制作镜头 {n} / {len(rows)}")
        path = temp / f"v{n}.mp4"
        encode_picture(path, size, row["frames"], row["layers"])
        parts.append(path)
    listing = temp / "parts.txt"
    listing.write_text("".join(f"file '{path.name}'\n" for path in parts), encoding="ascii")
    visual = temp / "visual.mp4"
    ffmpeg("-f", "concat", "-safe", 1, "-i", listing, "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", visual)
    return visual


def caption_pages(text, face, width):
    pages = []
    for paragraph in text.split("\n\n"):
        screen = ""
        # Keep whole spoken clauses on screen when a long cue needs multiple pages.
        fragments = re.findall(r"[^，,。！？!?；;：:]+[，,。！？!?；;：:]*|[，,。！？!?；;：:]+", paragraph)
        for fragment in fragments:
            if screen and face.getlength((screen + fragment).replace("\n", "")) > width * 1.45:
                pages.append(screen.strip())
                screen = ""
            screen += fragment
        if screen.strip():
            pages.append(screen.strip())
    wrapped = []
    for page in pages:
        width_used = face.getlength(page.replace("\n", ""))
        target = width
        if "\n" not in page and width < width_used < 1.85 * width:
            target = min(width, width_used * .5 + face.size)
        lines = lines_for(page, face, target)
        wrapped.extend("\n".join(lines[i:i+2]) for i in range(0, len(lines), 2))
    return wrapped


def burn_subtitles(source, folder, size, style_name=None):
    from talk_import import read_srt

    width, height = size
    scale = min(size) / 720
    font_size = max(12, round((40 if height > width else 36) * scale))
    margin = round(width * .07)
    bottom = round(height * (.077 if height > width else .055))
    compact = style_name == CAPTION_STYLES[1]
    if compact:
        font_size = max(12, round(36 * scale))
        bottom = round(height * .044)
    face = font(font_size, bold=True)
    family = re.sub(r"[,\r\n]", " ", face.getname()[0])
    if get_path("font_file"):
        # A selected font need not be installed. libass reads this temporary directory.
        fonts = folder / "fonts"
        fonts.mkdir(exist_ok=True)
        selected = Path(face.path)
        dest_font = fonts / ("custom" + selected.suffix)
        try:
            os.link(selected, dest_font)
        except OSError:
            shutil.copyfile(selected, dest_font)
    display = []
    for cue in read_srt(source):
        pages = caption_pages(cue["text"], face, width - 2 * margin - font_size // 2)
        weights = [max(1, len(page.replace("\n", ""))) for page in pages]
        total = sum(weights)
        elapsed = 0
        start = cue["start"]
        for page, weight in zip(pages, weights):
            elapsed += weight
            end = cue["start"] + round((cue["end"]-cue["start"]) * elapsed/total)
            if end > start:
                display.append({"text": page, "start": start, "end": end})
            start = end
    if not display:
        raise ValueError("字幕时间轴为空，请检查定稿和字幕编辑。")
    screen = folder / "screen.srt"
    write_srt(screen, display, max(cue["end"] for cue in display))
    ass = folder / "screen.ass"
    ffmpeg("-i", screen, "-c:s", "ass", ass)
    events = ass.read_text(encoding="utf-8-sig").split("[Events]", 1)[1]
    border_style, outline, shadow = (3, 8, 0) if compact else (1, 2.5, 1)
    outline_color = "&H00121914" if compact else "&H00121B20"
    shadow_color = "&H80121B20"
    if style_name == "白字微阴影":
        outline, shadow = 0, 1.5
        shadow_color = "&H80000000"
    elif style_name == "纯白字幕":
        outline, shadow = 0, 0
    border = f"{border_style},{outline * scale:.2f},{shadow * scale:.2f}"
    style = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
             f"PlayResX: {width}\nPlayResY: {height}\n\n[V4+ Styles]\n"
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
             "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding\n"
             f"Style: Default,{family},{font_size},&H00FFFFFF,&H00FFFFFF,{outline_color},{shadow_color},"
             f"{0 if compact else -1},0,0,0,100,100,0,0,{border},2,{margin},{margin},{bottom},1\n\n")
    ass.write_text(style + "[Events]" + events, encoding="utf-8-sig")
    return ass


def mux_video(visual, mix, output, mode, subtitles=None, *, caption_style=None):
    visual, mix, output = (Path(p).resolve() for p in (visual, mix, output))
    args = ["-i", visual, "-i", mix]
    video_args = ["-c:v", "copy"]
    if subtitles and mode == CAPTION_MODES[2]:
        args += ["-i", subtitles]
        video_args += ["-map", "2:s:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=zho", "-disposition:s:0", "default"]
    elif subtitles and mode == CAPTION_MODES[1]:
        track = media_stream(visual, probe(visual), "video")
        with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
            ass = burn_subtitles(subtitles, Path(scratch), (track["width"], track["height"]), caption_style)
            filter_spec = f"ass={ass.name}" + (":fontsdir=fonts" if (ass.parent / "fonts").is_dir() else "")
            ffmpeg(*args, "-map", "0:v:0", "-map", "1:a:0", "-vf", filter_spec,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", 18, "-threads", 4,
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", output, cwd=scratch)
        return
    ffmpeg(*args, "-map", "0:v:0", "-map", "1:a:0", *video_args, "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", output, cwd=subtitles.parent if subtitles else output.parent)


def render(root,p,assets,final,report,*,voice_profile=""):
    folder,version=output_dir(root,p),stamp()
    size=picture_size(p["orientation"])
    with render_progress.workspace(root,p,assets,final,voice_profile) as d:
        temp=Path(d)
        p = planning.auto_library_subjects(p, assets, root, report)
        rows=timeline(root,p,assets,temp,report,need_picture=True,need_material_audio=final,voice_profile=voice_profile)
        visual=render_picture(rows, temp, size, report)
        files={"无字幕画面":visual}
        if final:
            _,mix=sound_tracks(p,rows,temp,report)
            mode=p["captions"]["mode"]
            subtitles=temp/"captions.srt"
            cues = caption_rows(p, rows)
            if not cues and mode != CAPTION_MODES[0]:
                raise ValueError("所选字幕模式需要有效字幕，请检查分镜范围或口播字幕入点。")
            if cues:
                write_srt(subtitles,cues,sum(r["frames"] for r in rows)*1000//FPS)
                files["字幕"]=subtitles
            ready=temp/"final.mp4"
            mux_video(visual, mix, ready, mode, files.get("字幕"), caption_style=p["captions"].get("style"))
            files["完整成片"]=ready
        return publish(folder,version,files, matching_sidecar=final and mode == CAPTION_MODES[0])
