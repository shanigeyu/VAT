import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

import auto_video
import production
import review
from store import Store, new_project, shots_from_plan


class ReviewTests(unittest.TestCase):
    def setUp(self):
        for name in ("ffmpeg", "ffprobe"):
            try:
                production.tool(name)
            except ValueError as exc:
                self.skipTest(str(exc))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.store = Store(self.root)
        self.project = new_project("审看测试")
        self.report = lambda _: None

    def test_fifteen_second_target_reaches_the_selected_canvas_in_preview_and_export(self):
        voice = self.root / "voice.wav"
        artwork = self.root / "artwork.png"
        production.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=14", voice)
        Image.new("RGB", (72, 96), "navy").save(artwork)
        project = auto_video.new_job("短片", "15 秒", "", orientation="竖屏 3∶4")
        (asset,) = self.store.add_asset("image", project["id"], artwork)
        project.update(draft="一条十五秒短片。", approved="一条十五秒短片。", target_seconds=15,
                       shots=[{"start": 0, "end": 1, "visual": "夜空", "kind": "voice", "main": asset["id"]}])
        assets = self.store.assets(project["id"])
        with patch("production.speech_files", return_value=[voice]):
            record = review.create_preview(self.root, project, assets, self.report)
            exports = dict(production.render(self.root, project, assets, True, self.report))
        folder = review.version_dir(self.root, project["id"], record["version"])
        manifest = json.loads((folder / review.TIMELINE).read_text(encoding="utf-8"))
        self.assertEqual((manifest["preview"]["width"], manifest["preview"]["height"]), (360, 480))
        self.assertEqual((manifest["canvas"]["width"], manifest["canvas"]["height"]), (720, 960))
        info = production.probe(exports["完整成片"])
        video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (720, 960))
        self.assertAlmostEqual(float(video["duration"]), 15, delta=.12)

    def test_package_matches_watched_cut_after_sources_and_project_change(self):
        camera = self.root / "原始口播.mp4"
        voice = self.root / "录音.wav"
        graphic = self.root / "横条.png"
        production.ffmpeg("-f", "lavfi", "-i", "color=blue:s=64x64:r=25:d=1",
                          "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000:duration=1", camera)
        production.ffmpeg("-f", "lavfi", "-i", "color=red:s=60x12", "-frames:v", 1, graphic)
        production.ffmpeg("-f", "lavfi", "-i", "sine=frequency=400:sample_rate=32000:duration=0.4", voice)
        add = lambda kind, path: self.store.add_asset(kind, self.project["id"], path)[0]["id"]
        talk, main, overlay = add("talk", camera), add("video", camera), add("effect", graphic)
        units = ["开场。", "转场。", "片尾。"]
        shots = shots_from_plan(units, [{"end": i, "kind": kind, "visual": text}
                                       for i, kind, text in ((1, "talk", "出镜"), (2, "voice", "演示"), (3, "voice", "收束"))])
        shots[0].update(main=talk, offset=0.12, length=0.24)  # Legacy single-asset representation.
        shots[1].update(main=[{"id": main, "rect": [0.5, 0, 0.25, 0.5]},
                              {"id": main, "rect": [0, 0, 0.25, 0.5]}], effect=[{"id": overlay}])
        shots[2].update(main=main, voice_source="ai_file", voice_file=str(voice), voice_offset=0.08, voice_length=0.24)
        script = "".join(units)
        self.project.update(draft=script, approved=script, shots=shots, sections=[{"title": "演示", "end": 3}],
                            orientation="横屏 16∶9", sfx=str(voice))
        self.project["sound"].update(background=str(voice), clip_audio=True)
        self.project["captions"]["edits"] = {"1": {"text": "校对后的字幕"}}
        self.project["captions"]["mode"] = production.CAPTION_MODES[0]
        assets = self.store.assets(self.project["id"])
        camera_bytes = camera.read_bytes()
        with patch("production.speech_files", return_value=[voice]) as synth:
            record = review.create_preview(self.root, self.project, assets, self.report, voice_profile="test-profile")
            self.assertEqual(synth.call_args.kwargs["voice_profile"], "test-profile")
        self.project["previews"].append(record)
        self.store.save(self.project)
        self.assertEqual(self.store.projects()[0]["previews"], [record])
        folder = review.version_dir(self.root, self.project["id"], record["version"])
        streams = production.probe(folder / review.SAMPLE)["streams"]
        picture = next(s for s in streams if s["codec_type"] == "video")
        self.assertEqual((picture["width"], picture["height"], picture["avg_frame_rate"], int(picture["nb_frames"])),
                         (640, 360, "25/1", 22))
        self.assertTrue(any(s["codec_name"] == "aac" for s in streams))
        # At 360p the square clip is 160x160 inside each 160x180 box.
        sample_pixel = production.ffmpeg("-i", folder / review.SAMPLE, "-vf", "crop=2:2:400:50,scale=1:1",
                                         "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
        self.assertGreater(sample_pixel[8 * 3 + 2], 200)
        self.assertLess(sample_pixel[12 * 3 + 2], 80)

        camera.write_bytes(b"replaced source")
        voice.unlink()
        graphic.unlink()
        self.project.update(title="已重命名", draft="下一版", orientation="竖屏 9∶16")
        destination = self.root / "交接.zip"
        review.export_package(folder, destination, self.report)
        with ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            data = archive.read(review.TIMELINE).decode("utf-8")
            self.assertNotIn(self.root.as_posix(), data.replace("\\\\", "/"))
            manifest = json.loads(data)
            self.assertEqual(manifest["title"], "审看测试")
            self.assertEqual(manifest["canvas"]["width"], 1280)
            self.assertEqual(manifest["captions"]["final_mode"], production.CAPTION_MODES[0])
            self.assertIn("校对后的字幕", archive.read(review.CAPTIONS).decode("utf-8-sig"))
            first, mid, last = manifest["shots"]
            self.assertEqual([(s["start_frame"], s["end_frame"]) for s in manifest["shots"]], [(0, 6), (6, 16), (16, 22)])
            self.assertEqual([(p["layer"], p["start_frame"], p["end_frame"]) for p in mid["pictures"]],
                             [("main", 6, 11), ("main", 11, 16), ("effect", 6, 16)])
            self.assertEqual(first["pictures"][0]["rect_px"], {"x": 280, "y": 0, "width": 720, "height": 720})
            self.assertEqual([p["rect_px"] for p in mid["pictures"][:2]],
                             [{"x": x, "y": 20, "width": 320, "height": 320} for x in (640, 0)])
            self.assertEqual(mid["pictures"][-1]["rect_px"], {"x": 0, "y": 232, "width": 1280, "height": 256})
            self.assertEqual(first["voice"][0]["source_in_seconds"], 0.12)
            self.assertEqual(last["voice"][0]["source_in_seconds"], 0.08)
            self.assertEqual(mid["material_audio"][0][-1]["start_frame"], 11)
            self.assertEqual(archive.read(first["voice"][0]["file"]), camera_bytes)
            self.assertEqual(mid["voice_source"], "synth")
            self.assertEqual(archive.read(mid["voice"][0]["file"])[:4], b"RIFF")
            self.assertEqual(len(manifest["media"]), 4)  # Camera and imported WAV each used for several roles.
            self.assertEqual(archive.read(review.SAMPLE), (folder / review.SAMPLE).read_bytes())
            paths = [m["file"] for m in manifest["media"]] + list(manifest["audio"]["stems"].values())
            self.assertTrue(all(name in archive.namelist() for name in paths))
            self.assertAlmostEqual(production.wav_seconds(folder / manifest["audio"]["mix"]), 22 / 25)
        destination.write_bytes(b"earlier ZIP")
        (folder / review.SAMPLE).unlink()
        with self.assertRaisesRegex(ValueError, "缺失"):
            review.export_package(folder, destination, self.report)
        self.assertEqual(destination.read_bytes(), b"earlier ZIP")

    def test_failed_render_does_not_publish_a_version(self):
        self.project.update(draft="开场。", approved="开场。",
                            shots=[{"start": 0, "end": 1, "visual": "开场", "kind": "talk", "main": "missing"}])
        with self.assertRaisesRegex(ValueError, "镜头 1"):
            review.create_preview(self.root, self.project, {}, self.report)
        versions = self.root / "样片" / self.project["id"]
        self.assertEqual(list(versions.iterdir()), [])
        self.assertEqual(self.project["previews"], [])
