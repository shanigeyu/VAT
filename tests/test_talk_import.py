import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import production
import talk_import
from store import Store, new_project, script_units, shots_from_plan


SRT = """1
00:00:00,131 --> 00:00:00,790
开头的字幕。

2
00:00:01,131 --> 00:00:01,433
点击右上角。
打开菜单。

3
00:00:01,611 --> 00:00:01,870
接着选择导出。
"""


class TalkImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.srt = self.root / "口播字幕.srt"
        self.report = lambda _: None

    def source(self):
        for name in ("ffmpeg", "ffprobe"):
            try:
                production.tool(name)
            except ValueError as exc:
                self.skipTest(str(exc))
        video = self.root / "带原声的口播.mp4"
        production.ffmpeg(
            "-f", "lavfi", "-i", "color=c=red:s=96x64:r=25:d=1",
            "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=1",
            "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=25:d=1",
            "-f", "lavfi", "-i", "sine=frequency=900:sample_rate=48000:duration=1",
            "-filter_complex", "[0:v][1:a][2:v][3:a]concat=n=2:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]", "-pix_fmt", "yuv420p", video,
        )
        self.srt.write_text(SRT, encoding="utf-8-sig")
        return talk_import.prepare(self.srt, video)

    def test_common_subtitle_encodings_keep_multiline_text_and_timing(self):
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            with self.subTest(encoding=encoding):
                self.srt.write_bytes(SRT.replace("\n\n", "\r\n\r\n\r\n\r\n").encode(encoding))
                cues = talk_import.read_srt(self.srt)
                self.assertEqual(len(cues), 3)
                self.assertEqual(cues[1], {"start": 1131, "end": 1433, "text": "点击右上角。\n打开菜单。"})

    def test_ai_match_places_trimmed_picture_voice_and_cues_in_one_shot(self):
        source = self.source()
        store = Store(self.root)
        p = new_project("口播匹配")
        script = "片头旁白。点开右上角菜单。再导出文件。片尾旁白。"
        p.update(draft=script, approved=script)
        p["shots"] = shots_from_plan(script_units(script), [
            {"end": 1, "kind": "voice", "visual": "片头"},
            {"end": 3, "kind": "talk", "visual": "操作步骤"},
            {"end": 4, "kind": "voice", "visual": "片尾"},
        ])
        p["sound"]["clip_audio"] = True
        p["captions"]["edits"] = {"3": {"start": 2300, "end": 2700, "text": "片尾字幕"}}
        voice = self.root / "旁白.wav"
        production.ffmpeg("-f", "lavfi", "-i", "sine=frequency=650:duration=0.4", voice)
        aid, = store.add_asset("video", p["id"], source.path)
        for index in (0, 2):
            p["shots"][index].update(voice_source="record_file", voice_file=str(voice), main=[{"id": aid["id"]}])
        with patch("planning.model_list", return_value=[{"shot": 2, "first": 2, "last": 3}]):
            clips = talk_import.match_shots({}, "", p, source)
        store.save_talk_import(p, source.path, clips)
        p, = store.projects()
        assets = store.assets(p["id"])
        self.assertEqual(p["shots"][1]["voice_source"], "camera")
        self.assertEqual(p["shots"][1]["main"][0]["offset"], 1.131)
        self.assertEqual(p["captions"]["edits"]["3"], {"text": "片尾字幕"})

        cues, duration = production.caption_preview(self.root, p, assets, self.report)
        self.assertEqual(duration, 1560)
        self.assertEqual([(c["start"], c["end"]) for c in cues], [(0, 400), (400, 702), (880, 1139), (1160, 1560)])
        self.assertEqual(cues[1]["text"], "点击右上角。\n打开菜单。")
        self.assertEqual(cues[-1]["text"], "片尾字幕")

        tracks = dict(production.export_tracks(self.root, p, assets, self.report))
        with wave.open(tracks["本人文案"], "rb") as track:
            track.setpos(24000)
            samples = [left for left, _ in struct.iter_unpack("<hh", track.readframes(9600))]
            self.assertAlmostEqual(sum(a <= 0 < b for a, b in zip(samples, samples[1:])), 180, delta=2)
        with wave.open(tracks["素材声"], "rb") as track:
            track.setpos(24000)
            self.assertFalse(any(track.readframes(9600)))

        exported = dict(production.render(self.root, p, assets, True, self.report))
        pixel = production.ffmpeg(
            "-ss", "0.52", "-i", exported["完整成片"], "-frames:v", 1,
            "-vf", "crop=2:2:358:638,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        )
        self.assertGreater(pixel[2], 200)
        self.assertEqual(talk_import.read_srt(exported["字幕"])[2]["start"], 880)

    def test_standalone_import_uses_full_video_and_original_srt_clock(self):
        source = self.source()
        store = Store(self.root)
        p = new_project("完整口播")
        clips = talk_import.build_timeline(p, source)
        store.save_talk_import(p, source.path, clips)
        assets = store.assets(p["id"])
        cues, duration = production.caption_preview(self.root, p, assets, self.report)
        self.assertEqual(duration, 2000)
        self.assertEqual(p["orientation"], "横屏 16∶9")
        self.assertEqual([(c["start"], c["end"], c["text"]) for c in cues],
                         [(c["start"], c["end"], c["text"]) for c in source.cues])
        exported = dict(production.render(self.root, p, assets, True, self.report))
        self.assertEqual(talk_import.read_srt(exported["字幕"]), source.cues)
        self.assertEqual({s["codec_type"] for s in production.probe(exported["完整成片"])["streams"]}, {"video", "audio"})
