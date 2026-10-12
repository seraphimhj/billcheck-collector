import argparse
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from .mailbox import ImapMailbox
from .run import IMAP_ERRORS, UNAUTHORIZED_MSG, run_once, sanitize
from .server import RetryLater, Server, Unauthorized, require_https

log = logging.getLogger("billcheck_collector")

REQUIRED_ENV = ("BILLCHECK_TOKEN", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD")
STATE_DIR_HINT = (
    "Docker 请用命名卷 -v billcheck-collector:/data（容器以 uid 10001 运行，主机目录挂载需先"
    " chown 10001）；直接运行请把 STATE_PATH 设到当前用户可写的目录"
)
IMAP_HINT = "确认 IMAP_PASSWORD 是邮箱授权码而不是登录密码，且邮箱设置里已开启 IMAP 服务"
NETWORK_HINT = "核对 IMAP_HOST / IMAP_PORT，并确认这台机器能连到该服务器的 993 端口"


def load_env_file(path: str) -> dict[str, str]:
    """docker --env-file format: KEY=value per line, value taken literally, # comments."""
    out = {}
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value
    return out


def state_dir_error(path: Path) -> str | None:
    """None when the state file's directory exists (created if needed) and is writable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tempfile.TemporaryFile(dir=path.parent).close()
    except OSError as e:
        return sanitize(e)
    return None


def check(server: Server, open_mailbox, state_path: Path, secrets) -> int:
    """Preflight for installers: no heartbeat, no upload, no state written. 0 = all ok."""
    failed = False

    def report(ok: bool, name: str, detail: str = "") -> None:
        nonlocal failed
        failed = failed or not ok
        print(f"[{'ok' if ok else '失败'}] {name}" + (f"：{detail}" if detail else ""), flush=True)

    err = state_dir_error(state_path)
    if err:
        report(False, f"状态目录 {state_path.parent} 不可写", f"{err} → {STATE_DIR_HINT}")
    else:
        report(True, f"状态目录 {state_path.parent} 可写")

    try:
        cfg = server.config()
        report(True, "令牌有效", f"服务端下发 {len(cfg['sender_domains'])} 个银行发件域名")
    except Unauthorized:
        report(False, "令牌无效", UNAUTHORIZED_MSG)
    except RetryLater:
        report(False, "连不上服务端", "检查网络与 BILLCHECK_SERVER，稍后重试")

    try:
        mailbox = open_mailbox()
        mailbox.close()
        report(True, "IMAP 登录成功（只读打开收件箱）")
    except IMAP_ERRORS as e:
        hint = NETWORK_HINT if isinstance(e, OSError) else IMAP_HINT
        report(False, "IMAP 登录失败", f"{sanitize(e, secrets)} → {hint}")

    print("自检通过" if not failed else "自检未通过，按上面的提示处理后重新运行 --check", flush=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="billcheck_collector")
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    ap.add_argument(
        "--check", action="store_true",
        help="verify state dir, token and IMAP login, then exit (0 = ok); uploads nothing",
    )
    ap.add_argument("--env-file", help="read settings from a docker-style KEY=value file")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Real environment variables win over the file, like docker's -e over --env-file.
    env = {**(load_env_file(args.env_file) if args.env_file else {}), **os.environ}
    base_url = env.get("BILLCHECK_SERVER", "https://cc.ivpser.cn")
    if not require_https(base_url):
        log.error("BILLCHECK_SERVER 必须以 https:// 开头，已拒绝启动")
        sys.exit(2)
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        log.error("缺少环境变量：%s，已拒绝启动", ", ".join(missing))
        sys.exit(2)
    server = Server(base_url, env["BILLCHECK_TOKEN"])
    secrets = (env["IMAP_PASSWORD"], env["BILLCHECK_TOKEN"])
    state_path = Path(env.get("STATE_PATH", "/data/state.json"))
    interval = float(env.get("INTERVAL_SECONDS", "3600"))

    def open_mailbox() -> ImapMailbox:
        return ImapMailbox(
            env["IMAP_HOST"],
            int(env.get("IMAP_PORT", "993")),
            env["IMAP_USER"],
            env["IMAP_PASSWORD"],
        )

    if args.check:
        sys.exit(check(server, open_mailbox, state_path, secrets))

    # Fail fast: an unwritable state dir would otherwise surface every round as a mailbox error.
    err = state_dir_error(state_path)
    if err:
        log.error("状态目录 %s 不可写：%s → %s", state_path.parent, err, STATE_DIR_HINT)
        sys.exit(2)

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
