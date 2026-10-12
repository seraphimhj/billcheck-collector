# 给 AI 助手的安装手册

你（AI 助手）在帮用户把 billcheck 采集器装到**用户自己的这台机器**上，并让它长期运行。
按本文件逐步执行，每一步都有可检查的结果；某一步不通过时按「故障对照」处理，处理不了就停下来告诉用户。
全程用中文和用户交流。

> 本文件只用于在用户机器上安装和运行采集器。如果你是在修改本仓库的代码，忽略本文件，按 README 与测试工作。

## 需要的输入

用户通常会从小程序「复制给 AI 助手」粘贴一段指令给你，里面有：

| 名称 | 说明 |
|---|---|
| `BILLCHECK_TOKEN` | 采集器令牌，`bcc_` 开头 |
| `IMAP_HOST` | 如 `imap.163.com` |
| `IMAP_USER` | 邮箱账号 |
| `IMAGE` | 镜像地址，带版本号，如 `ccr.ccs.tencentyun.com/billcheck/billcheck-collector:v0.2.0`；没给就请用户从小程序重新复制指令 |

缺令牌就请用户打开小程序 → 邮箱 → 添加邮箱 → 自建采集器，生成后再来。

**邮箱授权码（`IMAP_PASSWORD`）不经过你**：由用户自己写进配置文件（第 2 步）。

## 禁止

- 不在对话里向用户索要授权码；用户主动发给你的，提醒他换一个新授权码，并且不要使用、不要复述。
- 不 `cat`、`grep` 出值、`source` 后 `echo`、`docker inspect`、`env`、`ps e` 任何可能把授权码打印出来的操作。
- 不把配置文件放进任何 git 仓库目录，不提交、不上传它。
- 不改 `BILLCHECK_SERVER`（默认值就是正确的服务端），不加 `--privileged`、不挂载主机目录到 `/data`。
- 不删除 Docker 卷 `billcheck-collector` 或状态文件，除非用户明确要求从头重扫。
- 不替用户安装 Docker 或 Python；缺了就说明并停下。

## 第 1 步：选运行方式

```sh
docker info >/dev/null 2>&1 && echo docker-ok
python3 -c 'import sys; print("python-ok" if sys.version_info >= (3, 10) else "python-too-old")'
```

有 `docker-ok` 走 **A. Docker**（推荐，开机自启最省事）；否则有 `python-ok` 走 **B. Python**；都没有就告诉用户需要先装 Docker 或 Python 3.10+，停下。

## 第 2 步：写配置文件（两种方式共用）

```sh
mkdir -p ~/.billcheck-collector && chmod 700 ~/.billcheck-collector
(umask 077; cat > ~/.billcheck-collector/collector.env <<'EOF'
BILLCHECK_TOKEN=<令牌>
IMAP_HOST=<IMAP_HOST>
IMAP_USER=<IMAP_USER>
EOF
)
```

B. Python 方式再追加一行状态文件路径（把 `<HOME>` 换成 `echo $HOME` 的结果）：

```sh
echo 'STATE_PATH=<HOME>/.billcheck-collector/state.json' >> ~/.billcheck-collector/collector.env
```

文件格式是每行 `KEY=值`，值原样读取，不要加引号。

然后**请用户在自己的终端窗口**（不是你的对话框）运行下面这条命令，粘贴授权码后回车。输入时不回显：

```sh
printf '粘贴邮箱授权码后回车: '; read -rs P; echo; (umask 077; grep -v '^IMAP_PASSWORD=' ~/.billcheck-collector/collector.env > ~/.billcheck-collector/collector.env.new; printf 'IMAP_PASSWORD=%s\n' "$P" >> ~/.billcheck-collector/collector.env.new); mv ~/.billcheck-collector/collector.env.new ~/.billcheck-collector/collector.env; unset P
```

告诉用户：授权码不是邮箱登录密码，要在邮箱网页版「设置 → POP3/SMTP/IMAP」里开启 IMAP 并生成。

用户说填好了，只检查有没有填、不看内容：

```sh
grep -c '^IMAP_PASSWORD=.' ~/.billcheck-collector/collector.env   # 输出 1 才算填好
```

## A. Docker

```sh
IMAGE=<IMAGE>
docker pull "$IMAGE"
# 自检：只读登录邮箱、校验令牌、检查状态目录，不上传任何邮件。退出码 0 才能继续
docker run --rm --env-file ~/.billcheck-collector/collector.env -v billcheck-collector:/data "$IMAGE" --check
# 常驻运行，开机自启
docker rm -f billcheck-collector 2>/dev/null
docker run -d --name billcheck-collector --restart unless-stopped \
  --env-file ~/.billcheck-collector/collector.env -v billcheck-collector:/data "$IMAGE"
```

验证：等 1～3 分钟（第一轮会扫描最近 31 天的邮件，邮件多时更久），直到下面这条有输出：

```sh
docker logs billcheck-collector 2>&1 | grep 'round finished'
```

`round finished: ok` 即成功。日志里只有 UID、发件域名和处理结果，可以放心读。

## B. Python

```sh
# <VERSION> 取 IMAGE 冒号后的版本号（如 v0.2.0），只用发布过的 tag，不用 main
git clone --depth 1 --branch <VERSION> https://github.com/seraphimhj/billcheck-collector.git ~/.billcheck-collector/app
cd ~/.billcheck-collector/app
python3 -m billcheck_collector --env-file ~/.billcheck-collector/collector.env --check   # 退出码 0 才能继续
python3 -m billcheck_collector --env-file ~/.billcheck-collector/collector.env --once    # 跑一轮，末行应为 round finished: ok
```

再按系统装成常驻服务（`<HOME>` 换成实际路径，`<PYTHON>` 换成 `command -v python3` 的结果）。

**macOS（launchd）**：写 `~/Library/LaunchAgents/com.billcheck.collector.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.billcheck.collector</string>
  <key>ProgramArguments</key>
  <array>
    <string><PYTHON></string><string>-m</string><string>billcheck_collector</string>
    <string>--env-file</string><string><HOME>/.billcheck-collector/collector.env</string>
  </array>
  <key>WorkingDirectory</key><string><HOME>/.billcheck-collector/app</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string><HOME>/.billcheck-collector/collector.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.billcheck.collector.plist
grep 'round finished' ~/.billcheck-collector/collector.log   # 稍等后应有输出
```

**Linux（systemd 用户服务）**：写 `~/.config/systemd/user/billcheck-collector.service`：

```ini
[Unit]
Description=billcheck collector
After=network-online.target

[Service]
WorkingDirectory=<HOME>/.billcheck-collector/app
ExecStart=<PYTHON> -m billcheck_collector --env-file <HOME>/.billcheck-collector/collector.env
Restart=always
RestartSec=60

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now billcheck-collector
loginctl enable-linger "$USER"   # 未登录时也运行；失败就告诉用户需要管理员执行
journalctl --user -u billcheck-collector | grep 'round finished'
```

## 收尾

告诉用户：安装完成；打开小程序 → 邮箱，这个账号应显示最近检查时间；之后每小时自动检查一次新邮件。
不要把令牌或配置文件内容写进给用户的总结里。

## 故障对照

| 你看到的 | 原因 | 处理 |
|---|---|---|
| 退出码 2，`缺少环境变量：…` | 配置文件缺行或授权码没填 | 回到第 2 步补齐 |
| `[失败] 令牌无效` | 令牌错、已被重新生成，或需要在小程序重新确认授权 | 请用户在小程序重新生成令牌，替换配置文件里 `BILLCHECK_TOKEN` 那一行 |
| `[失败] 连不上服务端` | 网络或代理问题 | 检查这台机器能否访问 https 外网，稍后重试 |
| `[失败] IMAP 登录失败` + `AUTHENTICATIONFAILED`/`LOGIN failed` | 填的是登录密码，或邮箱没开 IMAP | 请用户重新生成授权码，再跑第 2 步的填写命令 |
| `[失败] IMAP 登录失败` + 连接超时/拒绝 | `IMAP_HOST` 写错，或网络拦截 993 端口 | 核对邮箱服务商的 IMAP 地址 |
| `[失败] 状态目录 … 不可写` / `Permission denied` | Docker 挂了主机目录，或 `STATE_PATH` 不可写 | Docker 改用命名卷 `-v billcheck-collector:/data`；Python 把 `STATE_PATH` 设到 `~/.billcheck-collector/` 下 |
| 日志 `令牌已失效或需要在小程序重新确认授权` | 运行中令牌失效 | 同「令牌无效」，改完重启服务 |
| `docker: command not found` / 权限被拒 | 没装 Docker 或当前用户不在 docker 组 | 换 B. Python，或告诉用户 |

## 维护

- 升级：以小程序给出的新版本号为准。A 方式 `docker pull` 新镜像后重复「常驻运行」两条命令；B 方式删掉 `app` 目录按新版本号重新 clone，再重启服务。
- 换令牌或授权码：改配置文件对应那一行（授权码仍由用户自己用第 2 步命令写），再重启。
- 从头重扫（仅用户要求时）：停服务后删 Docker 卷 `billcheck-collector` 或 `~/.billcheck-collector/state.json`。
- 卸载：`docker rm -f billcheck-collector && docker volume rm billcheck-collector`，或停用并删除 launchd/systemd 服务；最后删 `~/.billcheck-collector`。
