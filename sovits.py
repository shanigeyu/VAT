import ctypes
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import wave
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import yaml
import task_control
from app_paths import get_path


DEFAULT_URL = "http://127.0.0.1:9880"
STOCK_MODEL = "v2pro-nvidia50-20250604-stock"
MAX_WAV_BYTES = 32 * 1024 * 1024


def endpoint(url):
    try:
        parts = urlsplit(url)
        address = ipaddress.ip_address("127.0.0.1" if parts.hostname == "localhost" else parts.hostname)
        port = parts.port if parts.port is not None else 80
        if (parts.scheme == "http" and address.is_loopback and "%" not in str(address)
                and port > 0 and parts.username is None and parts.password is None
                and parts.path in ("", "/") and not parts.query and not parts.fragment):
            return str(address), port
    except ValueError:
        pass
    raise ValueError("GPT-SoVITS 地址须使用本机 HTTP 回环地址，例如 http://127.0.0.1:9880。")


def local_file(path, label):
    if os.fspath(path).startswith(("\\\\", "//")):
        raise ValueError(f"{label}须使用本机文件。")
    path = Path(path).expanduser().resolve()
    if str(path).startswith(("\\\\", "//")) or not path.is_file():
        raise ValueError(f"找不到本机{label}「{path}」，请在「音频制作 → 配音设置」中检查。")
    return path


def load_profile(override=""):
    if not override:
        raise ValueError("请在「配音设置」选择你自己的 GPT-SoVITS 音色 JSON。")
    path = local_file(override, "音色配置")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取音色配置「{path.name}」：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("GPT-SoVITS 音色配置须为 JSON 对象。")
    profile = {}
    defaults = {"reference": "", "prompt_text": "", "prompt_lang": "zh", "text_lang": "zh",
                "base_url": DEFAULT_URL, "model_id": ""}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if not isinstance(value, str) or (key != "model_id" and not value.strip()):
            raise ValueError(f"GPT-SoVITS 音色配置中的 {key} 缺失或无效。")
        profile[key] = value.strip()
    if "://" in profile["reference"]:
        raise ValueError("参考音频须使用本机文件。")
    reference = local_file(path.parent / profile["reference"], "参考音频")
    with reference.open("rb") as audio:
        profile["reference_sha256"] = hashlib.file_digest(audio, "sha256").hexdigest()
    profile["reference"] = str(reference)
    profile["address"] = endpoint(profile["base_url"])
    return profile


def api_ready(address):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", "/openapi.json")
        with connection.getresponse() as response:
            if response.status == 200:
                routes = json.loads(response.read(512 * 1024))["paths"]
                return "post" in routes.get("/tts", {}) and "/set_gpt_weights" in routes
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return False
    finally:
        connection.close()
    return False


def launch_model(root, port, report):
    directory = get_path("sovits_dir")
    if not directory:
        raise ValueError("请先启动本机 GPT-SoVITS API，或在设置中选择 GPT-SoVITS 安装目录以自动启动。")
    installation = Path(directory)
    api = installation / "api_v2.py"
    source = installation / "GPT_SoVITS/configs/tts_infer.yaml"
    if not all(path.is_file() for path in (api, source)):
        raise ValueError(f"「{installation}」缺少 api_v2.py 或 GPT_SoVITS/configs/tts_infer.yaml，请检查设置。")
    selected = get_path("sovits_python")
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    candidates = ([Path(selected)] if selected else
                  [installation / "runtime" / ("python.exe" if os.name == "nt" else "bin/python"),
                   installation / ".venv" / suffix, installation / "venv" / suffix])
    python = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if not python:
        raise ValueError("请在设置中选择已安装 GPT-SoVITS 依赖的 Python 解释器，或手动启动本机 API。")
    try:
        config = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
        config["custom"] = dict(config["v2Pro"])
    except (OSError, ValueError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise ValueError("无法读取本机 GPT-SoVITS 的 v2Pro 权重配置。") from exc

    report("正在启动本机 GPT-SoVITS v2Pro")
    state = root.resolve() / "GPT-SoVITS"
    state.mkdir(exist_ok=True, parents=True)
    run_id = f"{port}-{time.time_ns()}"
    settings = state / f"tts-{run_id}.yaml"
    settings.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    log = state / f"api-{run_id}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    search = [str(python.parent), str(installation)]
    ffmpeg_dir = get_path("ffmpeg_dir")
    if ffmpeg_dir:
        search.insert(0, ffmpeg_dir)
    env["PATH"] = os.pathsep.join([*search, env.get("PATH", "")])
    command = [str(python), str(api), "-a", "127.0.0.1", "-p", str(port), "-c", str(settings)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with log.open("wb") as output:
        bundle = getattr(sys, "_MEIPASS", None)
        # PyInstaller's DLL search path belongs to VAT, not the model's Python runtime.
        if bundle and os.name == "nt":
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            process = subprocess.Popen(command, cwd=installation, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, creationflags=flags,
                                       start_new_session=os.name != "nt")
        finally:
            if bundle and os.name == "nt":
                ctypes.windll.kernel32.SetDllDirectoryW(str(bundle))

    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline and process.poll() is None:
            task_control.checkpoint()
            if api_ready(("127.0.0.1", port)) and process.poll() is None:
                return
            task_control.sleep(0.8)
        raise RuntimeError(f"本机 GPT-SoVITS 未能启动，日志在「{log}」。")
    except BaseException:
        if process.poll() is None:
            task_control.stop_process(process)
        raise


def ensure_service(profile, root, report):
    address = profile["address"]
    if api_ready(address):
        return
    if address[0] != "127.0.0.1" or profile["model_id"] not in ("", STOCK_MODEL):
        raise RuntimeError(f"无法连接 {profile['base_url']}，请启动该音色配置对应的本机 GPT-SoVITS api_v2.py。")
    try:
        with socket.create_connection(address, timeout=0.5):
            raise RuntimeError(f"端口 {address[1]} 已有服务监听，请检查本机 GPT-SoVITS API。")
    except OSError:
        launch_model(root, address[1], report)


def synthesize(text, path, profile):
    payload = dict(text=text, ref_audio_path=profile["reference"], prompt_text=profile["prompt_text"],
                   prompt_lang=profile["prompt_lang"], text_lang=profile["text_lang"],
                   media_type="wav", streaming_mode=False, seed=0)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection(*profile["address"], timeout=180)
    def fetch():
        try:
            connection.request("POST", "/tts", body=body,
                               headers={"Content-Type": "application/json", "Accept": "audio/wav"})
            with connection.getresponse() as response:
                if response.status != 200:
                    detail = response.read(1024).decode("utf-8", "replace").strip()
                    raise RuntimeError(f"本机 GPT-SoVITS 请求失败（HTTP {response.status}）。{detail}")
                return response.read(MAX_WAV_BYTES + 1)
        finally:
            connection.close()
    try:
        audio = task_control.network_call(fetch, close=connection.close)
    except TimeoutError as exc:
        raise RuntimeError("本机 GPT-SoVITS 推理超时，请检查模型服务或缩短这一句。") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"无法在 {profile['base_url']} 完成本机 GPT-SoVITS 请求。") from exc
    if len(audio) > MAX_WAV_BYTES:
        raise ValueError("单句配音超过 32 MiB，请分成短句再试。")
    try:
        with wave.open(BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            pcm = wav.readframes(frames)
            if frames < 1 or len(pcm) != frames * wav.getnchannels() * wav.getsampwidth():
                raise ValueError("本机 GPT-SoVITS 返回的配音为空或数据不完整。")
            silence = b"\x80" if wav.getsampwidth() == 1 else b"\x00"
            if not pcm.strip(silence):
                raise ValueError("本机 GPT-SoVITS 返回了静音，请检查本句文案、参考音频和对应原话。")
    except (EOFError, wave.Error) as exc:
        raise ValueError("本机 GPT-SoVITS 返回的数据无法解码为 PCM WAV。") from exc
    path.write_bytes(audio)
