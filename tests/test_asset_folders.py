import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import planning
from store import Store, new_project, shots_from_plan


class AssetFolderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_shared_image_folder_can_supply_voice_shot_without_mixing_video(self):
        store = Store(self.root)
        image = self.root / "用户增长曲线.PNG"
        movie = self.root / "无关素材.mp4"
        for path in (image, movie):
            path.touch()
        folder = store.create_asset_folder("image", "p1", "数据图")
        self.assertEqual(store.import_paths("image", "p1", [self.root], folder_id=folder), (1, 0))
        self.assertEqual(store.import_paths("video", "p1", [self.root]), (1, 0))
        assets = store.assets("p2")
        aid, = [aid for aid, a in assets.items() if a["folder_id"] == folder]
        self.assertEqual(assets[aid]["path"], str(image))
        self.assertIn("image", planning.slot_categories({"kind": "voice"}, "main"))
        self.assertNotIn("image", planning.slot_categories({"kind": "talk"}, "main"))
        p = new_project("跨期复用")
        p["approved"] = "用户增长曲线。"
        p["shots"] = shots_from_plan([p["approved"]], [{"end": 1, "kind": "voice", "visual": "增长曲线"}])
        self.assertEqual(planning.auto_library_subjects(p, assets)["shots"][0]["main"], [{"id": aid}])

    def test_legacy_asset_keeps_its_identity_when_moved(self):
        original = self.root / "旧素材.png"
        original.touch()
        with closing(sqlite3.connect(self.root / "workspace.sqlite3")) as conn, conn:
            conn.execute("""CREATE TABLE assets (
                id TEXT PRIMARY KEY, category TEXT NOT NULL, owner TEXT NOT NULL,
                path TEXT NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, UNIQUE(category, owner, path)
            )""")
            conn.execute("INSERT INTO assets VALUES (?, 'theme', '', ?, ?, 0)",
                         ("legacy", str(original), original.name))

        store = Store(self.root)
        project = new_project("旧项目")
        project["shots"] = shots_from_plan(["第一句。"], [{"end": 1, "visual": "底图", "kind": "voice"}])
        project["shots"][0]["theme"] = "legacy"
        store.save(project)
        self.assertEqual(store.assets(project["id"])["legacy"]["folder_id"], "")
        folder = store.create_asset_folder("theme", project["id"], "背景")
        self.assertEqual(store.move_assets("theme", project["id"], ["legacy"], folder), 1)

        reopened = Store(self.root)
        self.assertEqual(reopened.projects()[0]["shots"][0]["theme"], "legacy")
        asset = reopened.assets(project["id"])["legacy"]
        self.assertEqual((asset["path"], asset["enabled"], asset["folder_id"]), (str(original), 0, folder))
        self.assertTrue(original.is_file())
        self.assertEqual(reopened.move_assets("theme", project["id"], ["legacy"], ""), 1)
        self.assertEqual(reopened.assets(project["id"])["legacy"]["folder_id"], "")

    def test_nested_folders_respect_library_and_project_scope(self):
        store = Store(self.root)
        parent = store.create_asset_folder("talk", "p1", "Workshop")
        nested = store.create_asset_folder("talk", "p1", "片头", parent)
        clip = self.root / "口播.mp4"
        clip.touch()
        aid = store.add_asset("talk", "p1", clip)[0]["id"]
        self.assertEqual(store.asset_folders("talk", "p1")[1], {"id": nested, "parent_id": parent, "name": "片头"})
        self.assertEqual(store.asset_folders("talk", "p2"), [])
        self.assertEqual(store.asset_folders("theme", "p1"), [])
        with self.assertRaisesRegex(ValueError, "同名"):
            store.create_asset_folder("talk", "p1", " workshop ")
        with self.assertRaisesRegex(ValueError, "当前素材库"):
            store.create_asset_folder("talk", "p2", "片头", parent)
        with self.assertRaisesRegex(ValueError, "当前素材库"):
            store.move_assets("video", "p1", [aid], nested)
        self.assertEqual(store.move_assets("talk", "p2", [aid], ""), 0)
        self.assertEqual(store.move_assets("talk", "p1", [aid], nested), 1)
        self.assertEqual(store.assets("p1")[aid]["folder_id"], nested)

    def test_recursive_import_targets_folder_and_preserves_duplicates(self):
        store = Store(self.root)
        source = self.root / "资源目录"
        nested = source / "子文件夹"
        nested.mkdir(parents=True)
        first, second = source / "图片.png", nested / "视频.MP4"
        for path in (first, second, nested / "文案.txt"):
            path.touch()
        aid = store.add_asset("video", "p1", first)[0]["id"]
        folder = store.create_asset_folder("video", "p1", "录屏")

        self.assertEqual(store.import_paths("video", "p1", [source], {".png", ".mp4"}, folder), (1, 1))
        files = {asset["name"]: asset for asset in store.assets("p2").values()}
        self.assertEqual(files[first.name]["folder_id"], "")
        self.assertEqual(files[second.name]["folder_id"], folder)
        self.assertEqual(store.asset_folders("video", "p2")[0]["id"], folder)
        self.assertEqual(store.move_assets("video", "p2", [aid, files[second.name]["id"]], folder), 1)
        self.assertTrue(all(asset["folder_id"] == folder for asset in store.assets("p2").values()))
