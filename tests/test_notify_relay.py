import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from server_autoops.notify import NotifyError, make_notifier, validate_notify_config
from server_autoops.relay import dispatch


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_bark_url_never_in_config(self):
        with self.assertRaises(NotifyError):
            validate_notify_config({"kind": "bark", "url": "https://bark.example/abc"})
        with self.assertRaises(NotifyError):
            validate_notify_config({"kind": "bark"})

    def test_default_level_is_passive_for_every_kind(self):
        cfg = validate_notify_config({"kind": "file", "path": str(self.base / "out.jsonl")})
        send = make_notifier(cfg)
        for kind in ("needs_scan", "breaker", "daily", "alert_failure"):
            self.assertTrue(send("t", "b", kind))
        rows = [json.loads(x) for x in (self.base / "out.jsonl").read_text().splitlines()]
        self.assertEqual({r["level"] for r in rows}, {"passive"})
        self.assertEqual(stat.S_IMODE((self.base / "out.jsonl").stat().st_mode), 0o600)

    def test_bark_post_uses_configured_level_and_url_file(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b'{"code":200}'
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url_file = self.base / "bark.url"
        url_file.write_text(f"http://127.0.0.1:{server.server_address[1]}/devicekey\n")
        os.chmod(url_file, 0o644)
        send = make_notifier({"kind": "bark", "url_file": str(url_file), "levels": {"needs_scan": "active"}})
        with self.assertRaises(NotifyError):  # world-readable secret file is refused
            send("t", "b", "daily")
        os.chmod(url_file, 0o600)
        self.assertTrue(send("t", "b", "daily"))
        self.assertTrue(send("t", "b", "needs_scan"))
        server.shutdown(); server.server_close()
        self.assertEqual([s["level"] for s in seen], ["passive", "active"])

    def test_command_backend_gets_json_on_stdin(self):
        out = self.base / "cmd.json"
        script = self.base / "hook.sh"
        script.write_text(f"#!/bin/sh\ncat > {out}\n")
        script.chmod(0o700)
        send = make_notifier({"kind": "command", "argv": [str(script)]})
        self.assertTrue(send("标题", "正文", "breaker"))
        self.assertEqual(json.loads(out.read_text())["title"], "标题")

    def test_relay_appends_stream_even_when_push_fails(self):
        config = {"stream": str(self.base / "alerts.jsonl"), "timezone": "UTC+08:00",
                  "notify": {"kind": "command", "argv": ["/bin/false"]}}
        result = dispatch(config, "web", "UNHEALTHY", "failure", "web down", "HTTP 503", auto_recovery_handled=True)
        self.assertTrue(result["stream_ok"])
        self.assertFalse(result["push_ok"])
        record = json.loads((self.base / "alerts.jsonl").read_text())
        self.assertEqual(record["fault_code"], "UNHEALTHY")
        self.assertTrue(record["auto_recovery_handled"])
        self.assertTrue(record["iso_time"].endswith("+08:00"))
