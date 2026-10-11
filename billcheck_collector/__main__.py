import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .mailbox import ImapMailbox
from .run import run_once, sanitize
from .server import Server, require_https

log = logging.getLogger("billcheck_collector")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="billcheck_collector")
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    env = os.environ
    base_url = env.get("BILLCHECK_SERVER", "https://cc.ivpser.cn")
    if not require_https(base_url):
        log.error("BILLCHECK_SERVER 必须以 https:// 开头，已拒绝启动")
        sys.exit(2)
    server = Server(base_url, env["BILLCHECK_TOKEN"])
    secrets = (env.get("IMAP_PASSWORD", ""), env["BILLCHECK_TOKEN"])
    state_path = Path(env.get("STATE_PATH", "/data/state.json"))
    interval = float(env.get("INTERVAL_SECONDS", "3600"))

    def open_mailbox() -> ImapMailbox:
        return ImapMailbox(
            env["IMAP_HOST"],
            int(env.get("IMAP_PORT", "993")),
            env["IMAP_USER"],
            env["IMAP_PASSWORD"],
        )

    while True:
        try:
            result = run_once(
                open_mailbox, server, state_path, now=time.time(), secrets=secrets
            )
            log.info("round finished: %s", result)
        except Exception as e:  # unexpected failure: retry next round
            log.error("round failed: %s: %s", type(e).__name__, sanitize(e, secrets))
        if args.once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
