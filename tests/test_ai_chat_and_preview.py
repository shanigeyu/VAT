import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import ai_chat
import covers
import shot_chat
import shot_preview
from store import new_project, shots_from_plan

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
       '<circle cx="50" cy="50" r="30" fill="#2bb673"/></svg>')


def project_with_shots():
    p = new_project("v0.3.0 测试")
    units = ["第一句。", "第二句。", "第三句。"]
    p.update(draft="".join(units), approved="".join(units), orientation="竖屏 9∶16",
             shots=shots_from_plan(units, [{"end": i + 1, "kind": "voice", "visual": f"画面 {i + 1}"}
                                           for i in range(3)]))
    return p


class ChatTests(unittest.TestCase):
    def test_parse_reads_json_and_falls_back_to_plain_text(self):
        reply, changes = ai_chat.parse('```json\n{"reply":"好","changes":{"draft":"新稿"}}\n```')
        self.assertEqual((reply, changes), ("好", {"draft": "新稿"}))
        self.assertEqual(ai_chat.parse("只是聊聊，没有 JSON"), ("只是聊聊，没有 JSON", {}))

    def test_normalize_keeps_only_page_changes_and_flags_bad_items(self):
        p = project_with_shots()
        clean, problems = ai_chat.normalize("shots", {"shots": [
            {"shot": 2, "visual": " 数据对比 ", "svg": SVG},
            {"shot": 9, "visual": "越界"},
            {"shot": 3, "svg": "<svg"},
        ]}, p)
        self.assertEqual(clean, {"shots": [{"shot": 2, "visual": "数据对比", "svg": SVG}]})
        self.assertEqual(len(problems), 2)
        clean, problems = ai_chat.normalize("cover", {"title": "新标题", "style": "不存在", "label": ""}, p,
                                            styles=covers.STYLE_NAMES)
        self.assertEqual(clean, {"title": "新标题", "label": ""})
        self.assertTrue(problems)
        clean, _ = ai_chat.normalize("audio", {"voice_direction": "  平静\n坚定 "}, p)
        self.assertEqual(clean, {"voice_direction": "平静 坚定"})

    def test_ask_sends_page_context_history_and_message(self):
        sent = {}

        def fake(prefs, key, brief, data):
            sent.update(brief=brief, data=data)
            return json.dumps({"reply": "已改", "changes": {"voice_direction": "温柔"}}, ensure_ascii=False)

        with patch("providers.generate", side_effect=fake):
            reply, changes, problems = ai_chat.ask({"engine": "API"}, "", "audio", {"当前情感与语气": ""},
                                                   [{"role": "user", "text": "上一句"}], "温柔一点")
        self.assertEqual((reply, changes, problems), ("已改", {"voice_direction": "温柔"}, []))
        self.assertIn("voice_direction", sent["brief"])
        self.assertEqual(sent["data"]["用户消息"], "温柔一点")
        self.assertEqual(sent["data"]["对话记录"][0]["内容"], "上一句")

    def test_history_is_capped_per_page(self):
        p = project_with_shots()
        for n in range(ai_chat.HISTORY_LIMIT + 5):
            ai_chat.remember(p, "script", "user", str(n))
        rows = ai_chat.history(p, "script")
        self.assertEqual(len(rows), ai_chat.HISTORY_LIMIT)
        self.assertEqual(rows[-1]["text"], str(ai_chat.HISTORY_LIMIT + 4))
        self.assertEqual(ai_chat.history(p, "audio"), [])

    def test_shot_changes_apply_visual_svg_and_queue_images(self):
        p = project_with_shots()
        p["shots"][2]["main"] = [{"id": "asset"}]
        pending, skipped = shot_chat.apply_chat(p, {"shots": [
            {"shot": 1, "visual": "新的画面", "image_prompt": "城市夜景"},
            {"shot": 2, "svg": SVG},
            {"shot": 3, "image_prompt": "不会生效", "svg": SVG},
        ]})
        self.assertEqual((pending, skipped), ([0], [3]))
        self.assertEqual(p["shots"][0]["visual"], "新的画面")
        self.assertEqual(p["shots"][0]["art"], {"image_prompt": "城市夜景"})
        self.assertEqual(p["shots"][1]["art"], {"svg": SVG, "layer": "main"})
        self.assertEqual(p["shots"][2]["art"], {"svg": SVG, "layer": "effect"})


class ShotPreviewTests(unittest.TestCase):
    def test_placeholder_and_svg_stills_match_the_canvas(self):
        p = project_with_shots()
        p["shots"][1]["art"] = {"svg": SVG, "layer": "main"}
        size = shot_preview.preview_size(p["orientation"])
        empty = shot_preview.shot_still(".", p, {}, 0)
        drawn = shot_preview.shot_still(".", p, {}, 1)
        self.assertEqual(empty.size, size)
        self.assertEqual(drawn.size, size)
        self.assertEqual(drawn.getpixel((size[0] // 2, size[1] // 2)), (43, 182, 115))
        self.assertEqual(shot_preview.describe(p, {}, p["shots"][1]), "原创动效")

    def test_main_image_respects_its_box_and_signature_tracks_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "red.png"
            Image.new("RGB", (40, 40), (220, 20, 20)).save(picture)
            p = project_with_shots()
            assets = {"a": {"path": str(picture), "enabled": 1, "name": "red.png"}}
            p["shots"][0]["main"] = [{"id": "a", "rect": [0, 0, 0.5, 0.5]}]
            before = shot_preview.signature(p, assets, 0)
            still = shot_preview.shot_still(folder, p, assets, 0)
            w, h = still.size
            self.assertEqual(still.getpixel((w // 4, h // 4)), (220, 20, 20))
            self.assertNotEqual(still.getpixel((3 * w // 4, 3 * h // 4)), (220, 20, 20))
            p["shots"][0]["visual"] = "改过"
            self.assertNotEqual(before, shot_preview.signature(p, assets, 0))

    def test_missing_files_fall_back_to_placeholder(self):
        p = project_with_shots()
        assets = {"a": {"path": "C:/no/such/file.png", "enabled": 1, "name": "x"}}
        p["shots"][0]["main"] = [{"id": "a"}]
        p["shots"][0]["art"] = {"svg": "<svg broken"}
        self.assertEqual(shot_preview.shot_still(".", p, assets, 0).size, shot_preview.preview_size(p["orientation"]))


class CoverDirectionTests(unittest.TestCase):
    def test_direction_joins_every_background_prompt(self):
        prompts = covers.background_prompts("标题", "EP03", covers.STYLE_NAMES[0], "  雨夜\n霓虹 ")
        self.assertTrue(all("补充画面要求：雨夜 霓虹。" in prompt for prompt in prompts))
        self.assertTrue(all("补充画面要求" not in prompt for prompt in covers.background_prompts("标题")))


if __name__ == "__main__":
    unittest.main()
