import tempfile
import unittest
from unittest.mock import patch

import providers
import script_timing as timing
from store import Store, new_project


class ScriptTimingTests(unittest.TestCase):
    def test_fast_narration_with_mixed_chinese_and_english(self):
        sentence = "打开时间线，选择镜头，再导入配音。"
        draft = sentence * 14 + "最后导出 MP4，播放样片并检查成片。"
        topic = {"title": "整理素材"}
        self.assertEqual(timing.estimated_speech(draft), 40)
        self.assertEqual(timing.recommend({}, topic)["seconds"], 40)
        self.assertEqual(timing.estimated_speech("好"), 1)

    def test_ai_assessment_receives_the_calibrated_topic_baseline(self):
        topic = {"title":"用 Codex 自动剪辑 · 工具准备"}
        with patch("providers.generate", return_value='{"seconds":42,"reason":"留两秒展示界面。"}') as generate:
            advice = providers.duration_advice({}, "", topic)
            data = generate.call_args.args[-1]
            self.assertEqual(data["题材基准秒数"], 40)
            self.assertEqual(data["基准中文口播字数"], "200–232")
        self.assertEqual(timing.recommend({"duration_advice":advice}, topic)["seconds"], 42)

    def test_content_changes_advice_and_old_model_reason_expires(self):
        topic = {"id":"sample", "title":"新功能上线", "notes":"支持本地导出"}
        project = new_project("样片")
        self.assertEqual(timing.recommend(project, topic)["seconds"], 30)
        project["duration_advice"] = {"topic_key":timing.topic_key(topic),"seconds":55,"reason":"展示导出过程。"}
        self.assertEqual(timing.target(project, timing.recommend(project, topic)), 55)
        topic["title"] = "从零导出视频的实操教程"
        self.assertEqual(timing.recommend(project, topic)["seconds"], 100)

    def test_manual_choice_survives_reopen_and_has_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            project = new_project("样片")
            project["target_seconds"] = 73
            store.save(project)
            self.assertEqual(timing.target(Store(temp).projects()[0], {"seconds":45}), 73)

    def test_requested_duration_reaches_rewrite_and_existing_draft(self):
        with patch("providers.generate", return_value="改写结果") as generate:
            providers.rewrite({"style":""}, "", {"title":"研究", "notes":"资料"}, 90, "现有观点")
            data = generate.call_args.args[-1]
            self.assertEqual(data["整片目标秒数"], 90)
            self.assertEqual(data["中文口播参考字数"], "450–522")
            self.assertEqual(data["现有草稿"], "现有观点")
        self.assertEqual(timing.estimated_speech("   "), 0)
        self.assertGreater(timing.estimated_speech("一段中文，包含 AI 工具。"), 0)
