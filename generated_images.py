"""Materialize images requested by the visual director, once per project and model."""

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request

from PIL import Image, ImageOps, UnidentifiedImageError

import providers
import codex_harness
import task_control
from api_profiles import GEMINI, LOOPBACK_HOSTS, OPENAI, RESPONSES, base_url


PROTOCOLS = (OPENAI, RESPONSES, GEMINI)
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def image_data(settings, key, prompt):
    if settings["engine"] == "Codex":
        return codex_harness.generate_image(
            codex_harness.cli_path(settings.get("codex_path", "")), prompt,
            settings.get("codex_model", "").strip(), reasoning_effort=settings.get("codex_reasoning_effort") or "")
    protocol, model = settings["protocol"], settings["model"]
    if settings["engine"] != "API" or protocol not in PROTOCOLS:
        raise ValueError("请选择 Codex CLI，或支持生图的 OpenAI 兼容、Gemini API 及其生图模型。")
    base = base_url(settings["base"], protocol)
    auth = providers.api_auth(protocol, key)
    if protocol == GEMINI:
        endpoint = base + "/models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    else:
        endpoint = base + "/images/generations"
        body = {"model": model, "prompt": prompt, "n": 1}
    reply = providers.json_request(endpoint, body, timeout=240, max_bytes=MAX_IMAGE_BYTES*2, **auth)
    item = {}
    if isinstance(reply, dict):
        if protocol == GEMINI:
            for candidate in reply.get("candidates") or []:
                for part in candidate.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        item = {"b64_json": inline["data"]}
                        break
                if item:
                    break
        elif isinstance(reply.get("data"), list) and reply["data"]:
            item = reply["data"][0]
    if not isinstance(item, dict):
        raise ValueError("生图接口没有返回图片，请检查所选模型。")
    encoded = item.get("b64_json")
    if isinstance(encoded, str) and len(encoded) <= 4*((MAX_IMAGE_BYTES+2)//3):
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("生图接口返回了无效的图片数据。") from None
    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("所选模型没有返回图片，请检查生图能力或改选其他模型。")
    url = urljoin(base + "/", url)
    target, source = urlsplit(url), urlsplit(base)
    local = target.hostname in LOOPBACK_HOSTS
    if (not target.hostname or target.username is not None or target.password is not None
            or target.scheme not in ("https", "http") or (target.scheme == "http" or local)
            and (target.scheme, target.netloc) != (source.scheme, source.netloc)):
        raise ValueError("生图接口返回的图片地址无效。")
    # Signed CDN URLs carry their own authorization. Never forward the provider key here.
    req = Request(url, headers={"Accept": "image/*", "User-Agent": "VAT-Studio"})
    def fetch():
        with providers.api_opener(url).open(req, timeout=90) as response:
            return response.read(MAX_IMAGE_BYTES+1)
    try:
        data = task_control.network_call(fetch)
    except HTTPError as error:
        error.close()
        raise ValueError(f"下载生成的图片失败（HTTP {error.code}）。") from None
    except OSError:
        raise ValueError("下载生成的图片失败，请检查图像服务。") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("生成的图片超过 32 MB。")
    return data


def file_path(root, project_id, filename):
    if not re.fullmatch(r"[\w-]+", project_id) or not re.fullmatch(r"[0-9a-f]{64}\.png", filename):
        raise ValueError("项目中的原创图片引用无效。")
    return Path(root) / "原创素材" / project_id / filename


def create(root, project, prompt, settings, key):
    prompt = f"{prompt}\n画幅：{project['orientation']}。"
    if settings["engine"] == "Codex":
        identity = ["codex-image-generation-v1", settings.get("codex_path", ""), settings.get("codex_model", ""), prompt]
    else:
        identity = [settings["api_profile"], settings["base"], settings["protocol"], settings["model"], prompt]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    path = file_path(root, project["id"], digest + ".png")
    if path.is_file():
        return path.name
    data = image_data(settings, key, prompt)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as f:
        scratch = Path(f.name)
    try:
        with Image.open(BytesIO(data)) as im:
            if im.width * im.height > 32_000_000:
                raise ValueError("生成图片的分辨率超过 3200 万像素。")
            im = ImageOps.exif_transpose(im).convert("RGBA")
            im.save(scratch, format="PNG")
        task_control.checkpoint()
        os.replace(scratch, path)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("无法保存生成的图片，请检查生图结果。") from error
    finally:
        scratch.unlink(missing_ok=True)
    return path.name
