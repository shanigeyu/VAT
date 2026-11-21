import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import providers
import codex_harness
from api_profiles import ANTHROPIC, GEMINI, OPENAI, RESPONSES, parse_profile
from step_models import PREF_PREFIX, engine_name
from store import Store
from studio import Studio


PROFILE = """
- id: profile-switch
  disabled: !!js "!ctx.get('profileContext')"
- id: agent-default-model
  config:
    provider: deepseek-official
    model: default-model
    reasoningEffort: low
- id: llm-deepseek
  name: '@deepseek-ai/dsh-llm-deepseek-api-key'
  config:
    models:
      - id: default-model
      - id: extra-model
"""


class ModelSettingsTests(unittest.TestCase):
    def test_fresh_workspace_waits_for_a_user_selected_model(self):
        with TemporaryDirectory() as folder:
            studio = Studio.__new__(Studio)
            studio.store = Store(folder)
            studio.prefs = studio.store.prefs()
            self.assertEqual(studio.store.api_profiles(), [])
            self.assertEqual((studio.prefs["engine"], studio.prefs["base"], studio.prefs["model"]), ("", "", ""))
            self.assertEqual(engine_name(studio.prefs), "未配置模型")
            with self.assertRaisesRegex(ValueError, "配置自己的模型"):
                studio.engine("script")

            profile_id = studio.store.save_api_profile(
                {"base": "http://localhost:1234/v1", "model": "my-model"}, "", activate=False)
            studio.prefs = Store(folder).prefs()
            self.assertEqual(studio.prefs["engine"], "")
            config, key = studio.engine("script", selection={"engine": "API", "api_profile": profile_id})
            self.assertEqual((config["model"], key), ("my-model", ""))

    def test_deepseek_requests_only_use_the_supplied_key(self):
        address = "https://api.deepseek.com"
        reply = {"data": [{"id": "example"}]}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unselected-key"}), \
                patch("providers.json_request", return_value=reply) as request:
            self.assertEqual(providers.api_models(address, ""), ("example",))
            request.assert_called_once_with(address + "/models", key="", timeout=15)
            self.assertEqual(providers.api_models(address, "chosen-key"), ("example",))
            self.assertEqual(request.call_args.kwargs["key"], "chosen-key")

    def test_workflow_steps_inherit_or_override_the_global_harness_independently(self):
        with TemporaryDirectory() as folder:
            store = Store(folder)
            store.set_prefs({"engine": "Codex", "codex_model": "global-codex", "harness_model": "configured-dsh",
                             PREF_PREFIX + "script": {"engine": "DSH", "model": "script-dsh"},
                             PREF_PREFIX + "shots": {"engine": "Codex", "model": ""},
                             PREF_PREFIX + "audio": {"engine": "DSH"}})
            studio = Studio.__new__(Studio)
            studio.store = Store(folder)
            studio.prefs = studio.store.prefs()
            for page, expected_engine, model_field, expected_model in (
                ("topics", "Codex", "codex_model", "global-codex"),
                ("script", "DSH", "harness_model", "script-dsh"),
                ("shots", "Codex", "codex_model", ""),
                ("audio", "DSH", "harness_model", "configured-dsh"),
            ):
                with self.subTest(page=page):
                    config, key = studio.engine(page)
                    self.assertEqual((config["engine"], config[model_field], key), (expected_engine, expected_model, ""))

    def test_workflow_api_uses_the_selected_profile_and_its_own_key(self):
        with TemporaryDirectory() as folder:
            store = Store(folder)
            profile = {"name": "Step API", "base": "https://shared.example/v1", "model": "profile-model",
                       "protocol": ANTHROPIC}
            step_id = store.save_api_profile(profile, "")
            default_id = store.save_api_profile({**profile, "name": "Global API", "model": "global-model"}, "")
            store.set_prefs({PREF_PREFIX + "script": {"engine": "API", "api_profile": step_id, "model": "step-model"},
                             PREF_PREFIX + "shots": {"engine": "API", "api_profile": "missing"}})
            studio = Studio.__new__(Studio)
            studio.store, studio.prefs = store, store.prefs()

            credentials = {step_id: "step-key", default_id: "global-key"}
            with patch.object(store, "profile_key", side_effect=lambda profile_id, *_: credentials[profile_id]) as key:
                config, credential = studio.engine("script")
                key.assert_called_once_with(step_id, profile["base"], ANTHROPIC)
                self.assertEqual((config["api_profile"], config["model"], credential), (step_id, "step-model", "step-key"))
                config, credential = studio.engine("audio")
                self.assertEqual((config["api_profile"], config["model"], credential), (default_id, "global-model", "global-key"))
                with self.assertRaises(ValueError):
                    studio.engine("shots")
                self.assertEqual(key.call_count, 2)

    def test_codex_probe_and_production_use_selected_model_and_final_message(self):
        def fake_cli(path, args, cwd, prompt=None, timeout=300):
            self.assertEqual(path, 'codex.exe')
            self.assertEqual(args[args.index('--model') + 1], 'vat-model')
            self.assertEqual(args[-1], '-')
            self.assertIn('输入资料', prompt)
            Path(args[args.index('-o') + 1]).write_text('编导回复', encoding='utf-8')
            return 'Codex CLI progress log'

        prefs = {'engine': 'Codex', 'codex_model': 'vat-model'}
        with patch('codex_harness.cli_path', return_value='codex.exe'), patch('codex_harness.invoke', side_effect=fake_cli):
            self.assertEqual(providers.probe_model(prefs, ''), '编导回复')
            self.assertEqual(providers.generate(prefs, '', '写一段台词', {'选题': '台历'}), '编导回复')

    def test_codex_model_picker_filters_hidden_entries(self):
        catalog = {'models': [{'slug': 'visible', 'visibility': 'list'}, {'slug': 'internal', 'visibility': 'hide'}]}
        with patch('codex_harness.invoke', return_value=json.dumps(catalog)):
            self.assertEqual(codex_harness.models('codex.exe'), ('visible',))

    def test_paste_chinese_fields_with_markdown_and_model_notes(self):
        text = "\\\n".join(("名称\t示例服务", "Base URL 协议\tOpenAI 兼容",
                            r"Base URL  https\://api.example.org/v1", "API 密钥  example-key",
                            "模型  先填 example-chat。这是本机 示例服务 AI 模型目录里可用的模型。",
                            "试用的模型  可以留空。手动检测时填 example-chat。",
                            "图标、高级、更多端点  保持默认。"))
        profile, key = parse_profile(text)
        self.assertEqual(profile, {"name": "示例服务", "protocol": OPENAI, "base": "https://api.example.org/v1",
                                   "model": "example-chat", "test_model": ""})
        self.assertEqual(key, "example-key")

    def test_json_import_and_missing_or_invalid_fields(self):
        raw = {"name": "Local", "baseURL": "http://localhost:8765/v1/chat/completions/",
               "protocol": "openai-compatible", "model": "main", "testModel": "trial", "apiKey": "sample"}
        profile, key = parse_profile(json.dumps(raw))
        self.assertEqual((profile["base"], profile["test_model"], key), ("http://localhost:8765/v1", "trial", "sample"))
        for change in ({"baseURL": "http://outside.example"}, {"protocol": "unknown"}, {"model": []},
                       {"apiKey": "first\r\nsecond"}, {"name": []}, {"kind": "provider"}):
            with self.subTest(fields=list(change)), self.assertRaises(ValueError):
                parse_profile(json.dumps({**raw, **change}))

    def test_minimal_import_infers_protocol_and_saves_an_unnamed_agent(self):
        options = (
            ("https://gateway.example/v1/chat/completions", OPENAI, "https://gateway.example/v1", ""),
            ("https://gateway.example/v1/responses", RESPONSES, "https://gateway.example/v1", ""),
            ("https://gateway.example/v1/messages", ANTHROPIC, "https://gateway.example/v1", ""),
            ("https://gateway.example/v1beta/models/m%2Dqa:generateContent", GEMINI, "https://gateway.example/v1beta", "m-qa"),
        )
        for endpoint, protocol, base, model in options:
            with self.subTest(endpoint=endpoint):
                profile, key = parse_profile(json.dumps({"endpoint": endpoint, "apiKey": "sample"}))
                self.assertEqual((profile["name"], profile["protocol"], profile["base"], profile["model"], key),
                                 ("", protocol, base, model, "sample"))
        profile, _ = parse_profile("接口地址 https://gateway.example/v1/chat/completions\nAgent ID director\n标记 Agent")
        with TemporaryDirectory() as folder:
            store = Store(folder)
            agent_id = store.save_api_profile(profile, "")
            other_id = store.save_api_profile({**profile, "kind": "model"}, "")
            store.set_pref(PREF_PREFIX + "script", {"engine": "API", "api_profile": agent_id})
            studio = Studio.__new__(Studio)
            studio.store = Store(folder)
            studio.prefs = studio.store.prefs()
            agent, _ = studio.engine("script")
            self.assertEqual((agent["api_name"], agent["model"], agent["api_kind"]), ("director", "director", "agent"))
            self.assertEqual(engine_name(agent), "Agent · director")
            self.assertEqual((studio.prefs["api_profile"], studio.prefs["api_name"], studio.prefs["api_kind"]),
                             (other_id, "director (2)", "model"))

    def test_saved_profiles_without_a_kind_migrate_in_place(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "workspace.sqlite3"
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute("CREATE TABLE prefs(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
                conn.execute("INSERT INTO prefs VALUES('api_profile', ?)", (json.dumps("legacy"),))
                conn.execute("""CREATE TABLE api_profiles(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    protocol TEXT NOT NULL, base TEXT NOT NULL, model TEXT NOT NULL,
                    test_model TEXT NOT NULL, secret BLOB NOT NULL DEFAULT X''
                )""")
                conn.execute("INSERT INTO api_profiles VALUES(?,?,?,?,?,?,?)",
                             ("legacy", "Saved", OPENAI, "https://api.example.org/v1", "saved-model", "", b""))
            store = Store(folder)
            self.assertEqual((store.prefs()["api_profile"], store.prefs()["api_kind"]), ("legacy", "model"))
            profile, = store.api_profiles()
            store.save_api_profile({**profile, "kind": "agent"}, "", profile["id"])
            self.assertEqual(Store(folder).prefs()["api_kind"], "agent")

    @unittest.skipUnless(os.name == "nt", "Uses Windows current-user DPAPI")
    def test_legacy_migration_and_two_accounts_at_one_endpoint_survive_restart(self):
        with TemporaryDirectory() as folder:
            db_path = Path(folder) / "workspace.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("CREATE TABLE prefs(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
                conn.executemany("INSERT INTO prefs VALUES(?,?)", [(k, json.dumps(v)) for k, v in
                    {"base": "https://old.example/v1", "model": "legacy-model", "engine": "DSH"}.items()])
            store = Store(folder)
            self.assertEqual((store.prefs()["model"], store.prefs()["engine"]), ("legacy-model", "DSH"))
            account = {"name": "First", "base": "https://shared.example/v1", "model": "normal"}
            first = store.save_api_profile(account, "sample-first-key")
            second = store.save_api_profile({**account, "name": "Second", "test_model": "small"}, "sample-second-key")
            reopened = Store(folder)
            self.assertEqual((reopened.prefs()["api_profile"], reopened.prefs()["test_model"]), (second, "small"))
            self.assertEqual(len(reopened.api_profiles()), 3)
            self.assertEqual(reopened.profile_key(first, account["base"], OPENAI), "sample-first-key")
            self.assertEqual(reopened.profile_key(second, account["base"], OPENAI), "sample-second-key")
            with self.assertRaises(ValueError):
                reopened.profile_key(first, "https://other.example", OPENAI)
            with self.assertRaises(ValueError):
                reopened.save_api_profile({**account, "name": "First"}, "sample-other-key", second)
            self.assertEqual(reopened.profile_key(second, account["base"], OPENAI), "sample-second-key")
            self.assertNotIn(b"sample-first-key", db_path.read_bytes())
            self.assertNotIn(b"sample-second-key", db_path.read_bytes())

    def test_trial_model_applies_only_to_manual_probe(self):
        prefs = {"engine": "API", "base": "https://api.example.org/v1", "protocol": OPENAI,
                 "model": "example-chat", "test_model": "trial"}
        answer = {"choices": [{"message": {"content": "OK"}}]}
        with patch("providers.json_request", return_value=answer) as request:
            for expected, call in (("trial", lambda: providers.probe_model(prefs, "demo")),
                       (prefs["model"], lambda: providers.generate(prefs, "demo", "write", {"x": 1})),
                       (prefs["model"], lambda: providers.probe_model({**prefs, "test_model": ""}, "demo"))):
                self.assertEqual(call(), "OK")
                url, body = request.call_args.args
                self.assertEqual((url, body["model"], request.call_args.kwargs["key"]),
                                 (prefs["base"] + "/chat/completions", expected, "demo"))

    def test_protocol_controls_endpoint_authentication_and_text_extraction(self):
        fixtures = (
            (ANTHROPIC, "/messages", {"content": [{"type": "text", "text": "OK"}]}, "x-api-key"),
            (GEMINI, "/models/demo:generateContent", {"candidates": [{"content": {"parts": [
                {"thought": True, "text": "internal"}, {"text": "OK"}]}}]}, "x-goog-api-key"),
            (RESPONSES, "/responses", {"output": [{"type": "message",
                "content": [{"type": "output_text", "text": "OK"}]}]}, None),
        )
        for protocol, suffix, result, header in fixtures:
            with self.subTest(protocol=protocol), patch("providers.json_request", return_value=result) as request:
                prefs = {"protocol": protocol, "engine": "API", "base": "https://proxy.example/v1", "model": "demo"}
                self.assertEqual(providers.probe_model(prefs, "sample"), "OK")
                url, body = request.call_args.args
                self.assertEqual(url, prefs["base"] + suffix)
                auth = request.call_args.kwargs
                self.assertEqual(auth["extra_headers"][header] if header else auth["key"], "sample")
                self.assertIn({ANTHROPIC: "messages", GEMINI: "contents", RESPONSES: "input"}[protocol], body)

    def test_api_model_list_uses_the_configured_endpoint_and_key(self):
        with patch("providers.json_request", return_value={"data": [{"id": "example"}, {"id": "example"}]}) as request:
            models = providers.api_models("http://localhost:1234/v1/chat/completions", "session-key")

        self.assertEqual(models, ("example",))
        request.assert_called_once_with("http://localhost:1234/v1/models", key="session-key", timeout=15)

    def test_dsh_model_list_reads_the_local_catalog(self):
        with patch("providers.dsh_stdout", return_value=PROFILE):
            self.assertEqual(providers.dsh_models("dsh"), ("default-model", "extra-model"))

    def test_dsh_override_preserves_provider_and_reasoning(self):
        overlays = []

        def dsh_stdout(path, args, cwd, prompt=None, timeout=180):
            if args == ["--dump-config"]:
                return PROFILE
            self.assertEqual(args, ["--patch", "vat-model.json", "-"])
            self.assertEqual(prompt, "只回复 OK")
            overlays.append(Path(cwd) / args[1])
            payload = json.loads(overlays[-1].read_text(encoding="utf-8"))
            self.assertEqual(payload, [{
                "id": "agent-default-model",
                "config": {"provider": "deepseek-official", "model": "extra-model", "reasoningEffort": "low"},
            }])
            return "OK"

        with patch("providers.dsh_stdout", side_effect=dsh_stdout):
            self.assertEqual(providers.run_dsh("dsh", "只回复 OK", "extra-model"), "OK")

        self.assertEqual(len(overlays), 1)
        self.assertFalse(overlays[0].exists())

    def test_dsh_default_keeps_the_existing_command(self):
        with patch("providers.dsh_stdout", return_value="OK") as command:
            self.assertEqual(providers.run_dsh("dsh", "只回复 OK"), "OK")

        command.assert_called_once()
        self.assertEqual(command.call_args.args[1], ["-"])
