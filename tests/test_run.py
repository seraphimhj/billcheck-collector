import imaplib
import logging
import tempfile
import unittest
from pathlib import Path

from billcheck_collector import run, server, state
from billcheck_collector.mailbox import MailboxError

CFG = {"sender_domains": ["cmbchina.com"], "since_days": 31, "max_message_bytes": 1000}


def raw_from(addr):
    return f"From: {addr}\r\nSubject: secret-subject\r\n\r\nbody".encode()


class FakeMailbox:
    def __init__(self, msgs, uidvalidity="1"):
        # msgs: {uid: (sender_or_None, size, raw)}
        self.msgs = msgs
        self._uv = uidvalidity
        self.fetched = []

    def uidvalidity(self):
        return self._uv

    def search_since(self, days):
        return sorted(self.msgs)

    def headers(self, uids):
        return {u: (self.msgs[u][0], self.msgs[u][1]) for u in uids}

    def fetch(self, uid):
        self.fetched.append(uid)
        return self.msgs[uid][2]


class FakeServer:
    def __init__(self, actions=None, cfg=CFG, config_exc=None):
        self.actions = list(actions or [])
        self.cfg = cfg
        self.config_exc = config_exc
        self.uploads = []
        self.beats = []

    def config(self):
        if self.config_exc:
            raise self.config_exc
        return self.cfg

    def upload(self, raw, *, uid, uidvalidity):
        self.uploads.append(int(uid))
        return self.actions.pop(0) if self.actions else "done"

    def heartbeat(self, *, imap_ok):
        self.beats.append(imap_ok)


def bank(uid, size=10):
    return (uid, ("a@cmbchina.com", size, raw_from("a@cmbchina.com")))


class RunTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.path = Path(self._d.name) / "s.json"

    def test_is_bank_suffix(self):
        self.assertTrue(run.is_bank("x@Mail.CMBChina.com", ["cmbchina.com"]))
        self.assertFalse(run.is_bank("x@evilcmbchina.com", ["cmbchina.com"]))
        self.assertFalse(run.is_bank("nonsense", ["cmbchina.com"]))

    def test_non_bank_mail_is_never_fetched_or_uploaded(self):
        mb = FakeMailbox({1: ("x@news.com", 10, b"")})
        srv = FakeServer()
        self.assertEqual(run.run_once(mb, srv, self.path, now=0), "ok")
        self.assertEqual(mb.fetched, [])
        self.assertEqual(srv.uploads, [])
        self.assertEqual(state.load(self.path).watermark, 1)

    def test_unparsable_header_fetches_locally_but_uploads_only_bank(self):
        mb = FakeMailbox({
            1: (None, 10, raw_from("x@news.com")),
            2: (None, 10, raw_from("Bank <a@cmbchina.com>")),
        })
        srv = FakeServer()
        run.run_once(mb, srv, self.path, now=0)
        self.assertEqual(mb.fetched, [1, 2])
        self.assertEqual(srv.uploads, [2])
        self.assertEqual(state.load(self.path).watermark, 2)

    def test_oversize_bank_mail_is_skipped(self):
        mb = FakeMailbox(dict([bank(1, size=5000)]))
        srv = FakeServer()
        run.run_once(mb, srv, self.path, now=0)
        self.assertEqual(srv.uploads, [])
        self.assertEqual(state.load(self.path).watermark, 1)

    def test_retry_stops_round_without_advancing(self):
        mb = FakeMailbox(dict([bank(1), bank(2), bank(3)]))
        srv = FakeServer(["done", "retry", "done"])
        self.assertEqual(run.run_once(mb, srv, self.path, now=0), "retry")
        self.assertEqual(srv.uploads, [1, 2])
        self.assertEqual(state.load(self.path).watermark, 1)

    def test_retry_later_exception_stops_round(self):
        class Boom(FakeServer):
            def upload(self, raw, *, uid, uidvalidity):
                raise server.RetryLater(None)

        mb = FakeMailbox(dict([bank(1)]))
        boom = Boom()
        self.assertEqual(run.run_once(mb, boom, self.path, now=0), "retry")
        self.assertEqual(state.load(self.path).watermark, 0)
        self.assertEqual(boom.beats, [True])  # mailbox was fine; server failed mid-round

    def test_defer_tracks_and_reuploads_after_24h(self):
        mb = FakeMailbox(dict([bank(1)]))
        srv = FakeServer(["defer"])
        run.run_once(mb, srv, self.path, now=0)
        self.assertEqual(state.load(self.path).deferred, {"1": 0})
        run.run_once(mb, srv, self.path, now=3600)
        self.assertEqual(srv.uploads, [1])
        srv.actions = ["done"]
        run.run_once(mb, srv, self.path, now=86400)
        self.assertEqual(srv.uploads, [1, 1])
        st = state.load(self.path)
        self.assertEqual(st.deferred, {})
        self.assertEqual(st.watermark, 1)

    def test_deferred_retry_keeps_entry_and_stops_round(self):
        mb = FakeMailbox(dict([bank(1), bank(2)]))
        srv = FakeServer(["defer", "done"])
        run.run_once(mb, srv, self.path, now=0)
        srv.actions = ["retry"]
        self.assertEqual(run.run_once(mb, srv, self.path, now=86400), "retry")
        self.assertEqual(state.load(self.path).deferred, {"1": 0})

    def test_mailbox_error_in_deferred_fetch_keeps_entry(self):
        state.save(self.path, state.State("1", 1, {"1": 0}))

        class NoMb(FakeMailbox):
            def fetch(self, uid):
                raise MailboxError("NO")

        self.assertEqual(
            run.run_once(NoMb(dict([bank(1)])), FakeServer(), self.path, now=86400), "retry"
        )
        self.assertEqual(state.load(self.path).deferred, {"1": 0})

    def test_unknown_size_is_gated_on_raw_length(self):
        raw = raw_from("a@cmbchina.com") + b"x" * 2000
        mb = FakeMailbox({1: ("a@cmbchina.com", None, raw)})
        srv = FakeServer()
        run.run_once(mb, srv, self.path, now=0)
        self.assertEqual(srv.uploads, [])
        self.assertEqual(state.load(self.path).watermark, 1)

    def test_unauthorized_never_constructs_mailbox(self):
        def factory():
            raise AssertionError("mailbox must not be opened")

        srv = FakeServer(config_exc=server.Unauthorized())
        self.assertEqual(run.run_once(factory, srv, self.path, now=0), "unauthorized")

    def test_factory_mailbox_is_closed(self):
        mb = FakeMailbox(dict([bank(1)]))
        closed = []
        mb.close = lambda: closed.append(1)
        run.run_once(lambda: mb, FakeServer(), self.path, now=0)
        self.assertEqual(closed, [1])

    def test_uidvalidity_change_resets_state(self):
        state.save(self.path, state.State("old", 50, {"7": 1.0}))
        mb = FakeMailbox(dict([bank(1)]), uidvalidity="new")
        srv = FakeServer()
        run.run_once(mb, srv, self.path, now=0)
        st = state.load(self.path)
        self.assertEqual((st.uidvalidity, st.watermark, st.deferred), ("new", 1, {}))
        self.assertEqual(srv.uploads, [1])

    def test_unauthorized_returns_without_touching_state(self):
        mb = FakeMailbox(dict([bank(1)]))
        srv = FakeServer(config_exc=server.Unauthorized())
        self.assertEqual(run.run_once(mb, srv, self.path, now=0), "unauthorized")
        self.assertFalse(self.path.exists())


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.path = Path(self._d.name) / "s.json"

    def test_ok_round_posts_imap_ok(self):
        srv = FakeServer()
        self.assertEqual(run.run_once(FakeMailbox(dict([bank(1)])), srv, self.path, now=0), "ok")
        self.assertEqual(srv.beats, [True])

    def test_upload_retry_round_still_reports_imap_ok(self):
        srv = FakeServer(["retry"])
        run.run_once(FakeMailbox(dict([bank(1)])), srv, self.path, now=0)
        self.assertEqual(srv.beats, [True])

    def test_login_failure_posts_only_imap_ok_and_logs_sanitized_error(self):
        def factory():
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] wrong code hunter2 " + b"z" * 400)

        srv = FakeServer()
        with self.assertLogs("billcheck_collector", "WARNING") as cm:
            result = run.run_once(factory, srv, self.path, now=0, secrets=("hunter2",))
        self.assertEqual(result, "retry")
        self.assertEqual(srv.beats, [False])
        self.assertNotIn("hunter2", "\n".join(cm.output))
        self.assertIn("AUTHENTICATIONFAILED", "\n".join(cm.output))
        self.assertFalse(self.path.exists())

    def test_connect_oserror_posts_failure(self):
        def factory():
            raise ConnectionRefusedError("refused")

        srv = FakeServer()
        self.assertEqual(run.run_once(factory, srv, self.path, now=0), "retry")
        self.assertEqual(srv.beats, [False])

    def test_search_failure_posts_failure(self):
        class NoSearch(FakeMailbox):
            def search_since(self, days):
                raise MailboxError("IMAP SEARCH returned NO")

        srv = FakeServer()
        self.assertEqual(run.run_once(NoSearch({}), srv, self.path, now=0), "retry")
        self.assertEqual(srv.beats, [False])

    def test_no_heartbeat_when_config_fails(self):
        srv = FakeServer(config_exc=server.Unauthorized())
        run.run_once(FakeMailbox({}), srv, self.path, now=0)
        self.assertEqual(srv.beats, [])

    def test_heartbeat_failure_does_not_change_result(self):
        class DeadBeat(FakeServer):
            def heartbeat(self, **kw):
                raise server.RetryLater(None)

        self.assertEqual(
            run.run_once(FakeMailbox(dict([bank(1)])), DeadBeat(), self.path, now=0), "ok"
        )


class RawFromRecheckTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.path = Path(self._d.name) / "s.json"

    def test_header_says_bank_but_raw_does_not(self):
        mb = FakeMailbox({1: ("a@cmbchina.com", 10, raw_from("x@news.example"))})
        srv = FakeServer()
        with self.assertLogs("billcheck_collector", logging.INFO) as cm:
            self.assertEqual(run.run_once(mb, srv, self.path, now=0), "ok")
        self.assertEqual(srv.uploads, [])
        self.assertEqual(state.load(self.path).watermark, 1)
        text = "\n".join(cm.output)
        self.assertIn("news.example", text)
        self.assertNotIn("secret-subject", text)
        self.assertNotIn("x@news.example", text)

    def test_deferred_raw_not_bank_is_dropped(self):
        state.save(self.path, state.State("1", 1, {"1": 0}))
        mb = FakeMailbox({1: ("a@cmbchina.com", 10, raw_from("x@news.example"))})
        srv = FakeServer()
        run.run_once(mb, srv, self.path, now=86400)
        self.assertEqual(srv.uploads, [])
        self.assertEqual(state.load(self.path).deferred, {})


if __name__ == "__main__":
    unittest.main()
