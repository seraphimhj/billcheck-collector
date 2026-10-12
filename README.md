# billcheck-collector

在你自己的机器上运行的邮箱采集器：登录你的 IMAP 邮箱，把银行发来的账单邮件原文上传到 billcheck 服务端。
仅使用 Python 标准库，无第三方依赖，许可证 AGPL-3.0。

## 做什么

- 只读方式（`EXAMINE INBOX`、`BODY.PEEK`）连接你的邮箱，按 UID 增量处理新邮件。
- 头部先行：先只取每封邮件的 `From` 头与邮件大小（`RFC822.SIZE`），用服务端下发的银行发件域名列表在本机判断。
- 发件人属于银行域名的邮件，才会取全文，并把 RFC822 原文上传到服务端；超过服务端下发的大小上限的邮件不上传。
- 其余邮件只读取 `From` 地址与邮件大小，主题、正文、附件不下载，更不上传。
- 每轮结束向服务端报告一次心跳：只包含「邮箱是否可连通」这一个布尔值（`imap_ok`），不附带任何错误文字，用于在小程序里提示「采集器无法登录邮箱」。
- 例外：极少数邮件的发件人头取不到（或服务器没有返回大小）时，采集器会在本机拉取整封邮件来判断发件人；判定为非银行的，只在内存中丢弃，不上传、不落盘。

## 不做什么

- 邮箱授权码只通过环境变量交给本进程，只存在于你的机器上；本程序不写盘、不上传、不打印。
- 不标记已读、不删除、不移动、不发送任何邮件。
- 日志只含 UID、发件域名、处理结果与 IMAP 错误文字（授权码与令牌已替换，仅留在本机日志），不含授权码、令牌、主题、正文。
- 令牌只发往你配置的 `BILLCHECK_SERVER`，且必须是 `https://`；遇到重定向不会跟随。

## 审计边界

本仓库能证明的是：哪些数据离开了你的机器。服务端收到邮件原文之后如何处理，不在本仓库可审计范围内。

## 数据流

```
 你的邮箱 (IMAP)                你的机器                        billcheck 服务端
 ---------------      -----------------------------      ----------------------
 From + 邮件大小  --> 本机判断发件域名是否为银行
                        |- 非银行: 丢弃, 只记 UID
                        '- 银行:   取全文 --------------> POST 邮件原文 (RFC822)
                                                          <-- GET 配置 (银行域名列表)
                        每轮结束 ---------------------> POST 心跳 (仅 IMAP 是否可用)
 授权码 --------------> 仅在本进程内存中使用 (不外发)
```

## 获取令牌

打开小程序 → 邮箱 → 本地采集器，按提示生成令牌（`BILLCHECK_TOKEN`）。令牌是一次性展示的。

## 让 AI 助手安装

在小程序生成令牌后点「复制给 AI 助手的指令」，粘贴给你本机的 AI 编程助手（Claude Code、Codex 等），
它会按 [`AGENTS.md`](AGENTS.md) 完成安装、自检和常驻运行。授权码不经过 AI 助手：它会给你一条命令，
由你在自己的终端里输入授权码写进本机配置文件。

## 运行

### Docker

```sh
docker run -d --name billcheck-collector --restart unless-stopped \
  -e BILLCHECK_TOKEN=... \
  -e IMAP_HOST=imap.example.com \
  -e IMAP_USER=you@example.com \
  -e IMAP_PASSWORD=... \
  -v billcheck-collector-data:/data \
  <镜像地址见小程序令牌页>
```

加 `--once` 作为镜像参数可只跑一轮就退出；加 `--check` 只做自检（状态目录可写、令牌有效、IMAP 能登录），
不上传任何邮件，全部通过时退出码为 0。

`/data` 请用命名卷（如上）。容器以 uid 10001 运行，挂载主机目录需先 `chown 10001` 该目录，否则状态文件写不进去，
采集器会在启动时报「状态目录不可写」并退出。

也可以把变量写进文件（每行 `KEY=值`，值不加引号），用 `--env-file 文件路径` 代替一串 `-e`。

### 直接用 Python（>= 3.10，无需安装依赖）

```sh
git clone https://github.com/seraphimhj/billcheck-collector.git
cd billcheck-collector
export BILLCHECK_TOKEN=... IMAP_HOST=imap.example.com IMAP_USER=you@example.com IMAP_PASSWORD=...
export STATE_PATH="$HOME/.billcheck-collector/state.json"   # 默认是 /data/state.json
mkdir -p "$(dirname "$STATE_PATH")"
python3 -m billcheck_collector          # 常驻循环
python3 -m billcheck_collector --once   # 只跑一轮
python3 -m billcheck_collector --check  # 只自检，不上传
```

不想用 `export` 时，可以把变量写进文件（格式同 `docker --env-file`），运行时加 `--env-file 文件路径`；
已存在的环境变量优先于文件里的值。

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `BILLCHECK_TOKEN` | 是 | | 在小程序生成的采集器令牌 |
| `IMAP_HOST` | 是 | | IMAP 服务器地址 |
| `IMAP_USER` | 是 | | 邮箱账号 |
| `IMAP_PASSWORD` | 是 | | 邮箱授权码（不是登录密码） |
| `IMAP_PORT` | 否 | `993` | IMAP 端口 |
| `BILLCHECK_SERVER` | 否 | `https://cc.ivpser.cn` | 服务端地址，必须以 `https://` 开头，否则拒绝启动 |
| `STATE_PATH` | 否 | `/data/state.json` | 状态文件路径 |
| `INTERVAL_SECONDS` | 否 | `3600` | 常驻模式下两轮之间的间隔（秒） |

## 状态文件与重置

`STATE_PATH` 是一个 JSON 文件，记录 IMAP `uidvalidity`、已处理到的 UID 水位线 `watermark`，以及暂缓重试的 `deferred`。
每处理一封邮件原子写入一次。要从头重新扫描，停止采集器后删除该文件（Docker 下是 `/data` 卷里的 `state.json`）即可；
邮箱的 UIDVALIDITY 变化时水位线会自动重置。

## 常见问题

- 退出码 2 并提示「缺少环境变量」或「状态目录不可写」：按提示补齐配置后重启。
- 日志出现 401：令牌已失效或需要在小程序重新确认授权。进程不会退出，处理好后下一轮自动恢复。
- 登录 IMAP 失败或网络错误：记录日志，下一轮重试。

## 许可证

AGPL-3.0，见 `LICENSE`。
