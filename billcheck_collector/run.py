import imaplib
import logging
import time
from collections.abc import Callable, Iterable
from email.parser import BytesHeaderParser
from email.utils import parseaddr
from pathlib import Path

from . import state as state_mod
from .mailbox import Mailbox, MailboxError
from .server import RetryLater, Server, Unauthorized

log = logging.getLogger("billcheck_collector")

UNAUTHORIZED_MSG = "令牌已失效或需要在小程序重新确认授权，请打开小程序处理"
DEFER_RETRY_SECONDS = 86400
BATCH = 50
MAX_RETRY_AFTER = 300
MAX_ERROR_CHARS = 200
# login / EXAMINE / SEARCH / FETCH failures and socket/TLS errors talking to the IMAP server
IMAP_ERRORS = (MailboxError, imaplib.IMAP4.error, OSError)


def sanitize(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """Error text safe to log and to report: known secrets masked, one line, at most 200 chars."""
    parts = [a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a) for a in exc.args]
    text = " ".join(parts) or type(exc).__name__
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return " ".join(text.split())[:MAX_ERROR_CHARS]


def _domain(sender: str) -> str:
    return sender.rpartition("@")[2].strip().lower()


def is_bank(sender: str, domains: list[str]) -> bool:
    d = _domain(sender) if "@" in sender else ""
    return bool(d) and any(d == x or d.endswith("." + x) for x in domains)


def _raw_sender(raw: bytes) -> str | None:
    return parseaddr(str(BytesHeaderParser().parsebytes(raw).get("From", "")))[1] or None


def _upload(server: Server, raw: bytes, uid: int, uidvalidity: str) -> str:
    """Upload; on 429 with Retry-After wait once and try again."""
    try:
        return server.upload(raw, uid=str(uid), uidvalidity=uidvalidity)
    except RetryLater as e:
        if e.retry_after is None:
            raise
        time.sleep(min(e.retry_after, MAX_RETRY_AFTER))
        return server.upload(raw, uid=str(uid), uidvalidity=uidvalidity)


def _post_heartbeat(server: Server, imap_ok: bool) -> None:
    try:
        server.heartbeat(imap_ok=imap_ok)
    except (RetryLater, Unauthorized):
        log.warning("heartbeat not delivered, will report next round")


def run_once(
    mailbox: Mailbox | Callable[[], Mailbox],
    server: Server,
    state_path: Path,
    *,
    now: float,
    secrets: Iterable[str] = (),
) -> str:
    """`mailbox` may be a factory; it is only called after GET /config succeeded.

    Every round that got past /config ends with a heartbeat: imap_ok=True once the mailbox was
    usable, imap_ok=False otherwise (the
    sanitized error is only logged locally, never sent). An unexpected failure posts nothing,
    so the server's "collector silent" warning still fires."""
    opened = None
    beat: bool | None = None
    try:
        cfg = server.config()
        try:
            if callable(mailbox):
                opened = mailbox = mailbox()
            result = _round(mailbox, server, state_path, cfg, now)
        except RetryLater:
            beat = True  # the server failed mid-round; the mailbox was fine
            raise
        except IMAP_ERRORS as e:
            error = sanitize(e, secrets)
            beat = False
            log.warning("mailbox error, will retry next round: %s", error)
            return "retry"
        beat = True
        return result
    except Unauthorized:
        log.error(UNAUTHORIZED_MSG)
        return "unauthorized"
    except RetryLater:
        log.warning("server unavailable, will retry next round")
        return "retry"
    finally:
        close = getattr(opened, "close", None)
        if close:
            close()
        if beat is not None:
            _post_heartbeat(server, beat)


def _round(mailbox: Mailbox, server: Server, path: Path, cfg: dict, now: float) -> str:
    domains = [d.lower() for d in cfg["sender_domains"]]
    max_bytes = int(cfg["max_message_bytes"])
    st = state_mod.load(path)
    uv = mailbox.uidvalidity()
    if st.uidvalidity != uv:
        st = state_mod.State(uv, 0, {})
        state_mod.save(path, st)

    for key, last in sorted(st.deferred.items(), key=lambda kv: int(kv[0])):
        if now - last < DEFER_RETRY_SECONDS:
            continue
        try:
            raw = mailbox.fetch(int(key))
        except LookupError:
            del st.deferred[key]
            state_mod.save(path, st)
            continue
        raw_sender = _raw_sender(raw)
        if raw_sender is None or not is_bank(raw_sender, domains):
            log.info("deferred uid=%s domain=%s not a bank sender, dropped", key,
                     _domain(raw_sender or ""))
            del st.deferred[key]
            state_mod.save(path, st)
            continue
        action = _upload(server, raw, int(key), uv)
        log.info("deferred uid=%s action=%s", key, action)
        if action == "retry":
            return "retry"  # keep the entry and its timestamp
        if action == "defer":
            st.deferred[key] = now
        else:
            del st.deferred[key]
        state_mod.save(path, st)

    new = [u for u in mailbox.search_since(int(cfg["since_days"])) if u > st.watermark]
    for i in range(0, len(new), BATCH):
        heads = mailbox.headers(new[i : i + BATCH])
        for uid in new[i : i + BATCH]:
            sender, size = heads.get(uid, (None, None))
            raw = None
            # size None = server did not report it: fetch locally, then gate on len(raw)
            if sender is None and (size is None or size <= max_bytes):
                raw = mailbox.fetch(uid)
                sender = _raw_sender(raw)
            if sender is None or not is_bank(sender, domains):
                action = "skip"
            elif size is not None and size > max_bytes:
                log.info("uid=%s domain=%s oversize, skipped", uid, _domain(sender))
                action = "skip"
            else:
                raw = raw if raw is not None else mailbox.fetch(uid)
                if len(raw) > max_bytes:
                    log.info("uid=%s domain=%s oversize, skipped", uid, _domain(sender))
                    st.watermark = uid
                    state_mod.save(path, st)
                    continue
                raw_sender = _raw_sender(raw)
                if raw_sender is None or not is_bank(raw_sender, domains):
                    # Defence in depth: the full message must still be from a bank sender.
                    log.info("uid=%s domain=%s not a bank sender, skipped", uid,
                             _domain(raw_sender or ""))
                    st.watermark = uid
                    state_mod.save(path, st)
                    continue
                action = _upload(server, raw, uid, uv)
                log.info("uid=%s domain=%s action=%s", uid, _domain(sender), action)
                if action == "retry":
                    return "retry"
                if action == "defer":
                    st.deferred[str(uid)] = now
            st.watermark = uid
            state_mod.save(path, st)
    return "ok"
