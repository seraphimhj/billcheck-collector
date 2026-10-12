import contextlib
import imaplib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from billcheck_collector import __main__ as entry
from billcheck_collector.server import RetryLater, Unauthorized

ENV = {
    "BILLCHECK_TOKEN": "bcc_secrettoken",
    "IMAP_HOST": "imap.example.com",
    "IMAP_USER": "u@example.com",
    "IMAP_PASSWORD": "hunter2-password",
}


class FakeServer:
    exc = None

    def __init__(self, *a):
        pass

    def config(self):
        if self.exc:
            raise self.exc
        return {"sender_domains": ["cmbchina.com", "bank.com"]}

    def heartbeat(self, **kw):
        raise AssertionError("--check must not send a heartbeat")

    def upload(self, *a, **kw):
        raise AssertionError("--check must not upload")


class FakeMailbox:
    def __init__(self, *a):
        pass

    def close(self):
        pass


def imap_fail(*a):
    raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] bad " + a[3].encode())


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeServer.exc = None

    def run_check(self, env, mailbox=FakeMailbox):
        out = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(entry, "Server", FakeServer), \
                mock.patch.object(entry, "ImapMailbox", mailbox), \
                contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as cm:
            entry.main(["--check"])
        return cm.exception.code, out.getvalue()

    def env(self, **extra):
        return {**ENV, "STATE_PATH": str(Path(self.tmp.name) / "sub" / "state.json"), **extra}

    def test_all_ok_exits_zero_and_writes_no_state(self):
        code, out = self.run_check(self.env())
        self.assertEqual(code, 0, out)
        self.assertIn("2 个银行发件域名", out)
        self.assertIn("自检通过", out)
        self.assertEqual(os.listdir(Path(self.tmp.name) / "sub"), [])

    def test_missing_env_exits_two(self):
        env = self.env()
        del env["IMAP_PASSWORD"]
        with self.assertLogs("billcheck_collector", "ERROR") as logs:
            code, _ = self.run_check(env)
        self.assertEqual(code, 2)
        self.assertIn("IMAP_PASSWORD", "\n".join(logs.output))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_state_dir_named_as_such(self):
        ro = Path(self.tmp.name) / "ro"
        ro.mkdir()
        ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(ro.chmod, stat.S_IRWXU)
        code, out = self.run_check(self.env(STATE_PATH=str(ro / "state.json")))
        self.assertEqual(code, 1)
        self.assertIn("不可写", out)
        self.assertIn("命名卷", out)
        self.assertIn("[ok] IMAP", out)  # not misreported as a mailbox failure

    def test_unauthorized_and_unreachable(self):
        FakeServer.exc = Unauthorized()
        code, out = self.run_check(self.env())
        self.assertEqual(code, 1)
        self.assertIn("令牌无效", out)
        FakeServer.exc = RetryLater()
        code, out = self.run_check(self.env())
        self.assertEqual(code, 1)
        self.assertIn("连不上服务端", out)

    def test_imap_failure_hint_without_secrets(self):
        code, out = self.run_check(self.env(), mailbox=imap_fail)
        self.assertEqual(code, 1)
        self.assertIn("[AUTHENTICATIONFAILED] bad", out)
        self.assertIn("授权码", out)
        self.assertNotIn("hunter2-password", out)
        self.assertNotIn("bcc_secrettoken", out)

    def test_unreachable_host_points_at_host_not_password(self):
        def unreachable(*a):
            raise OSError(8, "nodename nor servname provided")

        code, out = self.run_check(self.env(), mailbox=unreachable)
        self.assertEqual(code, 1)
        self.assertIn("IMAP_HOST", out)
        self.assertNotIn("授权码", out)


class EnvFileTest(unittest.TestCase):
    def test_values_are_literal_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "collector.env"
            f.write_text("# comment\nIMAP_PASSWORD=a$b c=d'\nIMAP_USER=file-user\n\n")
            self.assertEqual(
                entry.load_env_file(str(f)),
                {"IMAP_PASSWORD": "a$b c=d'", "IMAP_USER": "file-user"},
            )
            env = {**ENV, "STATE_PATH": str(Path(d) / "state.json")}
            del env["IMAP_PASSWORD"]
            seen = {}

            def mailbox(host, port, user, password):
                seen.update(user=user, password=password)
                return FakeMailbox()

            with mock.patch.dict("os.environ", env, clear=True), \
                    mock.patch.object(entry, "Server", FakeServer), \
                    mock.patch.object(entry, "ImapMailbox", mailbox), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                entry.main(["--check", "--env-file", str(f)])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(seen, {"user": "u@example.com", "password": "a$b c=d'"})


class RunFailFastTest(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_state_dir_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            ro = Path(d) / "ro"
            ro.mkdir()
            ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                env = {**ENV, "STATE_PATH": str(ro / "state.json")}
                with mock.patch.dict("os.environ", env, clear=True), \
                        mock.patch.object(entry, "Server", FakeServer), \
                        self.assertLogs("billcheck_collector", "ERROR") as logs, \
                        self.assertRaises(SystemExit) as cm:
                    entry.main(["--once"])
            finally:
                ro.chmod(stat.S_IRWXU)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("命名卷", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
