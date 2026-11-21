"""Per-user storage locations and machine-specific tool selections."""

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


TOOL_PATHS = {"ffmpeg_dir": "dir", "sovits_dir": "dir", "sovits_python": "file",
              "ego_cli": "file", "font_file": "file"}


def _env_dir(name, fallback):
    value = os.getenv(name)
    if value and Path(value).is_absolute():
        return Path(value)
    return fallback


def config_dir():
    home = Path.home()
    if sys.platform == "win32":
        return _env_dir("LOCALAPPDATA", home / "AppData/Local") / "VAT Studio" / "config"
    if sys.platform == "darwin":
        return home / "Library/Application Support/VAT Studio/config"
    return _env_dir("XDG_CONFIG_HOME", home / ".config") / "vat-studio"


def default_data_home():
    home = Path.home()
    if sys.platform == "win32":
        # Preserve existing Windows installations and all their project references.
        return _env_dir("LOCALAPPDATA", home / "AppData/Local") / "分镜工作台"
    if sys.platform == "darwin":
        return home / "Library/Application Support/VAT Studio/workspace"
    return _env_dir("XDG_DATA_HOME", home / ".local/share") / "vat-studio"


def _read_selection(key):
    db = config_dir() / "locations.sqlite3"
    if not db.exists():
        return ""
    with closing(sqlite3.connect(db, timeout=8)) as conn:
        # The first writer may have created the file without committing its schema yet.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='locations'").fetchone():
            return ""
        row = conn.execute("SELECT value FROM locations WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def _save_selection(values):
    if not values:
        return
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(directory / "locations.sqlite3", timeout=8)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS locations(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT OR REPLACE INTO locations VALUES (?, ?)", values.items())


def _absolute(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("请输入绝对路径，或使用浏览按钮选择位置。")
    return path.resolve()


def _workspace_path(value):
    path = _absolute(value)
    if getattr(sys, "frozen", False):
        inside_app = path == Path(sys.executable).resolve().parent
    else:
        inside_app = path.is_relative_to(Path(__file__).resolve().parent)
    if inside_app:
        raise ValueError("请在软件目录以外选择工作区，以便独立备份项目和素材。")
    return path


def data_home():
    selected = os.getenv("FENJING_HOME") or _read_selection("workspace")
    return _workspace_path(selected) if selected else default_data_home()


def set_workspace(value):
    """Select a workspace on next launch. Never move or overwrite project files."""
    override = os.getenv("FENJING_HOME")
    path = _workspace_path(value) if value else default_data_home()
    if override:
        if _workspace_path(override) == path:
            return
        raise ValueError("当前工作区由 FENJING_HOME 指定，请更改或清除该环境变量再切换工作区。")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=path):
            pass
        db = path / "workspace.sqlite3"
        if db.exists():
            # Catch a damaged/unrelated DB before remembering it as the startup workspace.
            with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=8)) as conn:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if names and not names.intersection({"projects", "prefs"}):
                    raise ValueError("所选目录中的 workspace.sqlite3 不属于 VAT 项目。")
    except OSError as exc:
        raise ValueError(f"无法在所选工作区写入文件：{exc}") from None
    except sqlite3.Error as exc:
        raise ValueError(f"无法读取所选目录中的项目数据库：{exc}") from None
    _save_selection({"workspace": str(path) if value else ""})


def get_path(key):
    if key not in TOOL_PATHS:
        raise KeyError(key)
    return _read_selection(key)


def set_paths(values):
    selected = {}
    for key, value in values.items():
        kind = TOOL_PATHS[key]
        if not value:
            selected[key] = ""
            continue
        path = _absolute(value)
        if not (path.is_dir() if kind == "dir" else path.is_file()):
            raise ValueError(f"所选{'文件夹' if kind == 'dir' else '文件'}不存在：{path}")
        selected[key] = str(path)
    _save_selection(selected)
