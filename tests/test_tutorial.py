import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app_paths
import studio as studio_module
from store import Store
from studio import TUTORIAL_ENVIRONMENT, TUTORIAL_STEPS, Studio


class TutorialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        environment = patch.dict(os.environ, {"FENJING_HOME": str(folder / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        config = patch("app_paths.config_dir", return_value=folder / "config")
        config.start()
        self.addCleanup(config.stop)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.root.destroy)
        self.root.withdraw()
        self.editor = Studio(self.root, Store())
        self.addCleanup(lambda: self.root.after_cancel(self.editor.timer))

    def widgets(self, parent, kind=tk.Widget):
        found = []
        for child in parent.winfo_children():
            if isinstance(child, kind):
                found.append(child)
            found.extend(self.widgets(child, kind))
        return found

    def captions(self, parent):
        shown = [widget.cget("text") for widget in self.widgets(parent, tk.Label)]
        shown += [widget.cget("text") for widget in self.widgets(parent, tk.Button)]
        return shown

    def test_welcome_shows_the_tutorial_before_the_notice(self):
        order = []
        self.editor.show_tutorial = lambda on_close=None: (order.append("tutorial"),
                                                           on_close and on_close())
        self.editor.show_notice = lambda: order.append("notice")
        self.editor.show_welcome()
        self.assertEqual(order, ["tutorial", "notice"])

    def test_welcome_skips_a_dismissed_tutorial(self):
        order = []
        self.editor.show_tutorial = lambda on_close=None: order.append("tutorial")
        self.editor.show_notice = lambda: order.append("notice")
        self.editor.store.set_pref("tutorial", "off")
        self.editor.prefs = self.editor.store.prefs()
        self.editor.show_welcome()
        self.assertEqual(order, ["notice"])

    def test_tutorial_lists_every_step_and_remembers_dismissal(self):
        noticed = []
        self.editor.show_notice = lambda: noticed.append("notice")
        win = self.editor.show_tutorial(self.editor.show_notice)
        self.root.update_idletasks()
        shown = self.captions(win)
        self.assertLess(shown.index("运行环境"), shown.index(TUTORIAL_ENVIRONMENT))
        self.assertLess(shown.index(TUTORIAL_ENVIRONMENT), shown.index(TUTORIAL_STEPS[0][0]))
        for title, note in TUTORIAL_STEPS:
            with self.subTest(step=title):
                self.assertIn(title, shown)
                self.assertIn(note, shown)
        self.assertIn("复制软件位置", shown)

        dismiss = self.widgets(win, tk.Checkbutton)[0]
        dismiss.invoke()
        self.root.tk.call(win.protocol("WM_DELETE_WINDOW"))
        self.assertEqual(Store().prefs()["tutorial"], "off")
        # 销毁与「使用须知」都排在空闲时，跑一轮布局就会轮到最后一步
        self.root.update_idletasks()
        self.assertEqual(noticed, ["notice"])

    def test_settings_help_page_can_reopen_and_turn_the_tutorial_off(self):
        self.editor.go("settings")
        self.editor.show_settings("help")
        self.root.update_idletasks()
        page = self.captions(self.editor.body)
        self.assertIn("新手教程", page)
        self.assertIn("重新打开新手教程", page)
        self.assertTrue(self.editor.tutorial_startup.get())
        self.editor.tutorial_startup.set(False)
        self.editor.remember_tutorial()
        self.assertEqual(Store().prefs()["tutorial"], "off")
        self.editor.tutorial_startup.set(True)
        self.editor.remember_tutorial()
        self.assertEqual(Store().prefs()["tutorial"], "show")

    def test_copy_program_location_hands_an_agent_every_path(self):
        self.editor.copy_program_location()
        copied = self.root.clipboard_get()
        self.assertIn("程序", copied)
        self.assertIn(str(Path(studio_module.__file__).resolve()), copied)
        self.assertIn(str(app_paths.config_dir()), copied)
        self.assertIn(str(Store().root / "workspace.sqlite3"), copied)


if __name__ == "__main__":
    unittest.main()
