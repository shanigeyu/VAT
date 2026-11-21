import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import covers
import production
from shot_visuals import font
from store import Store, new_project, upgrade_project


class CoverTests(unittest.TestCase):
    def setUp(self):
        try:
            font(20)
        except ValueError as error:
            self.skipTest(str(error))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.store = Store(self.root)
        self.project = new_project("封面测试")

    def test_plate_cover_matches_each_requested_size(self):
        for name, expected in (("竖屏 9∶16", (1080, 1920)), ("竖屏 3∶4", (1080, 1440)),
                               ("横屏 16∶9", (1920, 1080))):
            with self.subTest(size=name):
                image = covers.compose("", name, "这条视频我一帧都没剪", "自动剪辑软件", "EP03", "黑金冲击")
                self.assertEqual((image.width, image.height), expected)
                self.assertEqual(image.mode, "RGB")

    def test_photo_background_fills_the_frame_and_darkens_under_the_title(self):
        source = self.root / "背景.png"
        Image.new("RGB", (320, 120), (210, 60, 40)).save(source)
        image = covers.compose(source, "竖屏 9∶16", "自动剪辑", "", "", "简约大字")
        self.assertEqual(image.size, (1080, 1920))
        top = image.getpixel((30, 30))
        bottom = image.getpixel((30, 1890))
        self.assertGreater(top[0], 150)
        self.assertLess(top[1], 110)
        self.assertLess(bottom[0], 60)

    def test_blank_title_is_rejected_and_long_titles_still_fit(self):
        with self.assertRaisesRegex(ValueError, "标题"):
            covers.compose("", "竖屏 9∶16", "   ")
        image = covers.compose("", "竖屏 9∶16", "用" * 60, "副标题" * 12, "角标" * 5, "暗黑科技")
        self.assertEqual(image.size, (1080, 1920))

    def test_save_writes_png_sidecar_and_record(self):
        image = covers.compose("", "竖屏 3∶4", "标题", "副标题", "EP03", "暖色纪实")
        fields = {"size": "竖屏 3∶4", "style": "暖色纪实", "title": "标题", "subtitle": "副标题",
                  "label": "EP03", "background": "", "source": "纯色"}
        record = covers.save(self.root, self.project, image, fields)
        path = covers.file_path(self.root, self.project["id"], record["file"])
        self.assertTrue(path.is_file())
        with Image.open(path) as saved:
            self.assertEqual(saved.size, (1080, 1440))
            self.assertEqual(saved.mode, "RGB")
        sidecar = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar, record)
        self.assertEqual((record["width"], record["height"]), (1080, 1440))
        self.assertEqual(record["source"], "纯色")

    def test_cover_references_cannot_escape_the_project(self):
        for name in ("../escape.png", "cover-2026.png", "cover-20260101-000000-zzzz.png", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                covers.file_path(self.root, self.project["id"], name)
        with self.assertRaises(ValueError):
            covers.project_dir(self.root, "../escape")

    def test_background_prompts_name_the_title_and_forbid_text(self):
        prompt = covers.background_prompt("自动剪辑", "EP03", "暗黑科技")
        self.assertIn("自动剪辑", prompt)
        self.assertIn("EP03", prompt)
        self.assertIn("不要出现任何文字", prompt)
        self.assertEqual(prompt, covers.background_prompt("自动剪辑", "EP03", "暗黑科技"))

    def test_background_prompts_offer_three_distinct_compositions(self):
        prompts = covers.background_prompts("自动剪辑", "EP03", "暗黑科技")
        self.assertEqual(len(prompts), 3)
        self.assertEqual(len(set(prompts)), 3)
        self.assertTrue(all(prompt.startswith(covers.background_prompt("自动剪辑", "EP03", "暗黑科技"))
                            for prompt in prompts))

    def test_ai_backgrounds_draw_the_cover_aspect_for_every_candidate(self):
        sources = [self.root / f"drawn-{index}.png" for index in range(3)]
        for index, path in enumerate(sources):
            Image.new("RGB", (64, 96), (10, 20, 30 + index)).save(path)
        settings = {"engine": "Codex", "codex_path": "", "codex_model": ""}
        with patch.object(covers.generated_images, "create", side_effect=["a.png", "b.png", "c.png"]) as create, \
                patch.object(covers.generated_images, "file_path", side_effect=sources):
            paths = covers.ai_backgrounds(self.root, {**self.project, "orientation": "横屏 16∶9"},
                                          settings, "", "标题", "EP03", "黑金冲击", "竖屏 3∶4")
        self.assertEqual(paths, sources)
        self.assertEqual(create.call_count, 3)
        prompts = [call.args[2] for call in create.call_args_list]
        self.assertEqual(len(set(prompts)), 3)
        self.assertTrue(all("标题" in prompt and "EP03" in prompt for prompt in prompts))
        self.assertTrue(all(call.args[1]["orientation"] == "竖屏 3∶4" for call in create.call_args_list))

    def test_ai_backgrounds_keep_the_candidates_that_finished(self):
        done = self.root / "ok.png"
        Image.new("RGB", (32, 32), (0, 0, 0)).save(done)
        messages = []
        with patch.object(covers.generated_images, "create",
                          side_effect=[ValueError("第一张失败"), "ok.png", "ok.png"]), \
                patch.object(covers.generated_images, "file_path", return_value=done):
            paths = covers.ai_backgrounds(self.root, self.project, {"engine": "Codex"}, "",
                                          "标题", "", "黑金冲击", "竖屏 3∶4", report=messages.append)
        self.assertEqual(paths, [done, done])
        self.assertTrue(any("没画出来" in message for message in messages))
        with patch.object(covers.generated_images, "create", side_effect=ValueError("模型没有返回图片")):
            with self.assertRaisesRegex(ValueError, "模型没有返回图片"):
                covers.ai_backgrounds(self.root, self.project, {"engine": "Codex"}, "",
                                      "标题", "", "黑金冲击", "竖屏 3∶4")

    def test_newest_export_is_the_default_frame_source(self):
        for name in ("ffmpeg", "ffprobe"):
            try:
                production.tool(name)
            except ValueError as error:
                self.skipTest(str(error))
        video = self.root / "成片.mp4"
        production.ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=1", video)
        preview = self.root / "样片.mp4"
        production.ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=1", preview)
        sample = self.root / "样片" / self.project["id"] / "20260101-000000-000000"
        sample.mkdir(parents=True)
        (sample / "preview.mp4").write_bytes(preview.read_bytes())
        self.project["exports"] = [{"kind": "完整成片", "path": str(video)},
                                   {"kind": "字幕", "path": str(self.root / "字幕.srt")}]
        self.project["previews"] = [{"version": "20260101-000000-000000"}]
        candidates = covers.video_candidates(self.root, self.project)
        self.assertEqual([item["kind"] for item in candidates], ["成片", "样片"])
        name = covers.frame_background(self.root, self.project, candidates[0]["path"], 0.4, "竖屏 9∶16")
        path = covers.background_path(self.root, self.project["id"], name)
        with Image.open(path) as frame:
            red, green, blue = frame.convert("RGB").getpixel((5, 5))
            self.assertGreater(blue, 150)
            self.assertLess(red, 110)

    def test_upgrade_adds_cover_history_to_older_projects(self):
        older = {"id": "abc", "title": "旧项目", "topics": [], "topic_id": "", "draft": "", "approved": "",
                 "shots": [], "orientation": "竖屏 9∶16", "sfx": "", "sfx_volume": 18, "exports": []}
        self.assertEqual(upgrade_project(older)["covers"], [])
        self.assertEqual(new_project("新项目")["covers"], [])


if __name__ == "__main__":
    unittest.main()
