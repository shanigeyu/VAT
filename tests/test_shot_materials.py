import struct
import tempfile
import unittest
import wave
from pathlib import Path

from production import FPS, caption_preview, export_tracks, ffmpeg, normalized_rect, render, tool
from store import Store, new_project, shot_clips, shots_from_plan


class ShotMaterialsTests(unittest.TestCase):
    def setUp(self):
        for name in ("ffmpeg", "ffprobe"):
            try:
                tool(name)
            except ValueError as exc:
                self.skipTest(str(exc))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root)
        self.project = new_project("多素材镜头")
        self.project.update(draft="第一句。", approved="第一句。",
                            sections=[{"title": "正文", "end": 1}])

    def video(self, color, category, tone=None, size="64x64"):
        path = self.root / f"{color}.mp4"
        args = ["-f", "lavfi", "-i", f"color={color}:s={size}:r={FPS}:d=1.2"]
        if tone is not None:
            args += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration=1.2"]
        ffmpeg(*args, "-pix_fmt", "yuv420p", path)
        return self.store.add_asset(category, self.project["id"], path)[0]["id"]

    def shot(self, kind, **fields):
        self.project["shots"] = shots_from_plan(["第一句。"], [{"end": 1, "visual": "画面", "kind": kind}])
        self.project["shots"][0].update(fields)
        self.store.save(self.project)
        self.project = self.store.projects()[0]
        return self.store.assets(self.project["id"])

    def colors(self, path, x=358, y=638):
        data = ffmpeg("-i", path, "-vf", f"crop=2:2:{x}:{y},scale=1:1",
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
        return list(struct.iter_unpack("BBB", data))

    def tone(self, path, begin, end):
        with wave.open(path, "rb") as track:
            rate = track.getframerate()
            track.setpos(round(begin * rate))
            samples = [left for left, _ in struct.iter_unpack("<hh", track.readframes(round((end-begin) * rate)))]
        return sum(x <= 0 < y for x, y in zip(samples, samples[1:])) / (end-begin)

    def test_video_and_material_audio_follow_the_saved_order(self):
        ordered = [self.video("red", "video", 300), self.video("green", "video"),
                   self.video("blue", "video", 900)]
        bottom = [self.video("black", "theme"), self.video("white", "theme")]
        effects = [self.video("yellow", "effect", size="64x8"), self.video("cyan", "effect", size="64x8")]
        narrator = self.root / "voice.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000:duration=1.44", narrator)
        self.project["sound"]["clip_audio"] = True
        assets = self.shot("voice", main=[{"id": aid} for aid in ordered],
                           theme=[{"id": aid} for aid in bottom], effect=[{"id": aid} for aid in effects],
                           voice_source="record_file", voice_file=str(narrator))
        self.assertEqual([clip["id"] for clip in shot_clips(self.project["shots"][0], "main")], ordered)

        report = lambda _: None
        files = dict(render(self.root, self.project, assets, True, report))
        path = files["完整成片"]
        picture = self.colors(path, y=400)
        self.assertEqual(len(picture), 36)
        for start, channel in ((0, 0), (12, 1), (24, 2)):
            self.assertTrue(all(pixel[channel] > 100 and pixel.count(max(pixel)) == 1
                                and pixel[channel] == max(pixel) for pixel in picture[start:start + 12]))
        base = self.colors(path, y=40)
        self.assertTrue(all(max(pixel) < 10 for pixel in base[:18]))
        self.assertTrue(all(min(pixel) > 240 for pixel in base[18:]))
        overlay = self.colors(path)
        self.assertTrue(all(r > 200 and g > 200 and b < 20 for r, g, b in overlay[:18]))
        self.assertTrue(all(r < 20 and g > 200 and b > 200 for r, g, b in overlay[18:]))

        audio = dict(export_tracks(self.root, self.project, assets, report))["素材声"]
        with wave.open(audio, "rb") as track:
            self.assertEqual(track.getnframes(), 36 * (48000 // FPS))
        self.assertAlmostEqual(self.tone(audio, 0.1, 0.4), 300, delta=10)
        self.assertEqual(self.tone(audio, 0.6, 0.9), 0)
        self.assertAlmostEqual(self.tone(audio, 1.1, 1.4), 900, delta=10)

    def test_camera_uses_each_trim_and_legacy_trim(self):
        first, second = self.video("red", "talk", 300), self.video("blue", "talk", 900)
        report = lambda _: None
        assets = self.shot("talk", main=first, offset=0.16, length=0.4)
        self.assertEqual(caption_preview(self.root, self.project, assets, report)[1], 400)

        assets = self.shot("talk", main=[{"id": first, "offset": 0.16, "length": 0.4},
                                         {"id": second, "offset": 0.2, "length": 0.8}])
        self.assertEqual(caption_preview(self.root, self.project, assets, report)[1], 1200)
        picture = self.colors(dict(render(self.root, self.project, assets, False, report))["无字幕画面"])
        self.assertEqual(len(picture), 30)
        self.assertTrue(all(red > 200 for red, _, _ in picture[:10]))
        self.assertTrue(all(blue > 200 for _, _, blue in picture[10:]))
        audio = dict(export_tracks(self.root, self.project, assets, report))["本人文案"]
        self.assertAlmostEqual(self.tone(audio, 0.10, 0.30), 300, delta=10)
        self.assertAlmostEqual(self.tone(audio, 0.55, 0.85), 900, delta=10)

    def test_camera_is_contained_in_the_saved_frame_box(self):
        camera = self.video("red", "talk", 300)
        assets = self.shot("talk", main=[{"id": camera, "length": 0.2, "rect": [0.5, 0, 0.5, 0.5]}])
        path = dict(render(self.root, self.project, assets, False, lambda _: None))["无字幕画面"]
        # The 360x640 box fits a 360x360 square, centered at (540,320).
        self.assertTrue(all(r > 200 for r, _, _ in self.colors(path, 530, 320)))
        for x, y in [(358, 320), (530, 120), (530, 520)]:
            self.assertTrue(all(max(rgb) < 60 for rgb in self.colors(path, x, y)))
        for invalid in ([0, 0, 0, 1], [0, -0.1, 1, 1], [0.5, 0, 0.6, 1], [0, 0, float("nan"), 1]):
            with self.assertRaisesRegex(ValueError, "画框"):
                normalized_rect(invalid)
