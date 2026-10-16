# 微助教自动签到

这是一个支持 macOS 和 Linux 的微助教自动签到脚本。程序会定时查询当前活动签到，并使用已验证的学生身份提交签到。

首次使用先执行 `cp config.example.json config.json`，再填写自己的配置。`config.json` 和运行时凭证不会提交到 Git。

## 1. 获取 `student_id`

`student_id` 是微助教接口返回的学生内部 ID，不是微信 OpenID，也不一定等于你的学号。程序会调用：

```text
GET https://v18.teachermate.cn/wechat-api/v2/students/role
请求头：openId: <你的凭证>
```

返回 JSON 中每个学生对象的 `student_id` 就是要填写的值。

例如返回 `[ {"student_id": 8909032, ...} ]`，配置中就填写 `8909032`。

推荐从已登录的微助教网页获取：

1. 用 Chrome 登录微助教网页，打开开发者工具（`⌥⌘I`）。
2. 在 **Network/网络** 中刷新页面，搜索 `students/role`。
3. 打开该请求的 **Response/响应**，找到自己账号对应的 `student_id`，复制数字；如果返回多个学生，选择要签到的那个。

也可以在 Network 请求中复制 `openId` 请求头后查询接口。不要把 `openId`、Cookie 或完整响应发给别人；它们等同于登录凭证。

把数字写入 [config.json](config.json) 的 `auth.expected_student_id`：

```json
{
  "auth": {
    "mode": "http",
    "expected_student_id": 8909032,
    "request_timeout": 15
  }
}
```

程序每次取得或续期凭证都会重新校验这个 ID。ID 填错时会拒绝使用凭证，不会替你尝试其他学生。

支持普通、GPS 定位和二维码三种签到。普通签到直接提交；GPS 使用配置的坐标；二维码订阅对应课堂的实时二维码频道，通过已绑定学生的身份提交。

macOS 和 Linux 均通过微助教 Faye WebSocket 订阅 `/attendance/{courseId}/{signId}/qr`，等待当前任务推送的二维码 URL，不依赖 Chrome 页面或教师网页会话。HTTP 换证模式首次仍需按下面的 `capture` → `inspect` 流程导入 `.runtime/web-identity.json`。未收到二维码或二维码过期时会记录原因并在下一轮重试，不记为成功。

二维码签到成功时，结果地址未带名次则从同一实时频道的学生成功事件读取；仍无名次时，成功推送不显示名次。程序先保存成功记录，再申请一次新的轮询凭证；不把二维码结果地址中的凭证用于后续轮询。换证失败会重试恢复凭证，已成功的签到不会重复提交。

## 2. 安装依赖

项目依赖 macOS 的 Chrome/微信自动化能力和 Python 3.9+：

```bash
cd /Users/mac/Downloads/wzj
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 3. 凭证配置

### 方式 A：HTTP（推荐）

HTTP 模式不需要后台保持微信窗口，但首次需要用 Chrome 登录一次，以便导入已验证的账号身份。

1. 确认 Chrome 中已经登录微助教，并且 `config.json` 的 `expected_student_id` 正确。
2. 保存当前网页会话：

   ```bash
   python web_session.py capture
   ```

   首次运行时允许终端控制 Chrome；如果 Chrome 报 Apple Events JavaScript 错误，在 Chrome 设置中打开“允许 Apple 事件中的 JavaScript”。

   如果提示没有凭证，先执行 `python web_session.py monitor`，然后在 Chrome 中刷新或重新登录，最后执行：

   ```bash
   python web_session.py watch --timeout 180
   ```

3. 校验网页身份，并生成 `.runtime/web-identity.json`：

   ```bash
   python web_session.py inspect
   ```

4. 校验身份和换证接口：

   ```bash
   python main.py --check-auth
   ```

### 方式 B：微信缓存

如果使用微信服务号重新授权，把配置改为：

```json
{
  "auth": {
    "mode": "wechat",
    "expected_student_id": 8909032,
    "wechat_profile": "/绝对路径/到/微信用户配置目录"
  }
}
```

`wechat_profile` 必须是其下存在 `Cache/Cache_Data` 的微信用户配置目录。微信需要保持登录；首次运行 macOS 可能会请求屏幕录制、辅助功能和完全磁盘访问权限。授权后执行：

```bash
python main.py --check-auth
```

## 4. 运行

先做一次只读检查：

```bash
python main.py --once --dry-run
```

确认无误后，前台持续运行：

```bash
python main.py
```

常用参数：

| 命令 | 作用 |
| --- | --- |
| `python main.py --status` | 查看状态，不访问网络 |
| `python main.py --refresh-auth` | 强制重新获取凭证 |
| `python main.py --once` | 检查一次并执行真实签到 |
| `python main.py --verify-recovery` | 测试失效凭证的自动恢复，不提交签到 |
| `python main.py --auth-report` | 查看凭证续期统计 |

`--once` 不带 `--dry-run` 时会真实提交签到，请确认配置正确。

## 5. 安装为 macOS 后台服务

完成上面的身份配置和 `--check-auth` 后执行：

```bash
python service.py install
python service.py status
```

服务管理：

```bash
python service.py start
python service.py stop
python service.py restart
python service.py refresh
python service.py verify-recovery
python service.py uninstall
```

连续 24 小时验收：

```bash
python service.py observe-24h
python service.py verify-recovery
```

`observe-24h` 部署并启动后台，建立 `.runtime/acceptance-24h.json`。你在 Chrome 发起签到后，记录会持续更新三种签到的成功任务、失败尝试、自动续期次数和最长成功轮询间隔。24 小时后记录自动封存为 `complete`，后台签到服务继续运行。再次执行不会重置正在进行的观察窗口。

`verify-recovery` 使用本地无效测试凭证执行真实恢复链路，结果保存在 `.runtime/recovery-test.json`。验收记录分别列出诊断通过和自然失效恢复；自然失效没有发生时不会标为已观察。完整事件记录在 `.runtime/auth-events.jsonl`，其中仅记录凭证指纹。

保持电脑开机、联网和用户会话登录；配置已启用 `caffeinate` 防止空闲休眠。二维码签到需要能够连接微助教的 Faye WebSocket。服务退出时由 macOS 自动重启，机器关机或合盖睡眠期间无法签到。

部署文件会复制到 `~/Library/Application Support/wzj`，运行时凭证、状态和日志在该目录的 `.runtime` 中；后台日志在 `~/Library/Logs/wzj/launcher.log`。

后台运行时会在本机启动只读日志 UI，默认地址为 `http://127.0.0.1:8765/`。页面显示 `.runtime/worker.log` 及轮转日志，支持文件切换、搜索和自动刷新；默认只监听本机，可通过 SSH 隧道或 Nginx 反向代理访问。

修改 `config.json` 后要执行 `python service.py restart`，让部署目录同步最新配置。

同一学生账号应只在一处运行换证服务。本机有进程锁，但无法阻止另一台电脑或服务器给同一账号换证；如果新凭证校验通过后几秒就持续收到 HTTP 401，请检查其他部署是否仍在运行。

### 已部署的 Linux 服务

服务器使用 `wzj.service`，程序在 `/opt/services/wzj/app`，配置在 `/etc/service-config/wzj`，运行记录在 `/var/lib/wzj`。查看和启停：

```bash
sudo systemctl status wzj.service --no-pager
sudo systemctl stop wzj.service
sudo systemctl start wzj.service
```

二维码订阅不使用 `/var/lib/wzj/web-session.json`，也不要求该网页账号拥有课程管理权限。学生凭证续期仍使用已验证的 `web-identity.json`。

## 6. 配置说明

| 配置项 | 说明 |
| --- | --- |
| `interval` | 查询间隔，单位毫秒，默认 `5000` |
| `wait` | 发现签到后提交前等待，单位毫秒 |
| `lat` / `lon` | GPS 签到坐标；只有 GPS 签到才会使用 |
| `auth_check_interval` | 重新校验学生身份的间隔，单位秒 |
| `retry_initial` / `retry_max` | 网络或换证失败时的重试退避，单位秒 |
| `prevent_sleep` | 是否使用 `caffeinate` 防止 macOS 休眠 |
| `auth.identity_file` | HTTP 模式身份文件的绝对路径；省略时使用 `.runtime/web-identity.json` |
| `web.enabled` | 是否启动日志 UI，默认 `true` |
| `web.host` / `web.port` | UI 监听地址和端口，默认 `127.0.0.1:8765` |
| `push.enable` | 是否推送签到事件，当前配置默认开启 |
| `push.service_ip` | 通知中显示的服务 IP；留空时使用本机出口 IP，云端应填写公网 IP |
| `push.xizhi_api` | 息知 `.send` 地址，留空则不推送 |
| `push.ntfy.api` / `push.ntfy.topic` / `push.ntfy.token` | ntfy 服务地址、主题和可选令牌；地址留空则不推送 |

推送配置使用息知地址和独立的 ntfy 主题。发现签到和签到成功各提醒一次；成功通知包含课程、名次（服务返回时）、签到方式、时间和服务 IP。配置、身份、网页会话等需要人工处理的问题会提醒，短暂断连及自动重试不会提醒。只读检查不发送签到提醒。修改推送配置后，按上文重启后台服务。

不要提交 `.runtime`、`credentials.json`、`web-session.json` 或 `web-identity.json`；这些文件包含登录凭证或其等价物。

回归检查（不提交真实签到）：

```bash
python -m unittest -v test_attendance.py
```

## 常见问题

- **`缺少有效的 .runtime/web-identity.json`**：按 HTTP 模式的 `capture` → `inspect` 顺序重新执行。
- **`学生与指定学生不一致`**：重新获取 `student_id`，确认 `config.json` 与当前登录账号属于同一个学生。
- **微信缓存目录不存在或无权限**：确认 `wechat_profile/Cache/Cache_Data` 存在，并给运行 Python 的终端授予完全磁盘访问权限。
- **后台进程未就绪**：先运行 `python service.py status`，再查看 `~/Library/Logs/wzj/launcher.log` 和 `.runtime/worker.log`。
