import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import task_control


def cli_path(saved=""):
    if saved.strip():
        candidate = Path(saved.strip().strip('"')).expanduser()
        return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else ""
    candidate = os.getenv("CODEX_APP_RUNTIME_CLI_PATH")
    if candidate and Path(candidate).is_file():
        return candidate
    candidate = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if candidate or os.name != "nt":
        return candidate or ""
    # Explorer-launched VAT does not inherit the desktop app's CLI path.
    import winreg
    packages = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, packages) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                name = winreg.EnumKey(key, i)
                if name.startswith("OpenAI.Codex_"):
                    with winreg.OpenKey(key, name) as package:
                        folder = Path(winreg.QueryValueEx(package, "PackageRootFolder")[0])
                        exe = folder / "app/resources/codex.exe"
                        if exe.is_file():
                            return str(exe)
    except OSError:
        pass
    return ""


def invoke(path, args, cwd, prompt=None, timeout=300):
    if not path:
        raise ValueError("找不到 Codex CLI，请在设置中选择本机 Codex 可执行文件。")
    if os.name == "nt" and Path(path).suffix.lower() != ".exe":
        raise ValueError("请选择 Codex CLI 的 .exe 可执行文件。")
    try:
        result = task_control.run_process([path, *args], cwd=cwd, input=(prompt or "").encode("utf-8"),
                                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError(f"Codex 在 {timeout} 秒内未完成，请检查连接后重试。") from None
    except OSError as exc:
        raise ValueError(f"无法启动 Codex CLI，请检查命令路径。{exc}") from None
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").lower()
        if any(x in diagnostic for x in ("401", "unauthorized", "login", "authentication")):
            hint = "请先在当前用户账户下登录 Codex。"
        elif any(x in diagnostic for x in ("429", "rate limit", "usage limit", "quota")):
            hint = "请检查 Codex 账户用量或稍后重试。"
        elif "model" in diagnostic and any(x in diagnostic for x in ("not supported", "not found", "not available")):
            hint = "请读取模型列表并选择当前账户可用的模型。"
        else:
            hint = "请检查 Codex CLI 的网络连接、模型和本地配置。"
        raise ValueError(f"Codex 退出码 {result.returncode}。{hint}")
    return result.stdout.decode("utf-8-sig", "replace")


def models(path):
    with tempfile.TemporaryDirectory(prefix="vat-codex-") as wd:
        raw = invoke(path, ["debug", "models"], wd, timeout=30)
    try:
        catalog = json.loads(raw)["models"]
        available = tuple(dict.fromkeys(m["slug"] for m in catalog if m.get("visibility") == "list" and m.get("slug")))
    except (KeyError, AttributeError, TypeError, ValueError):
        available = ()
    if not available:
        raise ValueError("Codex 未返回模型目录，可选择默认模型或手填模型名称。")
    return available


def generate(path, prompt, model="", *, reasoning_effort=""):
    with tempfile.TemporaryDirectory(prefix="vat-codex-") as wd:
        reply = Path(wd) / "reply.txt"
        args = ["-a", "never", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--disable", "plugins", "--disable", "shell_tool", "--color", "never", "-o", str(reply)]
        if reasoning_effort:
            args[:0] = ["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)]
        if model:
            args.extend(("--model", model))
        invoke(path, [*args, "-"], wd, prompt)
        answer = reply.read_text(encoding="utf-8-sig").strip() if reply.is_file() else ""
        if not answer:
            raise ValueError("Codex 没有返回正文。")
        return answer


def generate_image(path, prompt, model="", *, reasoning_effort=""):
    """Use Codex's native image tool and collect its fresh local image artifact."""
    with tempfile.TemporaryDirectory(prefix="vat-codex-image-") as wd:
        reply = Path(wd) / "reply.json"
        schema = Path(wd) / "reply-schema.json"
        schema.write_text(json.dumps({
            "type": "object", "properties": {"image_path": {"type": ["string", "null"]}},
            "required": ["image_path"], "additionalProperties": False,
        }), encoding="utf-8")
        args = ["-a", "never", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write",
                "--disable", "plugins", "--disable", "shell_tool", "--enable", "image_generation",
                "--color", "never", "--output-schema", str(schema), "-o", str(reply)]
        if reasoning_effort:
            args[:0] = ["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)]
        if model:
            args.extend(("--model", model))
        instruction = (
            "使用 Codex 原生图片生成工具，按下面的绘制要求生成一张图片。"
            "完成后返回 JSON，image_path 填本次生成结果中实际保存的图片路径。"
            "不要用代码、SVG 或 HTML 替代图片，不要调用其他生图服务。"
            "无需复制、移动或重命名原文件，VAT 会保存生成结果。"
            "如果原生生图工具不可用或没有生成图片，将 image_path 设为 null。\n\n绘制要求：\n" + prompt
        )
        started = time.time()
        invoke(path, [*args, "-"], wd, instruction, timeout=600)
        task_control.checkpoint()
        try:
            result = json.loads(reply.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            result = None
        source = result.get("image_path") if isinstance(result, dict) else None
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Codex 未返回生成的图片。请使用支持原生生图的 Codex 和当前账户可用的模型，或改选生图 API。")
        source = Path(source).expanduser()
        source = (source if source.is_absolute() else Path(wd) / source).resolve()
        codex_home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").expanduser()
        roots = (Path(wd).resolve(), (codex_home / "generated_images").resolve())
        if not any(source.is_relative_to(root) for root in roots):
            raise ValueError("Codex 返回的图片不在本次输出目录或 Codex 生成图片目录中，请重新生成。")
        try:
            with source.open("rb") as image:
                if os.fstat(image.fileno()).st_mtime < started - 2:
                    raise ValueError("Codex 返回了此前已有的图片，请重新生成本次画面。")
                data = image.read(32 * 1024 * 1024 + 1)
        except OSError:
            raise ValueError("找不到 Codex 本次生成的图片，请重试或检查 Codex 生图能力。") from None
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("Codex 生成的图片超过 32 MB。")
        if not data:
            raise ValueError("Codex 返回了空图片，请重试。")
        return data
