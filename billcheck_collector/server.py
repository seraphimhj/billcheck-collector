import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

from . import __version__


class Unauthorized(Exception):
    pass


class RetryLater(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("retry later")
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surfaces as HTTPError(3xx); the token must never follow a redirect


_opener = urllib.request.build_opener(_NoRedirect)


def _error_code(e: urllib.error.HTTPError) -> str | None:
    try:
        return json.loads(e.read()).get("error_code")
    except (ValueError, AttributeError, OSError):
        return None


def require_https(url: str) -> bool:
    return url.lower().startswith("https://")


class Server:
    def __init__(self, base_url: str, token: str, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, req: urllib.request.Request) -> tuple[int, bytes]:
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", f"billcheck-collector/{__version__}")
        try:
            with _opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise Unauthorized() from None
            # Only the billcheck size limit is final; a proxy/CDN 413 must not drop a bank mail.
            if e.code == 413 and _error_code(e) == "message_too_large":
                return e.code, b""
            if e.code == 422 and _error_code(e) == "not_bank_sender":
                return e.code, b""
            retry_after = None
            if e.code == 429:
                try:
                    retry_after = float(e.headers.get("Retry-After", ""))
                except ValueError:
                    pass
            raise RetryLater(retry_after) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise RetryLater(None) from None

    def config(self) -> dict:
        _, body = self._request(urllib.request.Request(f"{self.base_url}/api/collector/config"))
        return json.loads(body)

    def heartbeat(self, *, imap_ok: bool) -> None:
        req = urllib.request.Request(
            f"{self.base_url}/api/collector/heartbeat",
            data=json.dumps({"imap_ok": imap_ok}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        self._request(req)

    def upload(self, raw: bytes, *, uid: str, uidvalidity: str) -> str:
        query = urlencode({"uid": uid, "uidvalidity": uidvalidity})
        req = urllib.request.Request(
            f"{self.base_url}/api/collector/messages?{query}",
            data=raw,
            method="POST",
            headers={"Content-Type": "message/rfc822"},
        )
        status, body = self._request(req)
        if status in (413, 422):
            return "done"
        return str(json.loads(body)["action"])
