import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop import prepare_external_tools, workspace_lock
from store import Store
from studio import PAGES, Studio


class DesktopTests(unittest.TestCase):
    def test_packaged_linux_restores_external_loader_path(self):
        with patch("desktop.sys.platform", "linux"), patch("desktop.sys.frozen", True, create=True), \
                patch.dict(os.environ, {"LD_LIBRARY_PATH": "/vat/bundled",
                                        "LD_LIBRARY_PATH_ORIG": "/user/libs"}):
            prepare_external_tools()
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/user/libs")
            del os.environ["LD_LIBRARY_PATH_ORIG"]
            prepare_external_tools()
            self.assertNotIn("LD_LIBRARY_PATH", os.environ)

    def test_packaged_finder_preserves_path_and_adds_existing_brew_binaries(self):
        with patch("desktop.sys.platform", "darwin"), patch("desktop.sys.frozen", True, create=True), \
                patch("desktop.os.pathsep", ":"), patch.dict(os.environ, {"PATH": "/custom:/usr/bin"}), \
                patch("desktop.Path.is_dir", return_value=True):
            prepare_external_tools()
            prepare_external_tools()
            self.assertEqual(os.environ["PATH"], "/custom:/usr/bin:/opt/homebrew/bin:/usr/local/bin")

    def test_second_editor_cannot_lock_the_same_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with workspace_lock(path) as first, workspace_lock(path) as second:
                self.assertTrue(first)
                self.assertFalse(second)
            with workspace_lock(path) as reopened:
                self.assertTrue(reopened)

    def test_open_every_page_without_accounts_or_user_media(self):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            with patch.dict(os.environ, {"FENJING_HOME": str(temp / "data")}), \
                    patch("app_paths.config_dir", return_value=temp / "config"):
                try:
                    root = tk.Tk()
                except tk.TclError as exc:
                    self.skipTest(str(exc))
                self.addCleanup(root.destroy)
                root.withdraw()
                editor = Studio(root, Store())
                self.addCleanup(lambda: root.after_cancel(editor.timer))
                for key in PAGES:
                    with self.subTest(page=key):
                        editor.go(key)
                        root.update_idletasks()
                        self.assertGreater(len(editor.body.winfo_children()), 0)
                for key, value in (("theme", "classic"), ("accent", "blue"), ("language", "zh_TW")):
                    editor.apply_appearance(key, value)
                    self.assertEqual(Store().prefs()[key], value)
                for section in ("models", "paths", "help", "appearance"):
                    editor.show_settings(section)
                    root.update_idletasks()
                self.assertEqual(editor.picker.get(), editor.p["title"])
