import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auto_video
import planning
import production
from speech_api import Profile
from store import Store


PLAN = {"title": "整理文件的小方法", "angle": "用下载文件夹做一个示例", "seconds": 40,
        "style": "简约图解", "palette": "简约图解", "estimate": [90, 160]}


class AutomaticVideoTests(unittest.TestCase):
    def test_duration_formats_and_explicit_values_take_priority(self):
        for text in ("90", "90 秒", "1:30", "1 分 30 秒", "1分30秒"):
            with self.subTest(text=text):
                self.assertEqual(auto_video.duration_seconds(text), 90)
        self.assertIsNone(auto_video.duration_seconds(" "))
        for text in ("15", "15 秒", "0:15", "0分15秒"):
            with self.subTest(text=text):
                self.assertEqual(auto_video.duration_seconds(text), 15)
        for text in ("0", "0:00", "1:99", "随便", "14秒", "0:14"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                auto_video.duration_seconds(text)
        project = auto_video.new_job("整理文件", "90秒", "暗黑科技")
        with patch("providers.generate", return_value="```json\n" + json.dumps(PLAN) + "\n```") as generate:
            plan = auto_video.make_plan({}, "", project["auto_video"]["request"], 2)
        self.assertEqual(generate.call_args.args[-1]["用户选题"], "整理文件")
        self.assertEqual((plan["seconds"], plan["style"], plan["palette"]), (90, "暗黑科技", "暗黑科技"))
        self.assertEqual(plan["estimate"][0], 90)
        self.assertGreaterEqual(plan["estimate"][1], 225)

    def test_automatic_duration_respects_the_minimum_and_formats_keep_their_ratios(self):
        request = auto_video.new_job("", "", "")["auto_video"]["request"]
        with patch("providers.generate", return_value=json.dumps({**PLAN, "seconds": 5})):
            self.assertEqual(auto_video.make_plan({}, "", request, 0)["seconds"], 15)
        for name, dimensions in (("竖屏 9∶16", (720, 1280)), ("竖屏 3∶4", (720, 960)),
                                 ("横屏 16∶9", (1280, 720)), ("方形 1∶1", (720, 720))):
            with self.subTest(name=name):
                self.assertEqual(auto_video.new_job("", "15秒", "", orientation=name)["orientation"], name)
                self.assertEqual(production.picture_size(name), dimensions)
                self.assertEqual(production.picture_size(name, 360), tuple(edge // 2 for edge in dimensions))

    def test_slow_planner_increases_the_estimate(self):
        request = auto_video.new_job("", "", "")["auto_video"]["request"]
        with (patch("providers.generate", return_value=json.dumps(PLAN)),
              patch("auto_video.time.monotonic", side_effect=[0, 140])):
            plan = auto_video.make_plan({}, "", request, 0)
        # The visual director also runs for an empty library.
        self.assertEqual(plan["estimate"], [385, 870])

    def test_blank_job_runs_each_role_and_resumes_at_failed_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root)
            project = auto_video.new_job("", "", "", orientation="横屏 16∶9", asset_reference="high")
            store.save(project)
            image = root / "下载文件夹.png"
            image.touch()
            asset, = store.add_asset("image", project["id"], image)
            routes = []
            events = []

            def engine(page):
                routes.append(page)
                return {"style": ""}, ""

            replies = [json.dumps(PLAN), "先打开下载文件夹。再把资料按用途归类。",
                       '[{"end":2,"kind":"talk","visual":"下载文件夹的分类示例"}]',
                       json.dumps([{"shot": 1, "main": "1", "svg": '<svg viewBox="0 0 100 100"><circle r="10" cy="40" cx="20"><animate attributeName="cx" from="20" to="80" dur="1s"/></circle></svg>'}])]
            video = root / "成片.mp4"
            entries = [("字幕", str(root / "字幕.srt")), ("完整成片", str(video))]
            metadata = {"streams": [{"codec_type": "video", "duration": "40.04"}]}
            with (patch("providers.generate", side_effect=replies) as generate,
                  patch("planning.read_assets", return_value={}), patch("production.tool"),
                  patch("production.render", side_effect=[ValueError("测试中断"), entries]) as render,
                  patch("production.probe", return_value=metadata)):
                with self.assertRaisesRegex(ValueError, "配音、字幕与成片未完成"):
                    auto_video.run(store, project, engine, "", events.append)

                saved, = Store(root).projects()
                self.assertEqual(saved["auto_video"]["next_stage"], "render")
                self.assertEqual(saved["approved"], saved["draft"])
                self.assertEqual(saved["shots"][0]["main"], [{"id": asset["id"]}])
                self.assertTrue(all(shot["kind"] == "voice" for shot in saved["shots"]))
                self.assertEqual(saved["exports"], [])
                saved["auto_video"]["models"] = {"render": {"engine": "DSH"}}
                voice = Profile("https://speech.example/v1", "speech", "voice")
                engine_on_resume = Mock(side_effect=AssertionError("不应再次调用模型"))
                auto_video.run(store, saved, engine_on_resume, voice, events.append)
                engine_on_resume.assert_not_called()
                self.assertEqual(render.call_args.kwargs["voice_profile"], voice)
                self.assertEqual(generate.call_count, 4)
                self.assertEqual(generate.call_args_list[1].args[-1]["中文口播参考字数"], "160–188")
                for stage_call in (0, 2, 3):
                    brief, data = generate.call_args_list[stage_call].args[-2:]
                    self.assertEqual(data["画幅"], "横屏 16∶9")
                    self.assertIn(planning.ASSET_REFERENCES["high"].prompt, brief)
                    self.assertIn(planning.ASSET_REFERENCE_RULE, brief)
                self.assertEqual(saved["auto_video"]["request"]["asset_reference"], "high")

            self.assertEqual(routes, ["auto_video", "script", "shots", "shots"])
            self.assertEqual([event.stage for event in events], [*auto_video.STAGES, "render"])
            self.assertEqual(events[1].plan["estimate"], PLAN["estimate"])
            saved, = store.projects()
            self.assertEqual(saved["auto_video"]["next_stage"], "done")
            self.assertEqual(saved["auto_video"]["video_path"], str(video))
            self.assertEqual(len(saved["exports"]), 2)
