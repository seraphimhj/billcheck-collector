import imaplib
import json
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from billcheck_collector import __main__ as entry
from billcheck_collector import server


class Handler(BaseHTTPRequestHandler):
    hits: list = []
    reply = (200, {}, b"{}")

    bodies: list = []

    def do_POST(self):
        self.hits.append(self.path)
        self.bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        code, headers, body = self.reply
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *a):
        pass


class ServerTest(unittest.TestCase):
    def setUp(self):
        Handler.hits = []
        Handler.bodies = []
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.srv = server.Server(f"http://127.0.0.1:{self.httpd.server_port}", "bcc_t", timeout=5)

    def up(self):
        return self.srv.upload(b"x", uid="1", uidvalidity="2")

    def test_only_not_bank_sender_422_is_done(self):
        Handler.reply = (422, {}, json.dumps({"error_code": "not_bank_sender"}).encode())
        self.assertEqual(self.up(), "done")
        Handler.reply = (422, {}, json.dumps({"error_code": "validation_error"}).encode())
        with self.assertRaises(server.RetryLater):
            self.up()
        Handler.reply = (422, {}, b"not json")
        with self.assertRaises(server.RetryLater):
            self.up()

    def test_only_message_too_large_413_is_done_and_429_carries_retry_after(self):
        Handler.reply = (413, {}, json.dumps({"error_code": "message_too_large"}).encode())
        self.assertEqual(self.up(), "done")
        # A proxy/CDN body limit must not silently drop a bank mail: stop the round instead.
        Handler.reply = (413, {}, b"<html>413 Request Entity Too Large</html>")
        with self.assertRaises(server.RetryLater):
            self.up()
        Handler.reply = (413, {}, b"")
        with self.assertRaises(server.RetryLater):
            self.up()
        Handler.reply = (429, {"Retry-After": "7"}, b"")
        with self.assertRaises(server.RetryLater) as cm:
            self.up()
        self.assertEqual(cm.exception.retry_after, 7)

    def test_redirects_are_not_followed(self):
        Handler.reply = (302, {"Location": f"http://127.0.0.1:{self.httpd.server_port}/other"}, b"")
        with self.assertRaises(server.RetryLater):
            self.up()
        with self.assertRaises(server.RetryLater):
            self.srv.config()
        self.assertNotIn("/other", Handler.hits)

    def test_heartbeat_posts_json(self):
        Handler.reply = (204, {}, b"")
        self.srv.heartbeat(imap_ok=False)
        self.assertEqual(Handler.hits, ["/api/collector/heartbeat"])
        self.assertEqual(json.loads(Handler.bodies[0]), {"imap_ok": False})

    def test_401_raises_unauthorized(self):
        Handler.reply = (401, {}, b"")
        with self.assertRaises(server.Unauthorized):
            self.srv.config()


class EntryTest(unittest.TestCase):
    def test_http_server_url_refused(self):
        env = {"BILLCHECK_SERVER": "http://example.com", "BILLCHECK_TOKEN": "bcc_t"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertLogs("billcheck_collector", "ERROR") as cm:
                with self.assertRaises(SystemExit):
                    entry.main(["--once"])
        self.assertIn("https://", "\n".join(cm.output))

    def test_logs_never_contain_secrets(self):
        env = {
            "BILLCHECK_SERVER": "https://127.0.0.1:1",
            "BILLCHECK_TOKEN": "bcc_secrettoken",
            "IMAP_HOST": "h",
            "IMAP_USER": "u",
            "IMAP_PASSWORD": "hunter2-password",
            "STATE_PATH": "/nonexistent/state.json",
        }
        cfg = {"sender_domains": [], "since_days": 1, "max_message_bytes": 1}

        class OkServer:
            def __init__(self, *a):
                pass

            def config(self):
                return cfg

            def heartbeat(self, **kw):
                pass

        def boom(*a):
            raise RuntimeError("login failed for " + a[3])

        def imap_boom(*a):
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] bad creds " + a[3].encode())

        with mock.patch.dict("os.environ", env, clear=True):
            logging.getLogger("billcheck_collector").setLevel(logging.DEBUG)
            with self.assertLogs("billcheck_collector", "DEBUG") as cm:
                entry.main(["--once"])  # real Server -> network error path
                with mock.patch.object(entry, "Server", OkServer):
                    with mock.patch.object(entry, "ImapMailbox", boom):
                        entry.main(["--once"])  # unexpected failure path
                    with mock.patch.object(entry, "ImapMailbox", imap_boom):
                        entry.main(["--once"])  # IMAP login failure path
        text = "\n".join(cm.output)
        self.assertIn("round", text)
        # The IMAP server's reason is logged (not just the exception type), without secrets.
        self.assertIn("[AUTHENTICATIONFAILED] bad creds", text)
        self.assertIn("login failed for", text)
        for secret in ("hunter2-password", "bcc_secrettoken"):
            self.assertNotIn(secret, text)
