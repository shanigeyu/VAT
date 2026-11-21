import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from tempfile import TemporaryDirectory
from unittest.mock import patch

import providers
from store import Store


class LocalApiTests(unittest.TestCase):
    def test_model_list_probe_and_generate_using_reopened_keyless_profile(self):
        requests = []

        class LocalModel(BaseHTTPRequestHandler):
            def respond(self, body):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization"), None))
                self.respond({"data": [{"id": "local-model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), body))
                self.respond({"choices": [{"message": {"content": body["model"]}}]})

            def log_message(self, *_):
                pass

        with HTTPServer(("127.0.0.1", 0), LocalModel) as server, TemporaryDirectory() as folder:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}/v1"
                store = Store(folder)
                store.save_api_profile({"base": base + "/chat/completions", "model": "local-model",
                                        "kind": "agent", "test_model": "trial-model"}, "")
                reopened = Store(folder)
                prefs = reopened.prefs()
                self.assertEqual((prefs["api_name"], prefs["api_kind"], prefs["base"]), ("local-model", "agent", base))
                key = reopened.profile_key(prefs["api_profile"], prefs["base"], prefs["protocol"])
                self.assertEqual(key, "")
                with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": "",
                                             "DEEPSEEK_API_KEY": "unused-fixture"}):
                    self.assertEqual(providers.api_models(prefs["base"], key), ("local-model",))
                    self.assertEqual(providers.probe_model(prefs, key), "trial-model")
                    self.assertEqual(providers.generate(prefs, key, "编写文案", {"文本": "示例"}), "local-model")
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual([path for path, _, _ in requests],
                         ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"])
        self.assertTrue(all(auth is None for _, auth, _ in requests))
        self.assertEqual(json.loads(requests[-1][2]["messages"][-1]["content"]), {"文本": "示例"})
