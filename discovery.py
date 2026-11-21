import json
import os
import shutil
import subprocess
import tempfile
import task_control
from pathlib import Path

from app_paths import get_path

from store import count_metric


EGO_DOWNLOAD_URL = "https://lite.ego.app/"
EGO_SETUP_HINT = "请安装并打开 Ego Lite，在设置中选择 Ego 可执行文件，或将 ego-browser 加入 PATH，再重新检测。"


def ego_cli():
    custom = get_path("ego_cli")
    if custom:
        return custom if Path(custom).is_file() and os.access(custom, os.X_OK) else ""
    found = shutil.which("ego-browser")
    if found:
        return found
    name = "ego-browser.exe" if os.name == "nt" else "ego-browser"
    active = Path.home() / ".local/share/ego/active_version_dir" / name
    if active.is_file() and os.access(active, os.X_OK):
        return str(active)
    return ""


RESULT_PREFIX = b"VAT_RESULT "


def read_result(result_path, completed):
    # Ego on Windows can send normal console output to stderr. Keep console
    # transport as a fallback when the browser runtime cannot write the file.
    streams = (completed.stdout, completed.stderr)
    if completed.returncode == 0:
        candidates = []
        if result_path.is_file():
            candidates.append(result_path.read_bytes())
        for stream in streams:
            for line in reversed(stream.split(b"\n")):
                if line.startswith(RESULT_PREFIX):
                    candidates.append(line[len(RESULT_PREFIX):])
        for candidate in candidates:
            try:
                result = json.loads(candidate)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (isinstance(result, dict) and isinstance(result.get("status"), str)
                    and isinstance(result.get("rows"), list) and result.get("space") is not None):
                return result
        if candidates:
            raise ValueError("Ego 返回的数据不完整，请重新读取当前页。")

    diagnostics = (completed.stderr + b"\n" + completed.stdout).decode("utf-8", "replace")
    details = next((line.strip() for line in diagnostics.splitlines()
                    if not line.startswith("VAT_RESULT ") and ("Error:" in line or "EGO_" in line)), "")
    message = "未能读取 Ego 页面。"
    if details:
        message += "\n" + details[:260]
    elif completed.returncode:
        message += f"浏览器命令退出码 {completed.returncode}。"
    else:
        message += "浏览器命令已结束，但没有返回页面数据。"
    raise ValueError(message)


def browse(project_id, space, action, keyword="", row=None, detail_page=None):
    cli = ego_cli()
    if not cli:
        raise ValueError(EGO_SETUP_HINT)
    args = {"project": project_id, "space": space, "action": action, "keyword": keyword, "row": row, "detailPage":detail_page}
    template = Path(__file__).with_name("ego_douyin.mjs").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="vat-ego-") as temp:
        result_path = Path(temp) / "result.json"
        args["resultFile"] = str(result_path)
        mod = Path(temp) / "action.mjs"
        mod.write_text("const VAT_INPUT = "+json.dumps(args,ensure_ascii=True)+";\n"+template,encoding="utf-8")
        try:
            p = task_control.run_process([cli,"nodejs","-e","await import("+json.dumps(mod.as_uri())+")"],capture_output=True,
                               timeout=430 if action=="batch" else 115,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except FileNotFoundError:
            raise ValueError(EGO_SETUP_HINT) from None
        except subprocess.TimeoutExpired:
            raise ValueError("Ego 操作超时，请查看浏览器窗口，再读取当前页。") from None
        return read_result(result_path, p)


def page_count(value):
    try:
        return count_metric(value)
    except ValueError:
        return None
