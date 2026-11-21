import gc
import io
import json
import tempfile
import threading
import tkinter as tk
import unittest
import wave
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

import production
import speech_api
from api_profiles import protect_key
from store import Store, new_project, shots_from_plan
from studio import Studio


def example_wav(milliseconds):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", ""))
        audio.writeframes(b"\x00\x10" * (24 * milliseconds))
    return output.getvalue()


class ElevenLabsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sent = []
        self.replies = {"第一句。": (200, example_wav(160)), "第二句。": (200, example_wav(240))}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.sent.append((self.path, self.headers.get("xi-api-key"), self.headers.get("Authorization"), body))
                status, audio = test.replies[body["text"]]
                self.send_response(status)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        background = threading.Thread(target=server.serve_forever, daemon=True)
        background.start()
        self.addCleanup(background.join)
        self.addCleanup(server.shutdown)
        self.profile = speech_api.Profile(f"http://127.0.0.1:{server.server_port}/v1",
                                          speech_api.ELEVENLABS_MODEL, "my-voice-01", provider="elevenlabs")

    def require_ffmpeg(self):
        try:
            production.tool("ffmpeg")
        except ValueError as error:
            self.skipTest(str(error))

    def test_native_api_persists_secret_and_drives_voice_subtitles_and_cache(self):
        self.require_ffmpeg()
        project = new_project("ElevenLabs 配音测试")
        script = "第一句。第二句。"
        project.update(draft=script, approved=script,
                       shots=shots_from_plan(["第一句。", "第二句。"], [{"end": 2, "kind": "voice", "visual": "示例"}]))
        with patch("api_profiles.config_dir", return_value=self.root / "config"):
            key = "example-elevenlabs-token"
            profile = replace(self.profile.validated(), encrypted_key=protect_key(key).hex())
            store = Store(self.root)
            store.set_prefs({"speech_backend": "api", "speech_api": asdict(profile)})
            saved = speech_api.Profile(**Store(self.root).prefs()["speech_api"])
            self.assertEqual((saved.provider, saved.credential()), ("elevenlabs", key))
            self.assertNotIn(key.encode(), store.db.read_bytes())
            with patch("sovits.load_profile", side_effect=AssertionError("不应使用本机配音")):
                (_, output), = production.export_voice(self.root, project, lambda _: None, voice_profile=saved)
                cues, end = production.caption_preview(self.root, project, {}, lambda _: None, voice_profile=saved)
                production.export_voice(self.root, project, lambda _: None, voice_profile=saved)

            with wave.open(str(output), "rb") as audio:
                self.assertEqual(audio.getparams()[:4], (1, 2, 48000, 19200))
            self.assertEqual([(cue["start"], cue["end"]) for cue in cues], [(0, 160), (160, 400)])
            self.assertEqual(end, 400)
            self.assertEqual(self.sent, [
                ("/v1/text-to-speech/my-voice-01?output_format=wav_24000", key, None,
                 {"text": text, "model_id": speech_api.ELEVENLABS_MODEL})
                for text in ("第一句。", "第二句。")])
            production.export_voice(self.root, project, lambda _: None,
                                    voice_profile=replace(saved, voice="my-voice-02"))
            self.assertEqual(len(self.sent), 4)
            self.assertTrue(all("/my-voice-02?" in path for path, *_ in self.sent[2:]))
            self.assertEqual(len(list((self.root / "语音缓存").glob("*.wav"))), 4)

    def test_native_api_reports_auth_errors_and_rejects_non_wav(self):
        with self.assertRaisesRegex(ValueError, "填写 ElevenLabs API 密钥"):
            speech_api.request_wav("第一句。", self.profile, "")
        self.assertEqual(self.sent, [])
        self.replies["第一句。"] = (401, b"private response: example-secret")
        with self.assertRaisesRegex(ValueError, "HTTP 401") as error:
            speech_api.request_wav("第一句。", self.profile, "example-secret")
        self.assertNotIn("example-secret", str(error.exception))
        self.replies["第一句。"] = (200, b'{"error":"not audio"}')
        with self.assertRaisesRegex(ValueError, "ElevenLabs 未返回 WAV"):
            speech_api.request_wav("第一句。", self.profile, "example-secret")
        self.replies["第一句。"] = (200, example_wav(160)[:-10])
        with self.assertRaisesRegex(ValueError, "WAV 数据不完整"):
            speech_api.request_wav("第一句。", self.profile, "example-secret")

    def test_base_url_normalization_and_voice_id_escaping(self):
        for endpoint in ("https://api.elevenlabs.io", "https://api.elevenlabs.io/v1/",
                         "https://api.elevenlabs.io/v1/text-to-speech"):
            profile = replace(self.profile, endpoint=endpoint).validated()
            self.assertEqual(profile.endpoint, "https://api.elevenlabs.io/v1/text-to-speech")
            self.assertEqual(profile.validated(), profile)
        with self.assertRaisesRegex(ValueError, "网页地址"):
            replace(self.profile, endpoint="https://elevenlabs.io/app/developers").validated()
        with self.assertRaisesRegex(ValueError, "支持的语音 API 服务"):
            replace(self.profile, provider="unsupported").validated()
        speech_api.request_wav("第一句。", replace(self.profile, voice="voice/with space?query"), "sample-key")
        self.assertEqual(self.sent[0][0],
                         "/v1/text-to-speech/voice%2Fwith%20space%3Fquery?output_format=wav_24000")

    @staticmethod
    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from ElevenLabsTests.widgets(child)

    def control(self, parent, caption, kind):
        row = next(widget.master for widget in self.widgets(parent)
                   if isinstance(widget, tk.Label) and widget.cget("text") == caption)
        return next(widget for widget in row.winfo_children() if isinstance(widget, kind))

    def test_settings_selects_elevenlabs_and_keeps_its_own_key_after_reopening(self):
        with patch("api_profiles.config_dir", return_value=self.root / "config"):
            store = Store(self.root)
            store.save(new_project("配音设置测试"))
            original = speech_api.Profile("https://speech.example/v1/audio/speech", "original-tts", "original-voice",
                                          protect_key("original-service-key").hex())
            store.set_prefs({"speech_backend": "api", "speech_api": asdict(original)})
            try:
                window = tk.Tk()
            except tk.TclError as error:
                self.skipTest(str(error))
            self.addCleanup(gc.collect)
            self.addCleanup(window.destroy)
            window.withdraw()
            studio = Studio(window, store)
            self.addCleanup(lambda: window.after_cancel(studio.timer))
            studio.go("audio")
            studio.audio_profile_dialog()
            dialog = window.grab_current()
            provider = self.control(dialog, "语音服务", ttk.Combobox)
            provider.set("ElevenLabs")
            provider.event_generate("<<ComboboxSelected>>")
            self.assertEqual(self.control(dialog, "接口地址", tk.Entry).get(), speech_api.ELEVENLABS_BASE)
            self.assertEqual(self.control(dialog, "语音模型", tk.Entry).get(), speech_api.ELEVENLABS_MODEL)
            self.assertEqual(self.control(dialog, "音色", tk.Entry).get(), "")
            self.assertEqual(self.control(dialog, "API 密钥", tk.Entry).get(), "")
            self.control(dialog, "音色", tk.Entry).insert(0, "my-voice-01")
            self.control(dialog, "API 密钥", tk.Entry).insert(0, "native-service-key")
            save = lambda parent: next(widget for widget in self.widgets(parent)
                                      if isinstance(widget, tk.Button) and widget.cget("text") == "保存设置").invoke()
            save(dialog)
            studio.save_now()
            saved = speech_api.Profile(**Store(self.root).prefs()["speech_api"])
            self.assertEqual((saved.provider, saved.model, saved.voice, saved.credential()),
                             ("elevenlabs", speech_api.ELEVENLABS_MODEL, "my-voice-01", "native-service-key"))
            studio.audio_profile_dialog()
            dialog = window.grab_current()
            self.assertEqual(self.control(dialog, "语音服务", ttk.Combobox).get(), "ElevenLabs")
            self.assertEqual(self.control(dialog, "音色", tk.Entry).get(), "my-voice-01")
            save(dialog)
            self.assertEqual(Store(self.root).prefs()["speech_api"], asdict(saved))
            studio.audio_profile_dialog()
            dialog = window.grab_current()
            provider = self.control(dialog, "语音服务", ttk.Combobox)
            provider.set(speech_api.PROVIDERS["openai"])
            provider.event_generate("<<ComboboxSelected>>")
            self.control(dialog, "接口地址", tk.Entry).insert(0, "https://speech.example/v1")
            self.control(dialog, "语音模型", tk.Entry).insert(0, "other-tts")
            self.control(dialog, "音色", tk.Entry).insert(0, "other-voice")
            save(dialog)
            switched = speech_api.Profile(**Store(self.root).prefs()["speech_api"])
            self.assertEqual((switched.provider, switched.encrypted_key), ("openai", ""))
