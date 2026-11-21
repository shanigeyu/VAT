import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_paths
import sovits
from api_profiles import protect_key, unprotect_key
from store import Store, new_project


class UserStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.enterContext(patch("app_paths.config_dir", return_value=self.root / "config"))
        self.enterContext(patch("app_paths.default_data_home", return_value=self.root / "default"))
        self.enterContext(patch.dict(os.environ, {"FENJING_HOME": ""}))

    def test_old_default_theme_moves_to_the_new_default_once(self):
        import sqlite3
        from contextlib import closing
        store = Store()
        self.assertEqual(store.prefs()["theme"], "aurora")
        store.set_pref("theme", "modern")
        with closing(sqlite3.connect(store.db)) as conn, conn:
            conn.execute("DELETE FROM prefs WHERE key='theme_default_v022'")
        self.assertEqual(Store().prefs()["theme"], "aurora")
        store.set_pref("theme", "modern")
        self.assertEqual(Store().prefs()["theme"], "modern")

    def test_switch_workspace_open_original_and_override(self):
        first = Store()
        first.save(new_project("Original"))
        target = self.root / "Other workspace"
        app_paths.set_workspace(str(target))
        self.assertEqual(Store().root, target)
        self.assertEqual(Store().projects(), [])
        with patch.dict(os.environ, {"FENJING_HOME": str(first.root)}):
            self.assertEqual(Store().root, first.root)
            with self.assertRaisesRegex(ValueError, "FENJING_HOME"):
                app_paths.set_workspace(str(target))
        app_paths.set_workspace("")
        self.assertEqual([p["title"] for p in Store().projects()], ["Original"])
        with self.assertRaises(ValueError):
            app_paths.set_workspace(str(Path(app_paths.__file__).parent / "my-data"))
        bad = self.root / "invalid"
        bad.mkdir()
        (bad / "workspace.sqlite3").write_bytes(b"unrelated-file")
        with self.assertRaises(ValueError):
            app_paths.set_workspace(str(bad))
        self.assertEqual(Store().root, first.root)

    def test_machine_tools_are_shared_across_workspaces_and_updated_atomically(self):
        app_paths.set_paths({"ffmpeg_dir": str(self.root)})
        app_paths.set_workspace(str(self.root / "second"))
        self.assertEqual(app_paths.get_path("ffmpeg_dir"), str(self.root))
        with self.assertRaises(ValueError):
            app_paths.set_paths({"ffmpeg_dir": "", "font_file": str(self.root)})
        self.assertEqual(app_paths.get_path("ffmpeg_dir"), str(self.root))
        app_paths.set_paths({"ffmpeg_dir": ""})
        self.assertEqual(app_paths.get_path("ffmpeg_dir"), "")

    def test_sovits_uses_a_profile_only_after_it_is_selected(self):
        reference = self.root / "voice.wav"
        reference.write_bytes(b"test audio")
        profile = self.root / "voice.json"
        profile.write_text(json.dumps({"reference": reference.name, "prompt_text": "试音"}), encoding="utf-8")
        with patch.dict(os.environ, {"TUTORIAL_SOVITS_PROFILE": str(profile)}):
            self.assertEqual(Store().prefs()["sovits_profile"], "")
            with self.assertRaisesRegex(ValueError, "选择你自己的 GPT-SoVITS 音色 JSON"):
                sovits.load_profile()
            self.assertEqual(sovits.load_profile(str(profile))["reference"], str(reference))

    @unittest.skipIf(os.name == "nt", "POSIX private key storage")
    def test_keys_round_trip_and_stay_bound_to_user_config(self):
        directory = self.root / "user-one"
        with patch("api_profiles.config_dir", return_value=directory):
            encrypted = protect_key("test-credential")
            self.assertNotIn(b"test-credential", encrypted)
            self.assertEqual(unprotect_key(encrypted), "test-credential")
            self.assertEqual((directory / "api-secret.key").stat().st_mode & 0o777, 0o600)
        with patch("api_profiles.config_dir", return_value=self.root / "user-two"):
            with self.assertRaises(ValueError):
                unprotect_key(encrypted)
            protect_key("another-credential")
            with self.assertRaises(ValueError):
                unprotect_key(encrypted)
