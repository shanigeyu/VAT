import gc
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auto_video
import production
import providers
import render_progress
import task_control
from store import Store, new_project, shots_from_plan
from studio import Studio


PLAN = {"title": "保存步骤", "angle": "完成后继续", "seconds": 2, "style": "简约图解",
        "palette": "简约图解", "estimate": [30, 60]}
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 160"><rect width="96" height="160" fill="red"/></svg>'


class JobControlTests(unittest.TestCase):
    def test_cancel_during_network_wait_discards_the_late_result(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        control, outcomes = task_control.JobControl(), []

        def slow_request():
            started.set()
            release.wait(5)
            finished.set()
            return {"late": "response"}

        def work():
            try:
                with task_control.running(control):
                    outcomes.append(task_control.network_call(slow_request))
            except task_control.JobCancelled:
                outcomes.append("cancelled")

        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            control.cancel()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, ["cancelled"])
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(finished.wait(2))
        self.assertEqual(outcomes, ["cancelled"])

    def test_cancel_stops_the_cli_and_its_child_process(self):
        with tempfile.TemporaryDirectory() as folder:
            ready, leaked = Path(folder) / "ready", Path(folder) / "leaked"
            child = ("from pathlib import Path; import time; "
                     f"Path({str(ready)!r}).touch(); time.sleep(1.5); Path({str(leaked)!r}).touch()")
            parent = ("import subprocess, sys, time; "
                      f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)")
            control, outcomes = task_control.JobControl(), []

            def work():
                try:
                    with task_control.running(control):
                        task_control.run_process([sys.executable, "-c", parent], capture_output=True, timeout=10,
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except task_control.JobCancelled:
                    outcomes.append("cancelled")

            worker = threading.Thread(target=work)
            worker.start()
            deadline = time.monotonic() + 4
            try:
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
            finally:
                control.cancel()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, ["cancelled"])
            time.sleep(1.7)
            self.assertFalse(leaked.exists(), "取消后不应有 CLI 子进程继续制作")

    def test_completed_script_is_saved_before_cancellation_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            project = auto_video.new_job("保存步骤", "2秒", "")
            store.save(project)
            control = task_control.JobControl()

            def write(*_args, **_kwargs):
                control.cancel()
                return "第一步已完成。"

            with (patch("auto_video.make_plan", return_value=PLAN),
                  patch("providers.rewrite", side_effect=write),
                  patch("providers.storyboard") as storyboard,
                  task_control.running(control)):
                with self.assertRaises(task_control.JobCancelled):
                    auto_video.run(store, project, lambda *_: ({}, ""), "", lambda _: None)
            storyboard.assert_not_called()
            saved, = Store(folder).projects()
            self.assertEqual(saved["approved"], "第一步已完成。")
            self.assertEqual(saved["auto_video"]["next_stage"], "shots")
            self.assertEqual(saved["auto_video"]["status"], "paused")
            self.assertEqual(saved["auto_video"]["plan"], PLAN)
            shots = shots_from_plan(["第一步已完成。"], [{"end": 1, "kind": "voice", "visual": "已完成的第一步"}])
            video = Path(folder) / "final.mp4"
            with (patch("providers.rewrite") as rewrite, patch("auto_video.make_plan") as planner,
                  patch("providers.storyboard", return_value=shots),
                  patch("planning.visual_batches", return_value=iter([([], {0: {"svg": SVG}})])),
                  patch("production.tool"), patch("production.render", return_value=[("完整成片", str(video))]),
                  patch("production.probe", return_value={"streams": [{"codec_type": "video", "duration": "2"}]})):
                auto_video.run(Store(folder), saved, lambda *_: ({}, ""), "", lambda _: None)
            rewrite.assert_not_called()
            planner.assert_not_called()
            self.assertEqual(Store(folder).projects()[0]["auto_video"]["status"], "done")

    def test_stopped_render_reuses_completed_shots_but_never_partial_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / "voice.wav"
            production.ffmpeg("-f", "lavfi", "-i", "sine=duration=0.24:sample_rate=48000", voice)
            project = new_project("中断再继续")
            units = ["第一步。", "第二步。"]
            project.update(draft="".join(units), approved="".join(units))
            project["shots"] = shots_from_plan(units, [{"end": 1, "kind": "voice", "visual": "红色"},
                                                     {"end": 2, "kind": "voice", "visual": "蓝色"}])
            project["captions"]["mode"] = production.CAPTION_MODES[0]
            for shot in project["shots"]:
                shot.update(voice_source="record_file", voice_file=str(voice), art={"svg": SVG})
            control, calls = task_control.JobControl(), []
            original_run = production.run

            def record(args, **kwargs):
                calls.append(Path(str(args[-1])).name)
                return original_run(args, **kwargs)

            def stop_at_second(message):
                if message.startswith("正在制作镜头 2"):
                    control.cancel()

            with patch("production.picture_size", return_value=(96, 160)), patch("production.run", side_effect=record):
                with task_control.running(control), self.assertRaises(task_control.JobCancelled):
                    production.render(root, project, {}, True, stop_at_second)
                checkpoints = list((root / "制作进度").rglob("completed.json"))
                self.assertEqual(len(checkpoints), 1)
                self.assertIn("v1.mp4", json.loads(checkpoints[0].read_text(encoding="utf-8")))
                exports = dict(production.render(root, json.loads(json.dumps(project)), {}, True, lambda _: None))
            self.assertEqual(calls.count("v1.mp4"), 1)
            self.assertEqual(calls.count("v2.mp4"), 1)
            self.assertTrue(Path(exports["完整成片"]).is_file())
            self.assertTrue(Path(exports["字幕"]).is_file())
            self.assertAlmostEqual(production.media_seconds(Path(exports["完整成片"]),
                                   production.probe(exports["完整成片"]), "video"), .48, delta=.05)

            partial = root / "checkpoints"
            cache = render_progress.RenderProgress(partial)
            token = render_progress._current.set(cache)
            try:
                target = partial / "partial.mp4"
                with self.assertRaises(task_control.JobCancelled), render_progress.output(target, "same-input") as ready:
                    self.assertFalse(ready)
                    target.write_bytes(b"truncated")
                    raise task_control.JobCancelled()
                self.assertFalse(target.exists())
                with render_progress.output(target, "same-input") as ready:
                    self.assertFalse(ready)
                    target.write_bytes(b"finished")
                with render_progress.output(target, "changed-input") as ready:
                    self.assertFalse(ready)
                    target.write_bytes(b"new-result")
            finally:
                render_progress._current.reset(token)

    def test_unversioned_local_voice_keeps_finished_sentences_after_a_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root, control = Path(folder), task_control.JobControl()
            voice = root / "source.wav"
            production.ffmpeg("-f", "lavfi", "-i", "sine=duration=0.24:sample_rate=48000", voice)
            project = new_project("逐句继续")
            project.update(draft="第一句。第二句。", approved="第一句。第二句。")
            profile = {"model_id": "", "voice": "local-unversioned"}
            synthesized = []

            def synthesize(text, path, **_kwargs):
                synthesized.append(text)
                shutil.copyfile(voice, path)
                if len(synthesized) == 1:
                    control.cancel()

            with (patch("sovits.load_profile", return_value=profile), patch("sovits.ensure_service"),
                  patch("sovits.synthesize", side_effect=synthesize)):
                with task_control.running(control), self.assertRaises(task_control.JobCancelled):
                    production.export_voice(root, project, lambda _: None, voice_profile="local.json")
                exports = dict(production.export_voice(root, project, lambda _: None, voice_profile="local.json"))
            self.assertEqual(synthesized, ["第一句。", "第二句。"])
            self.assertAlmostEqual(production.wav_seconds(Path(exports["定稿配音"])), .48, delta=.01)

    def test_stop_after_export_before_metadata_does_not_render_the_video_again(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            project = auto_video.new_job("成片已完成", "", "")
            project["auto_video"].update(plan=PLAN, next_stage="render")
            store.save(project)
            video = Path(folder) / "video.mp4"
            video.write_bytes(b"completed-video")
            control = task_control.JobControl()

            def stop_probe(_path):
                control.cancel()
                task_control.checkpoint()

            with (patch("production.tool"),
                  patch("production.render", return_value=[("完整成片", str(video))]) as render):
                with patch("production.probe", side_effect=stop_probe), task_control.running(control):
                    with self.assertRaises(task_control.JobCancelled):
                        auto_video.run(store, project, Mock(), "", lambda _: None)
                saved, = Store(folder).projects()
                self.assertEqual(saved["auto_video"]["status"], "paused")
                self.assertTrue(saved["auto_video"]["render_outputs"])
                with patch("production.probe", return_value={"streams": [{"codec_type": "video", "duration": "2"}]}):
                    auto_video.run(Store(folder), saved, Mock(), "", lambda _: None)
            render.assert_called_once()
            self.assertEqual(Store(folder).projects()[0]["exports"], [{"kind": "完整成片", "path": str(video)}])

    def test_cancel_button_and_app_close_save_without_an_error_dialog(self):
        for close_app in (False, True):
            with self.subTest(close_app=close_app), tempfile.TemporaryDirectory() as folder:
                try:
                    root = tk.Tk()
                except tk.TclError as error:
                    self.skipTest(str(error))
                root.withdraw()
                studio = Studio(root, Store(folder))
                studio.p.update(draft="保留手工文案。", approved="保留手工文案。")
                started = threading.Event()
                cancelled, done = Mock(), Mock()

                def work(report):
                    started.set()
                    task_control.sleep(10)

                def walk(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from walk(child)

                try:
                    with patch("studio.messagebox.showerror") as error:
                        studio.job("测试中断", work, done, on_cancel=cancelled)
                        self.assertTrue(started.wait(2))
                        if close_app:
                            studio.close()
                        else:
                            next(w for w in walk(studio.active_job)
                                 if isinstance(w, tk.Button) and w.cget("text") == "中断并保存").invoke()
                        deadline = time.monotonic() + 3
                        while studio.active_job is not None and time.monotonic() < deadline:
                            root.update()
                            time.sleep(.01)
                        self.assertIsNone(studio.active_job)
                        cancelled.assert_called_once()
                        done.assert_not_called()
                        error.assert_not_called()
                    self.assertEqual(Store(folder).projects()[0]["approved"], "保留手工文案。")
                finally:
                    try:
                        root.after_cancel(studio.timer)
                        root.destroy()
                    except tk.TclError:
                        pass
                    gc.collect()
