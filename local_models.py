"""Register a local single-file GGUF with the user's Ollama service."""
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import task_control
import tempfile
from pathlib import Path


HOST = "http://127.0.0.1:11434"


def registered_size(tag):
    """Ollama's reported model size, including potentially shared weights."""
    conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2)
    try:
        conn.request("GET", "/api/tags")
        with conn.getresponse() as response:
            if response.status != 200:
                return None
            entries = json.loads(response.read(4 * 1024 * 1024))["models"]
        for entry in entries:
            if entry["name"] in (tag, tag.removesuffix(":latest")):
                size = entry.get("size")
                return size if type(size) is int and size >= 0 else None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    return None


def _run(cli, args, *, cwd=None, timeout=25):
    env = {**os.environ, "OLLAMA_HOST": HOST, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    try:
        result = task_control.run_process([cli, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ValueError("Ollama 操作超时。请检查本机服务和磁盘空间后重试。") from None
    except OSError as exc:
        raise ValueError(f"无法启动 Ollama CLI。请检查可执行文件路径和权限。{exc}") from None
    if result.returncode:
        detail = result.stderr or result.stdout
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail.decode("utf-8", "replace"))
        raise ValueError(f"Ollama {args[0]} 失败。请确认已启动 {HOST} 上的本机服务，并检查模型与磁盘空间。\n{text.strip()[-450:]}")
    return result.stdout.decode("utf-8", "replace")


def import_gguf(model_path, cli_path="", report=lambda _: None):
    """Return the exact local model tag to put into the OpenAI-compatible API profile."""
    model = Path(model_path).expanduser()
    if not model.is_absolute() or not model.is_file() or model.suffix.lower() != ".gguf":
        raise ValueError("请选择本机已存在的 GGUF 模型文件。")
    if re.search(r"-\d{5}-of-\d{5}\.gguf$", model.name, re.I):
        raise ValueError("请选择单文件 GGUF。分片模型可先在 Ollama 中按分片顺序导入，再读取模型列表。")
    custom = Path(cli_path).expanduser() if cli_path else None
    if custom and (not custom.is_absolute() or not custom.is_file() or not os.access(custom, os.X_OK)):
        raise ValueError("请选择可执行的本机 Ollama CLI 文件。")
    cli = str(custom) if custom else shutil.which("ollama.exe" if os.name == "nt" else "ollama")
    if not cli or (os.name == "nt" and Path(cli).suffix.lower() != ".exe"):
        raise ValueError("找不到 Ollama CLI。请安装 Ollama，并选择其可执行文件或将其加入 PATH。")
    names = (row[0] for line in _run(cli, ["list"]).splitlines() if (row := line.split()))
    existing = {name if ":" in name else name + ":latest" for name in names}
    report("正在校验 GGUF 文件内容。大型模型可能需要几分钟。")
    with model.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError("文件头无效，请选择完整的 GGUF 模型。")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    # An existing tag may be owned by the user. Compare its blob before reuse,
    # and pick a different free tag if the name already points to other weights.
    base = "vat-" + digest[:24]
    tag, sequence = base + ":latest", 1
    while tag in existing:
        blueprint = _run(cli, ["show", "--modelfile", tag])
        if re.search(r"^FROM\s+.*sha256[-:]" + digest + r'["\s]*$', blueprint, re.I | re.M):
            return tag
        sequence += 1
        tag = f"{base}-{sequence}:latest"
    with tempfile.TemporaryDirectory(prefix="vat-ollama-") as work:
        folder = Path(work)
        # A fixed local alias keeps quotes, newlines and glob characters in user
        # filenames out of Modelfile syntax, and avoids copying multi-GB weights.
        alias = folder / "model.gguf"
        try:
            os.link(model, alias)
        except OSError:
            try:
                alias.symlink_to(model)
            except OSError:
                report("正在临时复制权重以导入 Ollama，完成后会清理临时副本。")
                shutil.copyfile(model, alias)
        (folder / "Modelfile").write_text("FROM ./model.gguf\n", encoding="utf-8")
        report("正在将模型注册到本机 Ollama。")
        _run(cli, ["create", tag, "-f", "Modelfile"], cwd=folder, timeout=3600)
    return tag
