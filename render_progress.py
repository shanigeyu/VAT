"""Durable, input-specific render checkpoints; incomplete files are never reused."""

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from pathlib import Path

import task_control
from speech_api import AudioModel


_current = ContextVar("vat_render_progress", default=None)
CACHE_VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def file_state(path):
    path = Path(path)
    try:
        stat = path.stat()
        return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [str(path), None]


def render_identity(root, project, assets, final, voice_profile):
    # Exclude bookkeeping so saving a checkpoint does not invalidate its media.
    content = {k: v for k, v in project.items() if k not in ("exports", "previews", "auto_video")}
    auto = project.get("auto_video", {})
    content["auto_video"] = {k: auto[k] for k in ("request", "plan", "models", "voice_profile") if k in auto}
    if "models" in auto:
        content["auto_video"]["models"] = {role: selection for role, selection in auto["models"].items()
                                           if role != "render"}
    media = [file_state(asset["path"]) for asset in assets.values()]
    for shot in project["shots"]:
        if path := shot.get("voice_file"):
            media.append(file_state(path))
        if filename := (shot.get("art") or {}).get("image_file"):
            media.append(file_state(Path(root) / "原创素材" / project["id"] / filename))
    for path in (project.get("sfx"), project.get("sound", {}).get("background")):
        if path:
            media.append(file_state(path))
    voice = asdict(voice_profile) if is_dataclass(voice_profile) else str(voice_profile)
    if isinstance(voice_profile, (str, Path)) and str(voice_profile):
        media.append(file_state(voice_profile))
    if voice_profile and not is_dataclass(voice_profile) and any(
            shot.get("voice_source", "camera" if shot["kind"] == "talk" else "synth") == "synth"
            for shot in project["shots"]):
        import sovits
        voice = sovits.load_profile(voice_profile)
        media.append(file_state(voice["reference"]))
    if isinstance(voice_profile, AudioModel):
        voice = ["audio-model", voice]
    # Tool and font changes must also invalidate an earlier encode.
    from app_paths import get_path
    tools = {name: get_path(name) for name in ("ffmpeg_dir", "font_file")}
    if tools["font_file"]:
        media.append(file_state(tools["font_file"]))
    return digest([CACHE_VERSION, content, media, final, voice, tools])


class RenderProgress:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest = self.directory / "completed.json"
        try:
            self.completed = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(self.completed, dict):
                self.completed = {}
        except (OSError, ValueError):
            self.completed = {}
        self.hashes = {}

    def key(self, path, identity):
        path = Path(path).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            return None
        return path.relative_to(self.directory.resolve()).as_posix(), digest(identity)

    def ready(self, name, identity):
        saved = self.completed.get(name, {})
        state = file_state(self.directory / name)
        return saved.get("identity") == identity and saved.get("file") == state and len(state) == 3 and state[1] > 0

    def remember(self, name, identity):
        state = file_state(self.directory / name)
        if len(state) != 3 or not state[1]:
            raise ValueError("制作步骤没有生成完整文件，无法保存进度。")
        self.completed[name] = {"identity": identity, "file": state}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as handle:
            scratch = Path(handle.name)
            json.dump(self.completed, handle, ensure_ascii=False)
        try:
            os.replace(scratch, self.manifest)
        finally:
            scratch.unlink(missing_ok=True)

    def source(self, path):
        path = Path(path)
        if not path.is_file():
            return str(path)
        state = file_state(path)
        # Generated inputs can be rewritten deterministically on resume. Their
        # content, rather than the rewrite time, identifies downstream work.
        if path.resolve().is_relative_to(self.directory.resolve()):
            key = tuple(state)
            if key not in self.hashes:
                with path.open("rb") as handle:
                    self.hashes[key] = hashlib.file_digest(handle, "sha256").hexdigest()
            return [str(path), self.hashes[key]]
        return state


@contextmanager
def workspace(root, project, assets, final, voice_profile):
    identity = render_identity(root, project, assets, final, voice_profile)
    directory = Path(root) / "制作进度" / project["id"] / identity
    cache_root = (Path(root) / "制作进度").resolve()
    if directory.resolve() == cache_root or not directory.resolve().is_relative_to(cache_root):
        raise ValueError("项目的制作进度路径无效。")
    progress = RenderProgress(directory)
    token = _current.set(progress)
    try:
        yield directory
    except BaseException:
        raise
    else:
        # Completed outputs have been published. Keep scratch media only while
        # a job is unfinished, preserving unversioned voice behavior on re-export.
        shutil.rmtree(directory, ignore_errors=True)
    finally:
        _current.reset(token)


@contextmanager
def output(path, identity):
    task_control.checkpoint()
    progress = _current.get()
    key = progress.key(path, identity) if progress is not None else None
    if key is not None and progress.ready(*key):
        yield True
        return
    try:
        yield False
    except BaseException:
        # A stopped encoder can leave a nonempty, truncated file.
        if key is not None:
            Path(path).unlink(missing_ok=True)
        raise
    else:
        if key is not None:
            progress.remember(*key)


def command_identity(args):
    progress = _current.get()
    sources = [progress.source(args[i+1]) for i, arg in enumerate(args[:-1])
               if str(arg) == "-i"] if progress is not None else []
    return [list(map(str, args)), sources]
