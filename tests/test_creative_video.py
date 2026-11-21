import base64
import json
import tempfile
import threading
import tkinter as tk
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from PIL import Image

import auto_video
import generated_images
import planning
import production
import providers
import review
from creative_art import Artwork
from store import Store
from studio import Studio


SVG = """<svg viewBox="0 0 72 128" xmlns="http://www.w3.org/2000/svg">
<rect x="0" y="32" width="14" height="16" fill="#ff6400">
<animate attributeName="x" from="0" to="56" dur="1s" fill="freeze"/>
</rect></svg>"""


class CreativeVideoTests(unittest.TestCase):
    def test_one_click_controls_save_independent_choices_and_freeform_direction(self):
        def walk(parent):
            for child in parent.winfo_children():
                yield child
                yield from walk(child)

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.set_prefs({"engine": "Codex", "codex_model": "default-writer"})
            try:
                window = tk.Tk()
            except tk.TclError as error:
                self.skipTest(str(error))
            window.withdraw()
            self.addCleanup(window.destroy)
            studio = Studio(window, store)
            self.addCleanup(lambda: window.after_cancel(studio.timer))
            studio.go("auto_video")
            labels = {w.cget("text"): w for w in walk(studio.body) if isinstance(w, tk.Label)}
            for role, route, model_id in (("文案", "Codex CLI", "my-writer"),
                                          ("选材与动效", "DeepSeek Harness", "my-artist")):
                caption = labels[role]
                cell = caption.grid_info()
                control, = caption.master.grid_slaves(row=cell["row"], column=int(cell["column"])+1)
                control.invoke()
                dialog = window.grab_current()
                gateway, model = [w for w in walk(dialog) if isinstance(w, ttk.Combobox)]
                gateway.current(list(gateway.cget("values")).index(route))
                gateway.event_generate("<<ComboboxSelected>>")
                window.update()
                model.set(model_id)
                next(w for w in walk(dialog) if isinstance(w, tk.Button) and w.cget("text") == "保存").invoke()
            direction, = [w for w in walk(studio.body) if isinstance(w, tk.Text)]
            direction.insert("1.0", "用一镜到底的剪纸动画。")
            start = next(w for w in walk(studio.body) if isinstance(w, tk.Button) and w.cget("text") == "开始制作")
            with patch.object(studio, "job") as create_job:
                start.invoke()
                create_job.assert_called_once()
            job = next(p for p in store.projects() if "auto_video" in p)["auto_video"]
            self.assertEqual(job["request"]["direction"], "用一镜到底的剪纸动画。")
            self.assertEqual(job["models"]["script"], {"engine": "Codex", "model": "my-writer"})
            self.assertEqual(job["models"]["assets"], {"engine": "DSH", "model": "my-artist"})
            self.assertEqual(job["models"]["shots"], {"engine": "Codex", "model": "default-writer"})
            self.assertEqual(job["models"]["images"], {})

    def test_selected_routes_empty_library_retry_and_rendered_overlay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            previous_engine = store.prefs()["engine"]
            project = auto_video.new_job("移动的光", "1 秒", "蓝晒")
            img = BytesIO()
            Image.new("RGB", (72, 128), "#164ac7").save(img, "PNG")
            b64 = base64.b64encode(img.getvalue()).decode()
            requests = []
            replies = {
                "director": {"title": "光", "angle": "移动的光点", "seconds": 1, "style": "蓝晒"},
                "writer": "光移向远方。它停下了。",
                "story": [{"end": 1, "visual": "橙光移动", "section": "流动"},
                          {"end": 2, "visual": "停驻", "section": "静止"}],
                "artist": [{"shot": 1, "svg": SVG, "image_prompt": "蓝色幕布"},
                           {"shot": 2, "image_prompt": "蓝色纸张"}],
            }

            class Gateway(BaseHTTPRequestHandler):
                def send_json(self, body, status=200):
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(body).encode())

                def do_GET(self):
                    if self.path.endswith("/usage"):
                        self.send_json({"total_usage": 250})
                    elif self.path.endswith("/subscription"):
                        self.send_json({"hard_limit_usd": 15})
                    elif self.path == "/v1/models":
                        self.send_json({"data": [{"id": m} for m in replies]})
                    else:
                        self.send_json({}, 404)

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    model = body["model"]
                    requests.append((self.path, model, self.headers.get("Authorization")))
                    if self.path == "/v1/images/generations":
                        attempts = sum(p.endswith("/generations") for p, _, _ in requests)
                        if attempts == 2:
                            self.send_json({}, 503)
                        else:
                            self.send_json({"data": [{"b64_json": b64}]})
                    elif self.path == "/v1/chat/completions":
                        answer = replies[model]
                        content = answer if isinstance(answer, str) else json.dumps(answer)
                        self.send_json({"choices": [{"message": {"content": content}}]})
                    else:
                        self.send_json({}, 404)

                def log_message(self, *_):
                    pass

            with HTTPServer(("127.0.0.1", 0), Gateway) as server:
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
                worker.start()
                try:
                    base = providers.one_api_base(f"http://127.0.0.1:{server.server_port}")
                    self.assertEqual(providers.one_api_quota(base, "test"), (15, 2.5))
                    self.assertEqual(providers.api_models(base, "test"), tuple(replies))
                    studio = Studio.__new__(Studio)
                    studio.store = store
                    profiles = {}
                    for role, model in zip(("plan", "script", "shots", "assets", "images"), (*replies, "image-maker")):
                        address = base + "/images/generations" if role == "images" else base
                        pid = store.save_api_profile({"base": address, "model": model}, "test-"+role, activate=False)
                        profiles[role] = {"engine": "API", "api_profile": pid, "model": model}
                    studio.prefs = store.prefs()
                    self.assertEqual(studio.prefs["engine"], previous_engine)
                    project["auto_video"]["models"] = profiles
                    store.save(project)
                    with self.assertRaisesRegex(ValueError, "AI 生图未完成"):
                        auto_video.run(store, project, studio.engine, "", lambda _: None)
                    saved, = store.projects()
                    self.assertEqual(saved["auto_video"]["next_stage"], "images")
                    self.assertEqual(saved["auto_video"]["models"], profiles)
                    self.assertNotIn("test-images", json.dumps(saved))
                    self.assertEqual([s["title"] for s in saved["sections"]], ["流动", "静止"])
                    self.assertEqual(saved["shots"][0]["art"]["layer"], "effect")
                    self.assertTrue(saved["shots"][0]["art"]["image_file"])
                    self.assertEqual(len(list((root / "原创素材").rglob("*.png"))), 1)
                    voice = root / "voice.wav"
                    production.ffmpeg("-f", "lavfi", "-i", "sine=duration=0.52:sample_rate=32000", voice)
                    with patch("production.speech_files", return_value=[voice, voice]):
                        auto_video.run(store, saved, studio.engine, "", lambda _: None)
                        record = review.create_preview(root, saved, {}, lambda _: None)
                finally:
                    server.shutdown()
                    worker.join()
            self.assertEqual([m for _, m, _ in requests], [*replies, "image-maker", "image-maker", "image-maker"])
            self.assertEqual([a for _, _, a in requests], ["Bearer test-"+role for role in
                             ("plan", "script", "shots", "assets", "images", "images", "images")])
            self.assertEqual(saved["auto_video"]["next_stage"], "done")
            video = saved["auto_video"]["video_path"]
            def pixel(t, x):
                return production.ffmpeg("-ss", t, "-i", video, "-vf", f"crop=4:4:{x}:380,scale=1:1",
                                         "-frames:v", 1, "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
            first, last = pixel(0, 65), pixel(.44, 570)
            for rgb in (first, last):
                self.assertGreater(rgb[0], rgb[2] + 100)
            blue = pixel(.44, 65)
            self.assertGreater(blue[2], blue[0] + 100)
            bundle = review.version_dir(root, saved["id"], record["version"])
            manifest = json.loads((bundle / review.TIMELINE).read_text(encoding="utf-8"))
            pictures = manifest["shots"][0]["pictures"]
            self.assertEqual([p["layer"] for p in pictures], ["main", "effect"])
            self.assertTrue(all((bundle / p["file"]).is_file() for p in pictures))

    def test_text_only_director_can_draw_without_files_or_an_image_model(self):
        project = auto_video.new_job("", "", "")
        project.update(approved="一束光。", shots=[{"start": 0, "end": 1, "kind": "voice", "visual": "移动"}])
        replies = [json.dumps([{"shot": 1, "main": "不存在的文件"}]), "```svg\n" + SVG + "\n```"]
        with patch("providers.generate", side_effect=replies) as generate:
            (selected, designs), = planning.visual_batches({}, "", project, {}, creative=True)
            self.assertEqual(selected, [])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(designs[0], {"svg": SVG, "layer": "main"})
        art = Artwork(SVG)
        start = Image.open(BytesIO(art.frame((72, 128), 0, {}))).convert("RGBA")
        end = Image.open(BytesIO(art.frame((72, 128), 1, {}))).convert("RGBA")
        self.assertGreater(start.getpixel((4, 36))[3], 240)
        self.assertEqual(end.getpixel((4, 36))[3], 0)
        self.assertGreater(end.getpixel((60, 36))[3], 240)
        for invalid in ('<image href="file:///C:/unused"/>', '<script/>'):
            with self.assertRaises(ValueError):
                Artwork(SVG.replace("</svg>", invalid + "</svg>"))

    def test_resume_keeps_artwork_from_completed_shots(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            project = auto_video.new_job("流光", "", "")
            project["auto_video"].update(next_stage="assets", plan={"style": "剪纸", "angle": "流动"})
            project.update(approved="出发。回家。", shots=[
                {"start": n, "end": n+1, "kind": "voice", "visual": "光点"} for n in range(2)])
            engine = lambda page: ({}, "")
            with patch("providers.generate", side_effect=[SVG, ValueError("服务暂时离线")]):
                with self.assertRaisesRegex(ValueError, "服务暂时离线"):
                    auto_video.run(store, project, engine, "", lambda _: None)
            saved, = store.projects()
            self.assertEqual(saved["auto_video"]["next_stage"], "assets")
            self.assertEqual(saved["shots"][0]["art"]["svg"], SVG)
            self.assertNotIn("art", saved["shots"][1])
            with (patch("providers.generate", return_value='<?xml version="1.0"?>' + SVG) as generate,
                  patch("production.render", side_effect=ValueError("到达渲染"))):
                with self.assertRaisesRegex(ValueError, "到达渲染"):
                    auto_video.run(store, saved, engine, "", lambda _: None)
            generate.assert_called_once()
            self.assertEqual([s["shot"] for s in generate.call_args.args[-1]["镜头"]], [2])

    def test_gemini_reads_the_image_part_and_origin_is_part_of_the_cache_key(self):
        output = BytesIO()
        Image.new("RGBA", (4, 4), "navy").save(output, "PNG")
        reply = {"candidates": [{"content": {"parts": [{"text": "绘制完成"}, {"inlineData": {
            "mimeType": "image/png", "data": base64.b64encode(output.getvalue()).decode()}}]}}]}
        settings = {"engine": "API", "base": "https://example.org/v1beta", "model": "models/painter",
                    "protocol": "Google Gemini", "api_profile": "profile"}
        project = auto_video.new_job("", "", "")
        with tempfile.TemporaryDirectory() as folder, patch("providers.json_request", return_value=reply) as call:
            first = generated_images.create(folder, project, "夜色", settings, "test")
            self.assertEqual(first, generated_images.create(folder, project, "夜色", settings, "test"))
            self.assertEqual(call.call_count, 1)
            url, body = call.call_args.args
            self.assertEqual(url, "https://example.org/v1beta/models/painter:generateContent")
            self.assertIn("IMAGE", body["generationConfig"]["responseModalities"])
            self.assertNotEqual(first, generated_images.create(folder, project, "夜色",
                                {**settings, "base": "https://other.example/v1beta"}, "test"))

    def test_curve_motion_follows_its_own_path_and_easing(self):
        art = Artwork("""<svg viewBox="0 0 100 100"><circle r="4" fill="red">
        <animateMotion path="M10 80 Q50 0 90 80" dur="1s" fill="freeze" rotate="auto"
        calcMode="spline" keyTimes="0;1" keySplines="0.42 0 0.58 1"/>
        </circle></svg>""")
        for t, position in ((0, (10, 80)), (.5, (50, 40)), (1, (90, 80)), (.5, (50, 40))):
            im = Image.open(BytesIO(art.frame((100, 100), t, {}))).convert("RGBA")
            self.assertEqual(im.getpixel(position), (255, 0, 0, 255))
