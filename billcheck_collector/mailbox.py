import imaplib
import re
import ssl
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parseaddr
from typing import Protocol

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class MailboxError(Exception):
    """IMAP answered NO/BAD or something unusable; the round must fail without state change."""


def _ok(typ: str, what: str) -> None:
    if typ != "OK":
        raise MailboxError(f"IMAP {what} returned {typ}")


class Mailbox(Protocol):
    def uidvalidity(self) -> str: ...

    def search_since(self, days: int) -> list[int]: ...

    def headers(self, uids: list[int]) -> dict[int, tuple[str | None, int | None]]: ...

    def fetch(self, uid: int) -> bytes: ...


class ImapMailbox:
    def __init__(self, host: str, port: int, user: str, password: str):
        self._imap = imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl.create_default_context(), timeout=60
        )
        self._imap.login(user, password)
        typ, _ = self._imap.select("INBOX", readonly=True)  # EXAMINE: read-only
        if typ != "OK":
            raise imaplib.IMAP4.error("EXAMINE INBOX failed")

    def close(self) -> None:
        try:
            self._imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def uidvalidity(self) -> str:
        _, data = self._imap.response("UIDVALIDITY")
        value = data[-1].decode() if data and data[-1] else ""
        if not value:
            raise MailboxError("empty UIDVALIDITY")
        return value

    def search_since(self, days: int) -> list[int]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        date = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
        typ, data = self._imap.uid("SEARCH", "SINCE", date)
        _ok(typ, "SEARCH")
        return sorted(int(x) for x in (data[0] or b"").split())

    def headers(self, uids: list[int]) -> dict[int, tuple[str | None, int | None]]:
        if not uids:
            return {}
        spec = ",".join(str(u) for u in uids)
        typ, data = self._imap.uid(
            "FETCH", spec, "(BODY.PEEK[HEADER.FIELDS (FROM)] RFC822.SIZE)"
        )
        _ok(typ, "FETCH")
        # RFC 3501 lets the server order fetch items freely, so UID / RFC822.SIZE may sit in
        # the tuple prefix or in the bare bytes that follow the literal. Gather both per message.
        entries: list[list] = []  # [meta text, header bytes]
        for item in data:
            if isinstance(item, tuple):
                entries.append([item[0].decode("ascii", "replace"), item[1]])
            elif isinstance(item, bytes) and entries:
                entries[-1][0] += " " + item.decode("ascii", "replace")
        out: dict[int, tuple[str | None, int | None]] = {}
        for meta, header in entries:
            uid = re.search(r"UID (\d+)", meta)
            if not uid:
                continue
            size = re.search(r"RFC822\.SIZE (\d+)", meta)
            addr = parseaddr(str(message_from_bytes(header).get("From", "")))[1]
            out[int(uid.group(1))] = (
                addr if "@" in addr else None,
                int(size.group(1)) if size else None,
            )
        return out

    def fetch(self, uid: int) -> bytes:
        typ, data = self._imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        _ok(typ, "FETCH")
        for item in data:
            if isinstance(item, tuple):
                return bytes(item[1])
        raise LookupError(f"uid {uid} not found")
