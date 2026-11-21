import base64
import gc
import hashlib
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
from widgets import SoftButton


def example_wav(rate, milliseconds, *, streaming=False):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", ""))
        audio.writeframes(b"\x00\x10" * (rate * milliseconds // 1000))
    data = buffer.getvalue()
    if streaming:
        unknown = b"\xff\xff\xff\xff"
        return data[:4] + unknown + data[8:40] + unknown + data[44:]
    return data


class SpeechAPITests(unittest.TestCase):
    @staticmethod
    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from SpeechAPITests.widgets(child)

    def text_input(self, parent, caption):
        row = next(widget.master for widget in self.widgets(parent)
                   if isinstance(widget, tk.Label) and widget.cget("text") == caption)
        return next(widget for widget in row.winfo_children() if isinstance(widget, tk.Entry))

    def desktop(self, store):
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
        return studio, window

    def setUp(self):
        try:
            production.tool("ffmpeg")
        except ValueError as exc:
            self.skipTest(str(exc))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sent = []
        self.replies = {}
        self.audio_model_replies = {}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.sent.append((self.path, self.headers.get("Authorization"), body))
                if self.path == "/v1/chat/completions":
                    status, audio = test.audio_model_replies[body["model"]]
                else:
                    status, audio = test.replies[body["input"]]
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/octet-stream")
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
        self.profile = speech_api.Profile(f"http://127.0.0.1:{server.server_port}/v1", "test-tts", "test-voice")
        self.project = new_project("语音 API 测试")
        script = "第一句。第二句。"
        self.project.update(draft=script, approved=script,
                            shots=shots_from_plan(["第一句。", "第二句。"], [{"end": 2, "kind": "voice", "visual": "示例"}]))
        self.report = lambda _: None

    def test_saved_api_profile_drives_voice_and_subtitle_timing(self):
        self.replies.update({"第一句。": (200, example_wav(24000, 160, streaming=True)),
                             "第二句。": (200, example_wav(32000, 240))})
        store = Store(self.root)
        self.assertEqual(store.prefs()["speech_backend"], "local")
        with patch("api_profiles.config_dir", return_value=self.root / "config"):
            key = "example-speech-token"
            profile = replace(self.profile.validated(), encrypted_key=protect_key(key).hex())
            store.set_prefs({"speech_backend": "api", "speech_api": asdict(profile)})
            prefs = Store(self.root).prefs()
            saved = speech_api.Profile(**prefs["speech_api"])
            self.assertEqual((prefs["speech_backend"], saved.credential()), ("api", key))
            self.assertNotIn(key.encode(), store.db.read_bytes())
            with patch("sovits.load_profile", side_effect=AssertionError("本机音色配置不应被读取")):
                (_, path), = production.export_voice(self.root, self.project, self.report, voice_profile=saved)
                cues, end = production.caption_preview(self.root, self.project, {}, self.report, voice_profile=saved)

        with wave.open(path, "rb") as audio:
            self.assertEqual(audio.getparams()[:4], (1, 2, 48000, 19200))
        self.assertEqual([(cue["start"], cue["end"]) for cue in cues], [(0, 160), (160, 400)])
        self.assertEqual(end, 400)
        self.assertEqual(self.sent, [("/v1/audio/speech", "Bearer " + key,
                                    {"input": text, "model": "test-tts", "voice": "test-voice", "response_format": "wav"})
                                    for text in ("第一句。", "第二句。")])

    def test_retry_reuses_completed_sentences_and_rejects_non_audio(self):
        self.replies.update({"第一句。": (200, example_wav(24000, 160)),
                             "第二句。": (401, b"private server response")})
        with self.assertRaisesRegex(ValueError, "第二句。.*\n语音 API 返回 HTTP 401") as error:
            production.export_voice(self.root, self.project, self.report, voice_profile=self.profile)
        self.assertNotIn("private server response", str(error.exception))
        self.assertEqual(len(list((self.root / "语音缓存").glob("*.wav"))), 1)
        self.assertFalse(list((self.root / "导出").rglob("*.wav")))

        self.replies["第二句。"] = (200, b'{"error":"no wav"}')
        with self.assertRaisesRegex(ValueError, "未返回 WAV"):
            production.export_voice(self.root, self.project, self.report, voice_profile=self.profile)
        self.replies["第二句。"] = (200, example_wav(32000, 240)[:-10])
        with self.assertRaisesRegex(ValueError, "WAV 数据不完整"):
            production.export_voice(self.root, self.project, self.report, voice_profile=self.profile)
        self.replies["第二句。"] = (200, example_wav(32000, 240))
        production.export_voice(self.root, self.project, self.report, voice_profile=self.profile)
        self.assertEqual([body["input"] for _, _, body in self.sent], ["第一句。"] + ["第二句。"] * 4)

    def test_audio_models_generate_voice_directly_and_reuse_cached_audio(self):
        sources = [
            (speech_api.AudioModel(self.profile.endpoint, "gpt-audio", "alloy"), 160),
            (speech_api.AudioModel(self.profile.endpoint, "qwen-local", provider="vllm"), 240),
        ]
        for audio_model, duration in sources:
            wav = base64.b64encode(example_wav(24000, duration)).decode("ascii")
            choice = {"message": {"audio": {"data": wav}}}
            choices = [{"message": {"content": "旁白"}}, choice] if audio_model.provider == "vllm" else [choice]
            self.audio_model_replies[audio_model.model] = (200, json.dumps({"choices": choices}).encode())

        with (patch("speech_api.request_wav") as speech_only, patch("sovits.load_profile") as sovits_only,
              patch("providers.generate") as text_only):
            for voice, duration in sources:
                with self.subTest(voice=voice):
                    (_, path), = production.export_voice(self.root, self.project, self.report, voice_profile=voice)
                    cues, end = production.caption_preview(self.root, self.project, {}, self.report, voice_profile=voice)
                    with wave.open(path, "rb") as audio:
                        self.assertEqual(audio.getparams()[:4], (1, 2, 48000, 96 * duration))
                    self.assertEqual([cue["text"] for cue in cues], ["第一句。", "第二句。"])
                    self.assertEqual(end, 2 * duration)
        speech_only.assert_not_called()
        sovits_only.assert_not_called()
        text_only.assert_not_called()
        self.assertEqual(len(self.sent), 4)
        for (path, _, body), text in zip(self.sent, ["第一句。", "第二句。"] * 2):
            self.assertEqual(path, "/v1/chat/completions")
            self.assertIn("audio", body["modalities"])
            self.assertIn(text, json.dumps(body["messages"], ensure_ascii=False))

    def test_voice_direction_reaches_speech_requests_and_separates_the_cache(self):
        for text in ("第一句。", "第二句。"):
            self.replies[text] = (200, example_wav(24000, 120))
        plain = self.profile
        moody = replace(self.profile, direction="  开头惊讶，\n结尾坚定  ")
        production.export_voice(self.root, self.project, self.report, voice_profile=plain)
        production.export_voice(self.root, self.project, self.report, voice_profile=plain)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("instructions" not in body for _, _, body in self.sent))
        production.export_voice(self.root, self.project, self.report, voice_profile=moody)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual({body["instructions"] for _, _, body in self.sent[2:]}, {"开头惊讶， 结尾坚定"})

        audio_model = speech_api.AudioModel(self.profile.endpoint, "gpt-audio", "alloy", direction="温柔")
        wav = base64.b64encode(example_wav(24000, 120)).decode("ascii")
        self.audio_model_replies["gpt-audio"] = (200, json.dumps(
            {"choices": [{"message": {"audio": {"data": wav}}}]}).encode())
        production.export_voice(self.root, self.project, self.report, voice_profile=audio_model)
        prompt = self.sent[-1][2]["messages"][0]["content"]
        self.assertIn("语气与情感：温柔", prompt)
        self.assertTrue(prompt.endswith("第二句。"))

    def test_audio_model_rejects_a_text_only_response(self):
        voice = speech_api.AudioModel(self.profile.endpoint, "text-only", "alloy")
        payload = {"choices": [{"message": {"content": "收到。"}}]}
        self.audio_model_replies[voice.model] = (200, json.dumps(payload).encode())
        with self.assertRaisesRegex(ValueError, "音频"):
            production.export_voice(self.root, self.project, self.report, voice_profile=voice)
        self.assertFalse(list((self.root / "语音缓存").glob("*.wav")))
        self.assertFalse(list((self.root / "导出").rglob("*.wav")))

    def test_qwen_audio_model_stream_decodes_pcm_into_narration(self):
        pcm = b"\x00\x10" * (24000 * 160 // 1000)
        encoded = base64.b64encode(pcm).decode("ascii")
        fragments = [encoded[:17], encoded[17:]]
        events = [{"choices": [{"delta": {"audio": {"data": part}}}]} for part in fragments]
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        with patch("api_profiles.config_dir", return_value=self.root / "config"):
            key = "test-qwen-key"
            voice = speech_api.AudioModel(self.profile.endpoint, "qwen3-omni-flash", "Cherry",
                                          protect_key(key).hex(), provider="qwen")
            self.audio_model_replies[voice.model] = (200, stream.encode("utf-8"))
            (_, path), = production.export_voice(self.root, self.project, self.report, voice_profile=voice)
        self.assertTrue(all(auth == "Bearer " + key and body["stream"] for _, auth, body in self.sent))
        with wave.open(path, "rb") as audio:
            self.assertEqual(audio.getparams()[:4], (1, 2, 48000, 15360))

    def test_local_sovits_retains_existing_cache(self):
        profile = {"model_id": "installed-voice", "reference": "existing.wav"}
        text = "第一句。"
        identity = json.dumps(["gpt-sovits-v1", profile], ensure_ascii=False, sort_keys=True)
        filename = hashlib.sha256((identity + "\n" + text).encode("utf-8")).hexdigest() + ".wav"
        cache = self.root / "语音缓存"
        cache.mkdir()
        cached = cache / filename
        cached.write_bytes(example_wav(32000, 160))
        with patch("sovits.load_profile", return_value=profile) as load, patch("sovits.ensure_service") as service:
            paths = production.speech_files(self.root, [text], self.root, self.report, voice_profile="existing.json")
        self.assertEqual(paths, [cached])
        load.assert_called_once_with("existing.json")
        service.assert_not_called()

    def test_audio_page_sends_customer_voice_to_the_independent_speech_api(self):
        self.replies.update({"第一句。": (200, example_wav(24000, 160)),
                             "第二句。": (200, example_wav(32000, 240))})
        store = Store(self.root)
        store.save(self.project)
        store.save_api_profile({"base": "https://text-model.example/v1", "model": "writer"}, "different-model-key")
        profile = replace(self.profile.validated(), encrypted_key=protect_key("speech-only-key").hex())
        store.set_prefs({"speech_backend": "api", "speech_api": asdict(profile),
                         "step_model.voice": {"engine": "DSH"}})
        studio, window = self.desktop(store)
        notebook = next(w for w in self.widgets(studio.body) if isinstance(w, ttk.Notebook))
        notebook.select(2)
        voice = self.text_input(studio.body, "音色")
        customer_voice = "brandVoice:女声_01/v2"
        voice.delete(0, "end")
        voice.insert(0, customer_voice)
        with patch.object(studio, "job") as job, patch("sovits.load_profile") as local_voice:
            studio.voice()
            worker = job.call_args.args[1]
            worker(self.report)
            local_voice.assert_not_called()
        saved = Store(self.root).prefs()
        self.assertEqual(saved["speech_api"], {**asdict(profile), "voice": customer_voice})
        self.assertEqual([body["voice"] for _, _, body in self.sent], [customer_voice, customer_voice])
        self.assertEqual({(path, key) for path, key, _ in self.sent},
                         {("/v1/audio/speech", "Bearer speech-only-key")})
        self.assertTrue(all(body["model"] == "test-tts" for _, _, body in self.sent))
        studio.go("topics")
        studio.go("audio")
        self.assertEqual(self.text_input(studio.body, "音色").get(), customer_voice)

    def test_dialog_keeps_new_voice_and_model_instead_of_stale_page_values(self):
        store = Store(self.root)
        store.save(self.project)
        profile = replace(self.profile.validated(), encrypted_key=protect_key("saved-speech-key").hex())
        store.set_pref("speech_api", asdict(profile))
        studio, window = self.desktop(store)
        route = next(w for w in self.widgets(studio.body) if isinstance(w, ttk.Combobox))
        route.set(speech_api.BACKENDS["api"])
        route.event_generate("<<ComboboxSelected>>")
        voice = self.text_input(studio.body, "音色")
        voice.delete(0, "end")
        voice.insert(0, "pending-inline-voice")
        studio.audio_profile_dialog()
        dialog = window.grab_current()
        voice = self.text_input(dialog, "音色")
        self.assertEqual(voice.get(), "pending-inline-voice")
        voice.delete(0, "end")
        voice.insert(0, "new-dialog-voice")
        model = self.text_input(dialog, "语音模型")
        model.delete(0, "end")
        model.insert(0, "new-tts")
        next(w for w in self.widgets(dialog)
             if isinstance(w, (tk.Button, SoftButton)) and w.cget("text") == "保存设置").invoke()
        studio.save_now()
        saved = Store(self.root).prefs()
        self.assertEqual(saved["speech_backend"], "api")
        self.assertEqual(saved["speech_api"],
                         {**asdict(profile), "model": "new-tts", "voice": "new-dialog-voice"})
        self.assertEqual(self.text_input(studio.body, "音色").get(), "new-dialog-voice")
