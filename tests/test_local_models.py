import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import local_models


# Execute the test CLI in a separate process on all three operating systems.
TEST_CLI = """
import hashlib, json, os, sys
from pathlib import Path
assert os.environ["OLLAMA_HOST"] == "http://127.0.0.1:11434"
db = Path(os.environ["VAT_TEST_MODELS"])
models = json.loads(db.read_text())
command, *args = sys.argv[1:]
if command == "list":
    print("NAME ID SIZE MODIFIED")
    for name in models:
        print(name, "test 1KB now")
elif command == "show":
    assert args[0] == "--modelfile"
    print("FROM /models/blobs/sha256-" + models[args[1]])
elif command == "create":
    name, option, modelfile = args
    assert option == "-f" and name not in models
    assert Path(modelfile).read_text().strip() == "FROM ./model.gguf"
    models[name] = hashlib.sha256(Path("model.gguf").read_bytes()).hexdigest()
    db.write_text(json.dumps(models))
else:
    raise AssertionError(command)
"""


class LocalModelTests(unittest.TestCase):
    def test_cli_honors_path_reuses_weights_and_preserves_collision(self):
        with tempfile.TemporaryDirectory(prefix="VAT 模型 ") as folder:
            root = Path(folder)
            source = root / "中文 权重 [一] #.gguf"
            source.write_bytes(b"GGUFtest-data")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            reserved = "vat-" + digest[:24] + ":latest"
            registry = root / "models.json"
            registry.write_text(json.dumps({reserved: "a" * 64}))
            script = root / "test cli.py"
            script.write_text(TEST_CLI, encoding="utf-8")
            run = subprocess.run
            def invoke(argv, **kwargs):
                return run([argv[0], script, *argv[1:]], **kwargs)
            with patch.dict(os.environ, {"OLLAMA_HOST": "http://invalid.example", "VAT_TEST_MODELS": str(registry)}):
                with patch("local_models.subprocess.run", side_effect=invoke) as calls:
                    name = local_models.import_gguf(source, sys.executable)
                    self.assertEqual(name, local_models.import_gguf(source, sys.executable))
                    creates = [c for c in calls.call_args_list if c.args[0][1] == "create"]
                    self.assertEqual(len(creates), 1)
                    self.assertFalse(Path(creates[0].kwargs["cwd"]).exists())
            self.assertEqual(json.loads(registry.read_text()), {reserved: "a" * 64, name: digest})
            self.assertEqual(source.read_bytes(), b"GGUFtest-data")

    def test_invalid_files_and_unavailable_service(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "bad.gguf"
            model.write_bytes(b"broken")
            with patch("local_models._run", return_value=""):
                with self.assertRaisesRegex(ValueError, "文件头"):
                    local_models.import_gguf(model, sys.executable)
            failure = subprocess.CompletedProcess([], 1, b"", b"connection refused")
            with patch("local_models.subprocess.run", return_value=failure):
                with self.assertRaisesRegex(ValueError, "127.0.0.1:11434"):
                    local_models.import_gguf(model, sys.executable)

    def test_size_reads_only_local_catalog(self):
        response = MagicMock(status=200)
        response.read.return_value = b'{"models":[{"name":"vat-test:latest","size":305}]}'
        with patch("local_models.http.client.HTTPConnection") as connect:
            conn = connect.return_value
            conn.getresponse.return_value.__enter__.return_value = response
            self.assertEqual(local_models.registered_size("vat-test:latest"), 305)
            connect.assert_called_once_with("127.0.0.1", 11434, timeout=2)
            conn.request.assert_called_once_with("GET", "/api/tags")
            response.status = 500
            self.assertIsNone(local_models.registered_size("vat-test:latest"))
