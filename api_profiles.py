import ctypes
import json
import os
import re
import stat
import tempfile
from ctypes import wintypes
from urllib.parse import unquote, urlsplit

from app_paths import config_dir

OPENAI = "OpenAI 兼容"
RESPONSES = "OpenAI Responses"
ANTHROPIC = "Anthropic Messages"
GEMINI = "Google Gemini"
PROTOCOLS = (OPENAI, RESPONSES, ANTHROPIC, GEMINI)
AUTO_PROTOCOL = "自动识别"
KIND_LABELS = {"model": "模型", "agent": "Agent"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
GEMINI_GENERATE_PATH = re.compile(r"/models/([^/]+):generateContent$")
ADDRESS_ERROR = "请填写有效的接口地址，例如 http://localhost:11434/v1。"

FIELD_ALIASES = {
    "name": ("名称", "配置名称", "显示名", "name", "display_name"),
    "kind": ("标记", "类型", "kind", "target_type"),
    "protocol": ("Base URL 协议", "协议", "protocol", "base_url_protocol"),
    "base": ("Base URL", "接口地址", "接口根地址", "base_url", "base", "endpoint", "url"),
    "key": ("API 密钥", "API Key", "密钥", "api_key", "key"),
    "model": ("模型", "模型名称", "模型 ID", "调用 ID", "Agent ID", "model", "default_model"),
    "test_model": ("试用的模型", "试用模型", "测试模型", "test_model"),
}


def spelling(value):
    return re.sub(r"[\s_-]+", "", value).casefold()


def protocol_name(value):
    aliases = {
        "": OPENAI, "openai": OPENAI, "openaicompatible": OPENAI, "chatcompletions": OPENAI,
        "responses": RESPONSES, "anthropic": ANTHROPIC, "messages": ANTHROPIC, "gemini": GEMINI,
    }
    aliases.update({spelling(p): p for p in PROTOCOLS})
    try:
        return aliases[spelling(value)]
    except KeyError:
        raise ValueError("请选择支持的接口协议。") from None


def base_url(value, protocol=OPENAI):
    value = value.strip().rstrip("/")
    try:
        u = urlsplit(value)
        port = u.port
        valid = u.hostname and port != 0 and u.username is None and u.password is None
    except ValueError:
        valid = False
    if not valid or re.search(r"[\s\\?#\x00-\x1f]", value):
        raise ValueError(ADDRESS_ERROR)
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in LOOPBACK_HOSTS):
        raise ValueError("远程接口需要 HTTPS，本机接口也可以使用 HTTP。")
    generate_path = GEMINI_GENERATE_PATH.search(u.path) if protocol == GEMINI else None
    suffix = {OPENAI: "/chat/completions", RESPONSES: "/responses", ANTHROPIC: "/messages", GEMINI: "/models"}[protocol]
    if protocol in (OPENAI, RESPONSES) and u.path.endswith("/images/generations"):
        suffix = "/images/generations"
    if generate_path:
        suffix = generate_path[0]
    if u.path.endswith(suffix):
        value = value[:-len(suffix)].rstrip("/")
    if not urlsplit(value).path:
        if protocol == GEMINI:
            return value + "/v1beta"
        if protocol == ANTHROPIC or u.hostname == "api.openai.com":
            return value + "/v1"
    return value


def api_endpoint(address, protocol=""):
    value = single_line(address)
    try:
        endpoint = urlsplit(value)
    except ValueError:
        raise ValueError(ADDRESS_ERROR) from None
    path = endpoint.path.rstrip("/")
    gemini = GEMINI_GENERATE_PATH.search(path)
    if not protocol or protocol in (AUTO_PROTOCOL, "auto"):
        if path.endswith("/chat/completions"):
            protocol = OPENAI
        elif path.endswith("/responses"):
            protocol = RESPONSES
        elif path.endswith("/messages") or endpoint.hostname == "api.anthropic.com":
            protocol = ANTHROPIC
        elif gemini or endpoint.hostname == "generativelanguage.googleapis.com":
            protocol = GEMINI
        else:
            protocol = OPENAI
    protocol = protocol_name(protocol)
    model = unquote(gemini[1]) if protocol == GEMINI and gemini else ""
    return protocol, base_url(value, protocol), model


def single_line(value):
    if not isinstance(value, str) or any(ord(ch) < 32 for ch in value.strip()):
        raise ValueError("配置中的名称、地址、密钥和模型均须使用单行文本。")
    return value.strip()


def api_key(value):
    value = single_line(value)
    if any(not 33 <= ord(ch) <= 126 for ch in value):
        raise ValueError("API 密钥中含有空格或无效字符，请重新复制。")
    return value


def validated_profile(fields, *, require_model=True):
    name = single_line(fields.get("name", ""))
    protocol, base, url_model = api_endpoint(fields.get("base", ""), single_line(fields.get("protocol", "")))
    model = single_line(fields.get("model", "")) or single_line(url_model)
    if require_model and not model:
        raise ValueError("请点「读取列表」选择调用目标，或输入服务提供的模型 / Agent ID。")
    profile = {"name": name, "protocol": protocol, "base": base, "model": model,
               "test_model": single_line(fields.get("test_model", ""))}
    if "kind" in fields:
        kind = single_line(fields["kind"]).casefold()
        kind = "model" if kind == "模型" else kind
        if kind not in KIND_LABELS:
            raise ValueError("请选择「模型」或「Agent」标记。")
        profile["kind"] = kind
    return profile


FIELD_NAMES = {spelling(label): field for field, labels in FIELD_ALIASES.items() for label in labels}
LABEL_PATTERN = "|".join(re.escape(label) for labels in FIELD_ALIASES.values()
                         for label in sorted(labels, key=len, reverse=True))
FIELD_LINE = re.compile(rf"^({LABEL_PATTERN})(?:\s*[:：=|]\s*|\s+)(.*)$", re.I)


def parse_profile(text):
    text = text.lstrip("\ufeff").strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1]).strip()
    if text.startswith(("{", "[")):
        try:
            values = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 第 {e.lineno} 行、第 {e.colno} 列格式有误。") from None
        if not isinstance(values, dict):
            raise ValueError("请一次导入一个 API 配置对象。")
        fields = {FIELD_NAMES[spelling(k)]: v for k, v in values.items() if spelling(k) in FIELD_NAMES}
    else:
        fields = {}
        text = text.replace(r"\:", ":").replace("\u3000", " ")
        text = re.sub(rf"\\\s*(?=(?:{LABEL_PATTERN})(?:\s|[:：=|]))", "\n", text, flags=re.I)
        for line in text.splitlines():
            line = line.strip().removesuffix("\\").strip().strip("|").strip()
            match = FIELD_LINE.fullmatch(line.removeprefix("- ").removeprefix("• "))
            if match:
                fields[FIELD_NAMES[spelling(match[1])]] = match[2].strip().strip("`").strip()
        for field in ("model", "test_model"):
            value = fields.get(field, "").partition("。")[0].strip()
            if field == "test_model" and value in {"可以留空", "可留空", "留空", "选填"}:
                value = ""
            fields[field] = re.sub(r"^(?:先填|请填|填写)\s+", "", value).strip("`")
    if not fields.get("base"):
        raise ValueError("配置需要包含接口地址。可导入 JSON 或每行一个字段的配置文本。")
    profile = validated_profile(fields, require_model=False)
    return profile, api_key(fields["key"]) if "key" in fields else None


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


def _windows_secret(data, *, encrypt):
    if not data:
        return b""
    if os.name != "nt":
        raise ValueError("此密钥由原 Windows 用户加密，请在当前电脑重新填写并保存。")
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(buffer))
    result = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_ptr = ctypes.POINTER(DataBlob)
    operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    description = wintypes.LPCWSTR if encrypt else ctypes.POINTER(wintypes.LPWSTR)
    operation.argtypes = [blob_ptr, description, blob_ptr, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_ptr]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    # Current Windows user, UI forbidden. Only the encrypted blob goes into SQLite.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        if encrypt:
            raise ValueError("Windows 未能加密 API 密钥，配置未保存。")
        raise ValueError("当前 Windows 用户无法读取此配置的密钥，请重新填写后保存。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def protect_key(value):
    raw = api_key(value).encode("utf-8")
    if not raw or os.name == "nt":
        return _windows_secret(raw, encrypt=True)
    return b"VAT-F1:" + _user_cipher(create=True).encrypt(raw)


def unprotect_key(value):
    if value.startswith(b"VAT-F1:"):
        if os.name == "nt":
            raise ValueError("此密钥来自其他系统，请在当前电脑重新填写并保存。")
        from cryptography.fernet import InvalidToken
        try:
            return _user_cipher(create=False).decrypt(value[7:]).decode("utf-8")
        except InvalidToken:
            raise ValueError("当前用户无法解密此 API 密钥，请重新填写并保存。") from None
    return _windows_secret(value, encrypt=False).decode("utf-8")


def _user_cipher(*, create):
    from cryptography.fernet import Fernet

    directory = config_dir()
    path = directory / "api-secret.key"
    if create and not path.exists():
        # Publish a complete 0600 file atomically without replacing another writer's key.
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("请确认 api-secret.key 归当前用户所有并将权限设置为 600。")
            return Fernet(f.read(45))
    except OSError:
        raise ValueError("无法读取本机的 API 加密文件，请检查配置目录或重新填写密钥。") from None
