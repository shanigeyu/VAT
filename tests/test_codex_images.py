import gc
import json
import os
import tempfile
import tkinter as tk
import unittest
from io import BytesIO
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from PIL import Image

import codex_harness
import generated_images
import task_control
from api_profiles import ANTHROPIC
from auto_video import new_job
from store import Store
from studio import Studio


class CodexImageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = {"engine": "Codex", "codex_path": "selected-codex.exe", "codex_model": "image-agent"}
        self.project = new_job("测试画面", "", "")
        output = BytesIO()
        Image.new("RGBA", (18, 32), "navy").save(output, "PNG")
        self.png = output.getvalue()

    def fake_cli(self, path, args, cwd, prompt=None, timeout=300):
        self.assertEqual(path, "selected-codex.exe")
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(args[args.index("--enable") + 1], "image_generation")
        self.assertIn("shell_tool", [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--disable"])
        self.assertEqual(timeout, 600)
        self.assertIn("原生图片生成工具", prompt)
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(schema["required"], ["image_path"])
        picture = Path(cwd) / "native-image.png"
        picture.write_bytes(self.png)
        Path(args[args.index("-o") + 1]).write_text(json.dumps({"image_path": str(picture)}), encoding="utf-8")
        return "CLI progress is not the image"

    def test_codex_images_use_native_tool_and_are_saved_cached_and_separated_by_model(self):
        with patch("codex_harness.cli_path", side_effect=lambda saved: saved), \
                patch("codex_harness.invoke", side_effect=self.fake_cli) as invoke, \
                patch("providers.json_request", side_effect=AssertionError("Codex 生图不应调用 API")):
            first = generated_images.create(self.root, self.project, "夜色", self.settings, "unused-api-key")
            self.assertEqual(first, generated_images.create(self.root, self.project, "夜色", self.settings, ""))
            self.assertEqual(invoke.call_count, 1)
            self.assertIn("画幅：竖屏 9∶16", invoke.call_args.args[3])
            with Image.open(generated_images.file_path(self.root, self.project["id"], first)) as picture:
                self.assertEqual((picture.size, picture.mode), ((18, 32), "RGBA"))
            second = generated_images.create(self.root, self.project, "夜色",
                                              {**self.settings, "codex_model": "other-agent"}, "")
            self.assertNotEqual(first, second)
            self.assertEqual(invoke.call_count, 2)
            self.assertEqual(invoke.call_args.args[1][invoke.call_args.args[1].index("--model") + 1], "other-agent")

    def test_native_artifact_in_codex_home_is_copied_into_the_project(self):
        codex_home = self.root / "codex"

        def cli(path, args, cwd, prompt=None, timeout=300):
            picture = codex_home / "generated_images/session/native.png"
            picture.parent.mkdir(parents=True)
            picture.write_bytes(self.png)
            Path(args[args.index("-o") + 1]).write_text(json.dumps({"image_path": str(picture)}), encoding="utf-8")

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
                patch("codex_harness.cli_path", return_value="selected-codex.exe"), \
                patch("codex_harness.invoke", side_effect=cli):
            filename = generated_images.create(self.root, self.project, "咖啡杯", self.settings, "")
        self.assertTrue(generated_images.file_path(self.root, self.project["id"], filename).is_file())
        self.assertTrue((codex_home / "generated_images/session/native.png").is_file())

    def test_native_tool_cannot_return_an_unrelated_local_file(self):
        private = self.root / "unrelated.png"
        private.write_bytes(self.png)

        def cli(path, args, cwd, prompt=None, timeout=300):
            Path(args[args.index("-o") + 1]).write_text(json.dumps({"image_path": str(private)}), encoding="utf-8")

        with patch("codex_harness.invoke", side_effect=cli):
            with self.assertRaisesRegex(ValueError, "本次输出目录"):
                codex_harness.generate_image("selected-codex.exe", "新画面")
        self.assertEqual(private.read_bytes(), self.png)

    def test_old_images_and_empty_generation_results_are_rejected(self):
        def old_image(path, args, cwd, prompt=None, timeout=300):
            self.fake_cli(path, args, cwd, prompt, timeout)
            os.utime(Path(cwd) / "native-image.png", (1, 1))

        with patch("codex_harness.invoke", side_effect=old_image):
            with self.assertRaisesRegex(ValueError, "此前已有的图片"):
                codex_harness.generate_image("selected-codex.exe", "新画面")
        with patch("codex_harness.invoke", return_value="text without an image"):
            with self.assertRaisesRegex(ValueError, "未返回生成的图片"):
                codex_harness.generate_image("selected-codex.exe", "新画面")

    def test_cancelled_generation_does_not_publish_an_image(self):
        control = task_control.JobControl()

        def cli(path, args, cwd, prompt=None, timeout=300):
            self.fake_cli(path, args, cwd, prompt, timeout)
            control.cancel()

        with task_control.running(control), patch("codex_harness.cli_path", return_value="selected-codex.exe"), \
                patch("codex_harness.invoke", side_effect=cli):
            with self.assertRaises(task_control.JobCancelled):
                generated_images.create(self.root, self.project, "夜色", self.settings, "")
        self.assertEqual(list((self.root / "原创素材").rglob("*.png")), [])

    @staticmethod
    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from CodexImageTests.widgets(child)

    def test_image_selector_offers_codex_and_pins_its_model_with_an_unrelated_global_api(self):
        store = Store(self.root)
        store.save_api_profile({"base": "https://text.example/v1", "protocol": ANTHROPIC, "model": "writer"}, "")
        try:
            window = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(gc.collect)
        self.addCleanup(window.destroy)
        window.withdraw()
        studio = Studio(window, store)
        self.addCleanup(lambda: window.after_cancel(studio.timer))
        studio.go("auto_video")
        caption = next(w for w in self.widgets(studio.body)
                       if isinstance(w, tk.Label) and w.cget("text") == "AI 生图" and w.grid_info())
        cell = caption.grid_info()
        control, = caption.master.grid_slaves(row=cell["row"], column=int(cell["column"]) + 1)
        control.invoke()
        dialog = window.grab_current()
        route, model = [w for w in self.widgets(dialog) if isinstance(w, ttk.Combobox)]
        choices = list(route.cget("values"))
        self.assertIn("Codex CLI", choices)
        self.assertNotIn("DeepSeek Harness", choices)
        route.current(choices.index("Codex CLI"))
        route.event_generate("<<ComboboxSelected>>")
        model.set("image-agent")
        next(w for w in self.widgets(dialog) if isinstance(w, tk.Button) and w.cget("text") == "保存").invoke()
        self.assertEqual(Store(self.root).prefs()["auto_model.images"], {"engine": "Codex", "model": "image-agent"})
        self.assertIn("Codex CLI", control.cget("text"))
        start = next(w for w in self.widgets(studio.body) if isinstance(w, tk.Button) and w.cget("text") == "开始制作")
        with patch.object(studio, "job") as job, patch.object(store, "profile_key", return_value=""):
            start.invoke()
            job.assert_called_once()
        project = next(project for project in store.projects() if "auto_video" in project)
        self.assertEqual(project["auto_video"]["models"]["images"], {"engine": "Codex", "model": "image-agent"})


if __name__ == "__main__":
    unittest.main()
