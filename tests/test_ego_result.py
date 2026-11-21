import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from discovery import read_result


class EgoResultTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.result_file = Path(temp.name) / "result.json"
        self.data = {"space": 4, "status": "读取完成", "rows": [{"title": "第一行\u2028第二行"}]}
        self.payload = json.dumps(self.data, ensure_ascii=False).encode("utf-8")

    def read(self, stdout=b"", stderr=b"", code=0):
        return read_result(self.result_file, subprocess.CompletedProcess([], code, stdout, stderr))

    def test_normal_file_transport(self):
        self.result_file.write_bytes(self.payload)
        self.assertEqual(self.read(stderr=b"browser diagnostic\r\n"), self.data)

    def test_windows_console_transport_on_either_stream(self):
        record = b"VAT_RESULT " + self.payload + b"\r\n"
        for channel in ("stdout", "stderr"):
            with self.subTest(channel=channel):
                self.assertEqual(self.read(**{channel: record}), self.data)

    def test_partial_file_can_recover_from_console(self):
        self.result_file.write_bytes(self.payload[:7])
        self.assertEqual(self.read(stderr=b"VAT_RESULT " + self.payload), self.data)

    def test_real_browser_error_is_reported(self):
        with self.assertRaisesRegex(ValueError, "EGO_PAGE_UNRESPONSIVE"):
            self.read(stderr=b"Error: EGO_PAGE_UNRESPONSIVE", code=1)

    def test_truncated_payload_is_not_success(self):
        with self.assertRaisesRegex(ValueError, "数据不完整"):
            self.read(stderr=b'VAT_RESULT {"status":"')
