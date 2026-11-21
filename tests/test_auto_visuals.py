import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import planning
import production
from store import new_project, shots_from_plan
from talk_import import read_srt


class AutoVisualTests(unittest.TestCase):
    def test_repository_episode_does_not_pick_an_unrelated_episode_by_generic_words(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "其他项目的安装准备.mp4"
            path.touch()
            p = new_project("第一期")
            p.update(approved="准备安装工具。", topic_id="topic",
                     topics=[{"id": "topic", "title": "example/ornament", "url": "https://github.com/example/ornament"}])
            p["shots"] = shots_from_plan([p["approved"]], [{"end": 1, "kind": "voice", "visual": "工具安装"}])
            assets = {"other": {"name": path.name, "category": "video", "path": str(path)}}
            with patch("planning.read_assets", return_value={"other": [{"text": "安装工具和文档", "offset": 1}]}):
                self.assertEqual(planning.auto_library_subjects(p, assets, folder)["shots"][0]["main"], [])

    def test_library_uses_frame_text_and_keeps_manual_assignment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "VID_0084.mp4"
            path.touch()
            p = new_project("选择素材")
            p["approved"] = "按公司查询岗位。"
            p["shots"] = shots_from_plan([p["approved"]], [{"end": 1, "kind": "voice", "visual": "公司目录"}])
            assets = {"sample": {"name": path.name, "category": "video", "path": str(path)}}
            hints = {"sample": [{"offset": 0, "text": "更新记录"}, {"offset": 4.8, "text": "公司目录 岗位筛选"}]}
            with patch("planning.read_assets", return_value=hints):
                resolved = planning.auto_library_subjects(p, assets, folder)
                self.assertEqual(resolved["shots"][0]["main"], [{"id": "sample", "offset": 4.8}])
                self.assertEqual(p["shots"][0]["main"], [])
                p["shots"][0]["main"] = [{"id": "manual", "offset": 1.2}]
                self.assertEqual(planning.auto_library_subjects(p, assets, folder), p)

    def test_empty_library_and_effect_only_export_have_real_pictures_and_captions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / "audio.wav"
            production.ffmpeg("-f", "lavfi", "-i", "sine=duration=0.6:sample_rate=32000", voice)
            overlay = root / "accent.png"
            production.ffmpeg("-f", "lavfi", "-i", "color=black@0.0:s=64x64,format=rgba",
                              "-frames:v", 1, overlay)
            units = ["第一步整理资料。", "第二步核对结果。"]
            project = new_project("操作示例")
            project.update(approved="".join(units), draft="".join(units),
                           shots=shots_from_plan(units, [{"end": i+1, "kind": "voice", "visual": unit}
                                                        for i, unit in enumerate(units)]))
            for shot in project["shots"]:
                shot.update(voice_source="record_file", voice_file=str(voice))
            project["shots"][1]["effect"] = [{"id": "accent"}]
            assets = {"accent": {"name": "accent", "category": "effect", "path": str(overlay)}}
            exports = dict(production.render(root, project, assets, True, lambda _: None))
            self.assertNotEqual(Path(exports["完整成片"]).stem, Path(exports["字幕"]).stem)
            self.assertEqual([cue["text"] for cue in read_srt(exports["字幕"])], units)
            def pixels(file, time, crop):
                return production.ffmpeg("-ss", time, "-i", file, "-vf", crop + ",scale=96:50",
                                         "-frames:v", 1, "-pix_fmt", "gray", "-f", "rawvideo", "-")
            for t in (.24, .84):
                clean = pixels(exports["无字幕画面"], t, "crop=iw:ih/5:0:ih-ih/5")
                burnt = pixels(exports["完整成片"], t, "crop=iw:ih/5:0:ih-ih/5")
                self.assertGreater(sum(b > a + 30 for a,b in zip(clean,burnt)), 20)
                shot = pixels(exports["无字幕画面"], t, "crop=iw:ih/2:0:ih/5")
                self.assertGreater(max(shot) - min(shot), 100)

    def test_external_and_switchable_modes_remain_selectable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            visual, audio, srt = root / "visual.mp4", root / "audio.wav", root / "captions.srt"
            production.ffmpeg("-f", "lavfi", "-i", "color=red:s=96x64:d=0.4", visual)
            production.ffmpeg("-f", "lavfi", "-i", "sine=duration=0.4", audio)
            production.write_srt(srt, [{"text": "测试", "start": 0, "end": 400}], 400)
            for idx in (0, 2):
                out = root / f"out-{idx}.mp4"
                production.mux_video(visual, audio, out, production.CAPTION_MODES[idx], srt)
                streams = {s["codec_type"] for s in production.probe(out)["streams"]}
                self.assertEqual("subtitle" in streams, idx == 2)
            files = dict(production.publish(root, "test", {"完整成片": out, "字幕": srt}, matching_sidecar=True))
            self.assertEqual(Path(files["完整成片"]).stem, Path(files["字幕"]).stem)
