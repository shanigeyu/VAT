import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from api_profiles import OPENAI, protect_key, unprotect_key, validated_profile
from app_paths import data_home, default_data_home, set_workspace


LIBRARIES = {"theme": "主题风格库", "effect": "动效库", "video": "视频素材库", "image": "图片素材库", "talk": "口播素材库"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEOS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
SHOT_SLOTS = {"main": "主体画面", "theme": "主题底图", "effect": "动效叠层"}
APP_NAME = "VAT（自动剪辑软件）"
DEFAULTS = {
    "engine": "", "codex_path": "", "codex_model": "", "api_profile": "",
    "harness": "", "harness_model": "", "style": "", "voice": "", "sovits_profile": "",
    "speech_backend": "local", "speech_api": {}, "speech_llm": {},
    "theme": "modern", "accent": "jade", "language": "zh_CN", "source_dir": "",
    "tutorial": "show",
}
SAVE_PROJECT = "INSERT INTO projects VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data"


def uid():
    return uuid.uuid4().hex


def library_owner(category, project_id):
    if category not in LIBRARIES:
        raise ValueError("请选择素材库。")
    return project_id if category == "talk" else ""


def library_extensions(category):
    if category == "image":
        return IMAGES
    if category in {"talk", "video"}:
        return VIDEOS
    return IMAGES | VIDEOS


def new_project(title):
    return upgrade_project({
        "id": uid(), "title": title, "topics": [], "topic_id": "", "draft": "",
        "approved": "", "shots": [], "orientation": "竖屏 9∶16",
        "sfx": "", "sfx_volume": 18, "exports": [],
    })


def upgrade_project(p):
    p.setdefault("previews", [])
    p.setdefault("covers", [])
    p.setdefault("target_seconds", None)
    p.setdefault("duration_advice", None)
    p.setdefault("sound", {"clip_audio": False, "background": "", "bg_gain": 15, "ai_gain": 100, "human_gain": 100})
    captions = p.setdefault("captions", {})
    captions.setdefault("mode", "烧录到成片")
    captions.setdefault("edits", {})
    if "sections" not in p:
        p["sections"] = []
        if p["shots"]:
            # Keep existing shots and their library links intact when opening an older project.
            p["sections"] = [{"title": "原有分镜", "end": p["shots"][-1]["end"]}]
    return p


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_home()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "workspace.sqlite3"
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            path_collation = "NOCASE" if os.name == "nt" else "BINARY"
            conn.executescript(f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    protocol TEXT NOT NULL, base TEXT NOT NULL, model TEXT NOT NULL,
                    test_model TEXT NOT NULL, secret BLOB NOT NULL DEFAULT X'',
                    kind TEXT NOT NULL DEFAULT 'model'
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, owner TEXT NOT NULL,
                    path TEXT NOT NULL COLLATE {path_collation}, name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, folder_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(category, owner, path)
                );
                CREATE TABLE IF NOT EXISTS asset_folders (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, owner TEXT NOT NULL,
                    parent_id TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
                    UNIQUE(category, owner, parent_id, name)
                );
            """)
            profile_columns = {r[1] for r in conn.execute("PRAGMA table_info(api_profiles)")}
            if "kind" not in profile_columns:
                conn.execute("ALTER TABLE api_profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'model'")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
            if "enabled" not in columns:
                conn.execute("ALTER TABLE assets ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
            if "folder_id" not in columns:
                conn.execute("ALTER TABLE assets ADD COLUMN folder_id TEXT NOT NULL DEFAULT ''")
            if conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                conn.execute("INSERT OR IGNORE INTO prefs VALUES('theme', ?)", (json.dumps("classic"),))
            else:
                conn.execute("INSERT OR IGNORE INTO prefs VALUES('theme', ?)", (json.dumps(DEFAULTS["theme"]),))
            if not conn.execute("SELECT 1 FROM api_profiles LIMIT 1").fetchone():
                legacy = {k: json.loads(v) for k, v in conn.execute(
                    "SELECT key, data FROM prefs WHERE key IN ('base','model')")}
                if legacy.get("base") and legacy.get("model"):
                    name = urlsplit(legacy["base"]).hostname or "原有 API"
                    profile_id = uid()
                    conn.execute("INSERT INTO api_profiles(id,name,protocol,base,model,test_model) VALUES(?,?,?,?,?,?)",
                                 (profile_id, name, OPENAI, legacy["base"], legacy["model"], ""))
                    self._write_prefs(conn, {"api_profile": profile_id})

    def query(self, sql, args=()):
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, args).fetchall()

    def prefs(self):
        prefs = {**DEFAULTS, **{r["key"]: json.loads(r["data"]) for r in self.query("SELECT * FROM prefs")}}
        rows = self.query("SELECT name AS api_name, kind AS api_kind, protocol, base, model, test_model FROM api_profiles WHERE id=?",
                          (prefs["api_profile"],)) if prefs["api_profile"] else []
        if rows:
            return {**prefs, **dict(rows[0])}
        if prefs["engine"] == "API":
            prefs["engine"] = ""
        return {**prefs, "api_profile": "", "api_name": "", "api_kind": "model", "protocol": OPENAI,
                "base": "", "model": "", "test_model": ""}

    def set_pref(self, key, value):
        self.set_prefs({key: value})

    def set_prefs(self, values):
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            self._write_prefs(conn, values)

    @staticmethod
    def _write_prefs(conn, values):
        rows = ((key, json.dumps(value, ensure_ascii=False)) for key, value in values.items())
        conn.executemany("INSERT OR REPLACE INTO prefs VALUES(?,?)", rows)

    def api_profiles(self):
        return [dict(row) for row in self.query(
            "SELECT id, name, kind, protocol, base, model, test_model FROM api_profiles ORDER BY rowid")]

    def profile_key(self, profile_id, base, protocol):
        rows = self.query("SELECT secret FROM api_profiles WHERE id=? AND base=? AND protocol=?",
                          (profile_id, base, protocol))
        if not rows:
            raise ValueError("API 配置已更改，请重新选择。")
        return unprotect_key(rows[0]["secret"])

    def save_api_profile(self, fields, key, profile_id=None, *, activate=True):
        profile = validated_profile(fields)
        values = {"kind": None, **profile, "id": profile_id or uid(), "secret": protect_key(key)}
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not values["name"]:
                name = model = values["model"]
                number = 2
                while conn.execute("SELECT 1 FROM api_profiles WHERE name=? AND id<>?", (name, values["id"])).fetchone():
                    name = f"{model} ({number})"
                    number += 1
                values["name"] = name
            try:
                if profile_id:
                    changed = conn.execute("""UPDATE api_profiles SET name=:name, protocol=:protocol, base=:base,
                        model=:model, test_model=:test_model, secret=:secret, kind=COALESCE(:kind, kind)
                        WHERE id=:id""", values).rowcount
                    if not changed:
                        raise ValueError("此 API 配置已不存在，请重新选择。")
                else:
                    conn.execute("""INSERT INTO api_profiles(id,name,protocol,base,model,test_model,secret,kind)
                        VALUES(:id,:name,:protocol,:base,:model,:test_model,:secret,COALESCE(:kind,'model'))""", values)
            except sqlite3.IntegrityError:
                raise ValueError("已有同名 API 配置，请换个名称，或在列表中选择原配置进行编辑。") from None
            if activate:
                self._write_prefs(conn, {"engine": "API", "api_profile": values["id"]})
        return values["id"]

    def projects(self):
        return [upgrade_project(json.loads(r["data"])) for r in self.query("SELECT data FROM projects ORDER BY rowid")]

    def save(self, project):
        self.query(SAVE_PROJECT, (project["id"], json.dumps(project, ensure_ascii=False)))

    def save_talk_import(self, project, path, clips):
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("口播视频已移动，请重新选择文件。")
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            asset_id, = conn.execute(
                """INSERT INTO assets(id,category,owner,path,name) VALUES(?,?,?,?,?)
                   ON CONFLICT(category,owner,path) DO UPDATE SET enabled=1 RETURNING id""",
                (uid(), "talk", project["id"], str(path), path.name),
            ).fetchone()
            first_unit = project["shots"][min(clips)]["start"]
            edits = project["captions"]["edits"]
            # Timing overrides are absolute. Keep text edits when earlier shot durations change.
            for key, edit in list(edits.items()):
                unit = key.split(":", 1)[0]
                if unit.isdecimal() and int(unit) >= first_unit:
                    edit.pop("start", None)
                    edit.pop("end", None)
                    if not edit:
                        del edits[key]
            for index, clip in clips.items():
                project["shots"][index].update(kind="talk", voice_source="camera", main=[dict(clip, id=asset_id)])
            conn.execute(SAVE_PROJECT, (project["id"], json.dumps(project, ensure_ascii=False)))

    def assets(self, project_id):
        rows = self.query("SELECT * FROM assets WHERE owner='' OR owner=? ORDER BY rowid DESC", (project_id,))
        return {r["id"]: dict(r) for r in rows}

    def asset_folders(self, category, project_id):
        owner = library_owner(category, project_id)
        rows = self.query(
            "SELECT id, parent_id, name FROM asset_folders WHERE category=? AND owner=? ORDER BY rowid",
            (category, owner),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _check_asset_folder(conn, category, owner, folder_id):
        if folder_id and conn.execute(
            "SELECT 1 FROM asset_folders WHERE id=? AND category=? AND owner=?",
            (folder_id, category, owner),
        ).fetchone() is None:
            raise ValueError("请选择当前素材库中的文件夹。")

    def create_asset_folder(self, category, project_id, name, parent_id=""):
        owner = library_owner(category, project_id)
        name = name.strip()
        if not name:
            raise ValueError("请输入文件夹名称。")
        folder_id = uid()
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            self._check_asset_folder(conn, category, owner, parent_id)
            added = conn.execute(
                """INSERT INTO asset_folders(id, category, owner, parent_id, name) VALUES(?,?,?,?,?)
                   ON CONFLICT(category, owner, parent_id, name) DO NOTHING""",
                (folder_id, category, owner, parent_id, name),
            ).rowcount
            if not added:
                raise ValueError("当前位置已有同名文件夹。")
        return folder_id

    def move_assets(self, category, project_id, asset_ids, folder_id):
        owner = library_owner(category, project_id)
        with closing(sqlite3.connect(self.db, timeout=8)) as conn, conn:
            self._check_asset_folder(conn, category, owner, folder_id)
            return conn.executemany(
                "UPDATE assets SET folder_id=? WHERE id=? AND category=? AND owner=? AND folder_id<>?",
                ((folder_id, aid, category, owner, folder_id) for aid in asset_ids),
            ).rowcount

    def add_asset(self, category, project_id, path):
        path = Path(path).resolve()
        if category not in LIBRARIES or not path.is_file():
            raise ValueError("请选择一个存在的素材文件。")
        if category == "image" and path.suffix.lower() not in IMAGES:
            raise ValueError("请选择支持的图片文件。")
        return self.query(
            "INSERT INTO assets(id,category,owner,path,name) VALUES(?,?,?,?,?) ON CONFLICT(category,owner,path) DO NOTHING RETURNING id",
            (uid(), category, project_id if category == "talk" else "", str(path), path.name),
        )

    def import_paths(self, category, project_id, paths, extensions=None, folder_id=""):
        owner = library_owner(category, project_id)
        if extensions is None:
            extensions = library_extensions(category)
        elif category == "image":
            extensions = extensions & IMAGES

        def walk_files():
            for path in map(Path, paths):
                if path.is_file():
                    yield path
                elif path.is_dir():
                    for folder, dirs, files in os.walk(path):
                        # os.walk skips symlinks. Older Python on Windows can still follow junctions.
                        if os.name == "nt":
                            dirs[:] = [d for d in dirs if not (
                                (Path(folder) / d).lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)]
                        yield from (Path(folder)/name for name in files)
        found = added = 0
        with closing(sqlite3.connect(self.db, timeout=12)) as conn, conn:
            self._check_asset_folder(conn, category, owner, folder_id)
            for path in walk_files():
                if path.suffix.lower() in extensions:
                    found += 1
                    added += conn.execute(
                        """INSERT INTO assets(id, category, owner, path, name, folder_id) VALUES(?,?,?,?,?,?)
                           ON CONFLICT(category, owner, path) DO NOTHING""",
                        (uid(), category, owner, str(path.resolve()), path.name, folder_id),
                    ).rowcount
        return added, found-added


def count_metric(text):
    s = str(text).strip().replace(",", "").replace("，", "").lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|亿|w|k)?", s)
    if not m:
        raise ValueError("请填写点赞量和粉丝数，例如 3500 或 1.2万。")
    scale = {"万": 10000, "w": 10000, "亿": 100000000, "k": 1000}.get(m[2], 1)
    n = Decimal(m[1]) * scale
    if n != int(n):
        raise ValueError("折算后的数量需要是整数。")
    return int(n)


def eligible(topic):
    return (topic["source"] == "抖音" and type(topic.get("likes")) is int
            and type(topic.get("followers")) is int and topic["likes"] > 10 * topic["followers"])


def script_units(text):
    # Keep the approved wording, including punctuation, when grouping sentences into shots.
    return [p.strip() for p in re.findall(r"[^。！？!?\n]+[。！？!?]*[”’」』]*|[。！？!?]+", text) if p.strip()]


def confirmed(p):
    return bool(p["approved"]) and p["draft"].strip() == p["approved"]


def shot_clips(shot, slot):
    clips = shot.get(slot)
    if isinstance(clips, str) and clips:
        legacy = {"id": clips}
        if slot == "main":
            legacy.update(offset=shot.get("offset", 0), length=shot.get("length", 0))
        return [legacy]
    return clips or []


def shots_from_plan(units, plan):
    if not isinstance(plan, list) or not plan:
        raise ValueError("分镜结果为空，请重试或使用按句拆镜。")
    shots, first = [], 0
    for row in plan:
        if not isinstance(row, dict):
            raise ValueError("分镜数据格式有误。")
        end, visual, kind = row.get("end"), row.get("visual"), row.get("kind")
        if type(end) is not int or not first < end <= len(units) or kind not in ("voice", "talk") or not isinstance(visual, str) or not visual.strip():
            raise ValueError("模型返回的镜头范围或内容有误，请重试或使用按句拆镜。")
        shots.append({"start": first, "end": end, "kind": kind, "visual": visual.strip(),
                      **{slot: [] for slot in SHOT_SLOTS}})
        first = end
    if first != len(units):
        raise ValueError("分镜没有覆盖全部定稿，请重试或使用按句拆镜。")
    return shots
