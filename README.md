# Turb GPT Free Register

ChatGPT / OpenAI 账号注册与 Codex OAuth 授权工具。项目提供本地 Web 管理服务和 CLI 两种入口，注册驱动、邮箱来源和浏览器自动化实现均以独立模块组织；运行时业务数据统一保存在 SQLite。

> 本项目仅适用于你拥有合法授权的测试、研发和自动化场景。使用前请确认符合目标服务的服务条款、当地法律和第三方服务商的使用政策。

## 1. 项目简介

### 1.1 项目定位

项目解决的是一套本地注册任务的编排问题：准备邮箱素材，选择注册驱动，执行邮箱/手机验证，保存账号状态，并按需继续完成 Codex OAuth。管理服务和注册执行服务相互独立：

- **Web 管理服务**：Flask 后端位于 `apps/web/`，负责任务编排、配置编辑和 API；React/Vite 前端位于 `web/`。
- **注册模拟服务**：注册用例位于 `registration/application/`，浏览器和协议适配器位于 `registration/drivers/`，可单独替换或扩展。
- **基础业务模块**：`core/` 提供邮箱、短信、OAuth、浏览器客户端和业务数据访问能力。
- **唯一运行时数据源**：`data/turb_gpt.sqlite3` 保存账号、邮箱池、任务、凭证、日志和批次归档。

项目基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 改造，并已将历史 JSON/TXT/日志镜像收敛到 SQLite。历史文件仍可作为一次性导入输入，但正常运行不会在项目目录生成散落数据文件。

### 1.2 运行入口

| 入口 | 适用场景 | 启动命令 |
|---|---|---|
| WebUI | 日常配置、批量任务、账号和日志管理 | `./webui.sh start` |
| CLI | 自动化脚本、无界面批量任务 | `uv run --locked python main.py` |
| Codex 补跑 | 对已注册账号单独重试 OAuth | `uv run --locked python scripts/operations/codex_oauth.py ...` |

生产和开发环境使用同一组固定地址：浏览器访问 Vite 前端
`http://127.0.0.1:5555`，Flask 后端监听 `http://127.0.0.1:6666`。Vite
dev/preview 将 `/api` 代理到后端。WebUI 仅绑定本机回环地址，不再要求授权码登录。

## 2. 功能概览

### 2.1 注册驱动

注册驱动可在 WebUI「设置 → 浏览器与网络 → 代理浏览器 → 总览与驱动」中选择（底层配置键为 `REGISTRATION_DRIVER`），当前支持六种实现：

| 驱动 | 说明 | 典型依赖 |
|---|---|---|
| `protocol` | `curl_cffi` 协议流程，结合 Sentinel/PoW | 代理、协议客户端 |
| `roxy` | RoxyBrowser 指纹浏览器 + Selenium | 本机 RoxyBrowser API |
| `cloak` | CloakBrowser + Playwright 适配层 | Cloak binary，支持免费版 |
| `browser_use` | Browser Use Cloud stealth Chromium | `BROWSER_USE_API_KEY` |
| `skyvern` | Skyvern Browser Sessions 云端浏览器 | `SKYVERN_API_KEY` |
| `chrome_cdp` | 正常启动本机 Google Chrome，并通过 CDP 接管；本地推荐 | 系统 Google Chrome |

浏览器驱动通过 `registration/drivers/registry.py` 注册，业务用例不直接依赖某个浏览器实现。这样可以为不同环境切换驱动，也可以在不改动 WebUI 和任务服务的情况下添加新驱动。

未开启 2FA 的 Roxy 注册保留邮箱 OTP 优先的流程；只有未提供 OTP 入口且必须创建密码时，才会使用设置中的备用密码，或为当前账号生成独立随机密码。普通 OTP 注册记录为 `email_otp`，`protocol` 驱动始终是 OTP-only。

Roxy 开启 `ENABLE_2FA` 后会明确选择密码注册，在当前 Roxy 浏览器内完成 2FA 设置，再清理登录态并执行一次全新的“密码 + TOTP”登录。必须实际提交密码、完成 TOTP 挑战，并取得邮箱匹配且包含 `accessToken` 的新会话，才保存为成功账号；不会为了这一验证另开系统 Chrome，也不会把 OTP 登录或旧会话误记为密码验证成功。此清理会影响该 Profile 的登录态，应使用专用注册 Profile。

### 2.2 邮箱与验证码

WebUI「设置 → 邮箱设置」中的 `EMAIL_SOURCE` 支持多个来源组合：

- **Outlook**：专用素材格式为 `email----password----clientId----refreshToken`。
- **通用 API**：按内容提取邮箱和 `http://` / `https://` 接码地址。
- **GPT账号导入**：导入器不要求固定顺序；邮箱、接码地址、Base32/TOTP 2FA（通常 16-64 个字符）按内容识别，剩余的单个字段作为密码。
- **GPTMail**：运行时创建临时邮箱并通过 API 取码。
- **Cloudflare Worker**：使用 Worker API 创建地址并轮询验证码，标识为 `cloudflare`。
- **Cloudflare 域名邮箱**：本地生成地址，通过 QQ IMAP 收信，标识为 `cloudflare_domain`。
- **MailNest / CloudMail**：按对应服务的 API 配置获取临时邮箱。

动态邮箱来源在 WebUI「设置」中配置；“邮箱”工作区只显示手动导入的 Outlook/通用 API 素材。导入器支持 `---`、`----`、`|`、`====`，并能保留接码 URL 内部出现的分隔符。分隔符可在 WebUI「设置 → 邮箱设置 → 导入分隔符」中设置，多个分隔符用英文逗号分隔，默认值为 `---,----,|,====`。导入后，邮箱池正文存储在 SQLite，不依赖根目录文本文件持续运行。

### 2.3 Codex OAuth 与接码

- GPT 注册与 Codex OAuth 完全分离：注册完成后账号状态为“未授权”，只有在 GPT账号 页面显式点击授权才会启动 Codex OAuth。
- 授权驱动支持 `protocol`、`roxy`、`cloak`、`chrome_cdp`、`browser_use`、`skyvern` 和 `same_as_registration`。
- 支持 CPA 管理接口或本地 PKCE 授权地址。
- 手机验证支持 GrizzlySMS、本地 L 服务和 H 服务。GPT账号授权任务只有在实际进入手机验证页面时才从手机号池取号；未触发短信验证不会占用号码或可用次数。
- Codex 凭证、授权回执和重试日志写入 SQLite 的 `codex_credentials`、`codex_retry_logs` 等文件类别。

### 2.4 WebUI 管理能力

React WebUI 在生产环境由 Vite preview 提供，Flask 后端负责 API；主导航合并为四个菜单：

- **邮箱**：邮箱注册工作区的预留入口，目前暂为空；后续使用邮箱完成 ChatGPT 注册后，生成的账号会注入 GPT账号流程。仅创建邮箱本身不会产生 GPT 账号。
- **GPT账号**：同一张账号表承载账号导入、GPT 注册、Codex OAuth 认证、GPT/邮箱验活、限额查询、凭证导出和 Sub2API 同步。表格同时展示明文密码、2FA、邮箱接码 API、GPT 注册状态、Codex 授权状态、手机接码状态、已验证手机、GPT 状态、套餐、备注、创建/修改时间、操作和日志；任务仍在后台执行，不再单独展示任务列表。需要短信时系统自动从手机号池调度。
- **手机号池**：导入和维护接码手机号、接码地址、可用次数、绑定状态，并删除失效或无用号码。
- **设置**：承接原来的运行配置，统一管理邮箱来源、注册/浏览器驱动、代理、Codex、短信和第三方 API 参数。

注册模拟服务仍由后端任务和 CLI 提供，不再作为独立的 WebUI 一级菜单；浏览器驱动仍可拆卸替换。

### 2.5 数据与安全特性

- SQLite 是运行时唯一事实来源，数据库文件默认位于 `data/turb_gpt.sqlite3`，权限按 `0600` 处理。
- 项目不再生成 `accounts/`、`codex_accounts/`、`logs/`、`run/`、`注册日志/` 或 `codex_接码日志/` 等旧物理目录。
- GPT账号表按当前运维需求直接展示密码和 TOTP/2FA；access token、refresh token 等更高风险凭证仍只在复制或下载动作中按需读取。WebUI 不设登录门禁，请只在受控的本机账户上使用，并妥善保护数据库。
- `.env`、数据库、凭证和运行时文件均被 `.gitignore` 排除，禁止提交到仓库。

## 3. 使用指南

### 3.1 环境准备

要求：

- Python 3.10 或更高版本。
- [uv](https://docs.astral.sh/uv/)（用于管理 Python 解释器、虚拟环境和依赖）。
- Node.js `20.19.0+`（20.x）或 `22.12.0+`（当前锁定的 Vite 8.2.2 要求；仅前端开发/构建需要）。
- 可用的网络出口和代理；使用云端浏览器时还需要对应服务的 API Key。
- 使用 RoxyBrowser 时，本机 Roxy API 必须可访问。

`webui.sh` 是面向 macOS/Linux 的 Unix shell 管理脚本；Windows 用户请使用下面的
`uv run` 前台启动命令（或在 WSL 中运行管理脚本）。

安装 uv（如果尚未安装）：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

项目根目录的 `.python-version` 固定默认解释器系列为 Python 3.10；uv 会优先使用自己下载和维护的解释器，并在本机没有匹配版本时自动下载。在项目根目录同步锁定依赖（会自动创建或更新 `.venv`）：

```bash
uv sync --locked
```

日常命令通过 `uv run --locked` 执行，不需要手工激活虚拟环境。例如：

```bash
uv run --locked python -c "import sys; print(sys.executable)"
```

`uv.lock` 是可复现安装所需的锁文件，应与项目源码一起提交。若维护者修改了依赖，先运行 `uv lock` 更新锁文件，再用 `uv sync --locked` 验证；普通使用者不要删除或手工编辑 `uv.lock`。仓库保留的 `requirements.txt` 仅用于兼容旧版 pip 部署，新依赖只应维护在 `pyproject.toml` 中。

日常启动不要求手工创建或编辑 `.env`。首次启动后，打开 WebUI 的“设置”页面即可填写邮箱、代理、浏览器、Codex、短信和第三方 API；保存时系统会自动把配置写入项目根目录 `.env` 并热加载。不要把真实密钥写入 `config/*.py`、README、Issue 或提交记录。

`.env.example` 仅作为自动化部署或高级用户的模板，不是日常配置入口。只有少数启动级选项需要在启动前通过环境变量设置，例如数据库路径：

```dotenv
TURB_SQLITE_PATH=/path/to/turb_gpt.sqlite3
```

如果需要无人值守启动，可以预先准备 `.env`；交互式使用不需要这样做。

### 3.2 邮箱菜单与邮箱素材

“邮箱”菜单用于维护手动导入的邮箱素材；注册任务在“GPT账号 → 注册新账号”中临时选择邮箱池或其他已配置来源。邮箱分隔符在“设置 → 邮箱设置”中配置；已有 ChatGPT 账号和接码链接则从“GPT账号”菜单导入。也可以复制示例文件作为一次性导入输入：

```bash
cp 用于注册的邮箱.txt.example 用于注册的邮箱.txt
```

Outlook 素材示例：

```text
email@example.com----mail-password----client-id----refresh-token
```

通用 API 素材示例：

```text
email@example.com----https://mail.example.com/code?id=...
```

GPT账号也可以按内容乱序粘贴，密码和 2FA 为可选字段：

```text
密码|JBSWY3DPEHPK3PXP|email@example.com|https://mail.example.com/code?id=...
```

Markdown 链接（`[说明](https://...)`）和聊天复制产生的反斜杠转义也会自动清理。接码 URL 查询参数或路径中如果包含 `----` 等分隔符，且没有其他可识别字段，解析器会将其视为 URL 内容保留。

在 WebUI「设置 → 邮箱设置」中设置 `EMAIL_SOURCE`，可填一个来源或用英文逗号分隔多个来源作为兜底。GPTMail、Cloudflare、Cloudflare 域名邮箱、MailNest 和 CloudMail 的密钥、地址、项目代码等字段也都在设置页填写；点击顶部“保存”后会写入 `.env` 并热加载。`config/email.py` 只用于查看默认值和开发维护，不需要手工编辑。

### 3.3 选择注册驱动

进入 WebUI「设置 → 浏览器与网络 → 代理浏览器 → 总览与驱动」，选择 `chrome_cdp`、`protocol`、`roxy`、`cloak`、`browser_use` 或 `skyvern`。各浏览器的 API 地址、工作区、无头、代理、GeoIP、语言、时区和超时设置在“代理浏览器”顶部的对应模块中配置；云浏览器的 API Key 也在各自模块填写。Roxy 的团队/项目可以使用配置页中的“获取团队”工具读取并保存。

`config/*.py` 中仍保留安全默认值，供 CLI 和开发环境使用，但不建议为了日常运行直接编辑源码。修改配置页后，新的任务会读取热加载后的值；已经运行中的浏览器任务不会被强行改写。

### 3.4 配置代理和 Codex

注册与授权的网络优先级统一为：**任务/账号显式代理 → 代理池 → 本机网络**。未配置代理时不再默认连接 `127.0.0.1:7897`；显式空字符串表示本机直连，已配置但无效的代理不会静默切换出口。

Roxy 固定 Profile 会在关闭状态下同步本次任务的网络设置（含清除旧代理）；已打开且网络不一致时会提示先关闭，不强制重启用户窗口。浏览器、PKCE 换 Token、2FA 和授权后验活保持同一路由；`ROXY_CREATE_USE_PROXY_POOL` 仅保留兼容，不再影响此策略。

成功授权后会私下保存已核验的网络线路（包含明确直连），后续凭证刷新与 GPT 验活沿用该线路，不依赖临时 Profile 继续存在，也不重新随机抽取代理。该内部字段不会在账号列表接口中返回；更改账号显式代理时会清除旧线路记录。旧账号若只有 Profile 信息而无法读回网络配置，会明确报错，不猜测为直连。

在 WebUI「设置 → 代理池」中逐行填写代理 URL，并设置套餐查询的独立网络模式、超时和重试策略。代理包含用户名或密码时也建议只通过配置页保存，系统会将其写入 `.env`。

在「设置 → Codex」中选择授权地址来源、OAuth 驱动以及 CPA/sub2API 参数。接码配置位于「设置 → 手机接码」区域，进入后通过顶部横向选项切换“总览与开关 / GrizzlySMS / L 接码服务 / H 接码服务”。开启“加入手机号池”后，当前选中的平台会在“手机号池”顶部显示为特殊的动态来源，GPT账号授权遇到手机验证时会自动向该平台取号；关闭后只使用手工导入的手机号。旧的全局固定号码配置不会参与 WebUI 授权。接口语义可参考 [L_API.md](L_API.md)。

### 3.5 启动 WebUI

推荐使用根目录管理脚本：

```bash
./webui.sh start
./webui.sh status
./webui.sh logs
./webui.sh restart
./webui.sh stop
```

服务使用两个固定监听器：Vite 前端为 <http://127.0.0.1:5555>，Flask
后端为 <http://127.0.0.1:6666>。浏览器只需访问前端；Vite 会把 API
请求代理到后端。同一工作区只允许一组 WebUI 前后端进程，不能通过参数改成其他端口。

首次使用的完整流程：

1. 执行 `./webui.sh start`。
2. 打开前端 <http://127.0.0.1:5555>，直接进入工作区。
3. 进入“设置”，填写邮箱来源、注册/浏览器驱动、代理、API Key、Codex 和其他第三方服务参数；如需平台自动取号，在「手机接码」中通过顶部选项切换平台、填写凭证并开启“加入手机号池”，点击顶部“保存”。
4. 如果不使用自动取号，进入“手机号池”导入手机号和接码链接，并确认可用次数大于 0；开启平台动态来源时无需预先导入固定号码。
5. 进入“GPT账号”，导入已有账号或查看注册服务生成的账号。对未注册行点击“注册”，对已有账号点击“授权”；短信验证时系统自动从手机号池取号。

因此，普通使用者不需要在启动前编辑 `.env` 或修改 `config/*.py`。数据库位置 `TURB_SQLITE_PATH` 和固定监听地址属于启动级设置，仍需在进程启动前准备。

可选环境变量：

```bash
OPEN_BROWSER=1 ./webui.sh start
VERBOSE=1 ./webui.sh start
```

也可以前台启动：

```bash
uv run --locked python web.py
```

这条命令只启动 `127.0.0.1:6666` 上的 Flask 后端；前端仍需在 `web/`
目录执行 `npm run dev` 或 `npm run preview`，并通过
`http://127.0.0.1:5555` 访问。两种 Vite 模式都会把 API 请求代理到
`6666`。Unix 环境下启动前请先执行 `./webui.sh status`，避免多个进程同时
读写同一份数据。

### 3.6 GPT账号注册、授权与手机号池资源

GPT账号表中的注册和授权按钮作用于同一行。注册按钮会把该行的邮箱接码 API 或 Outlook 凭证准备到对应邮箱池，并创建带目标邮箱的注册任务；授权按钮使用该行的 Relay 账号资料。后台任务和完整日志仍保留在 SQLite，但界面只在账号行投影当前操作和最近日志。

注册任务要完成 ChatGPT/OpenAI 注册，至少需要以下资源：

- **邮箱身份**：邮箱地址，以及可读取验证码的 HTTP 接码 API；或者 Outlook 的邮箱密码、Client ID 和 Refresh Token。只有邮箱地址而没有收码能力时，自动注册无法取得邮箱验证码。
- **注册资料**：设置中的注册驱动（`chrome_cdp`、`protocol`、`roxy`、`cloak`、`browser_use` 或 `skyvern`）、代理/浏览器参数，以及注册流程需要的显示名和生日生成配置。
- **可选 2FA**：启用自动 2FA 时需要允许账号完成二次验证；已有账号导入可直接提供 Base32/TOTP 密钥。

Codex 授权要完成 OAuth 登录和回调，至少需要：

- GPT账号表中的 ChatGPT 密码；如果账号启用了 2FA，还需要 TOTP 密钥；
- “设置”中的 Codex OAuth/CPA/sub2API、浏览器驱动和代理配置；
- “手机号池”中的号码与取码地址。授权启动不会预先检查或占用手机号；只有流程实际触发短信验证时，系统才从手机号池分配号码并在验证成功后扣减可用次数。

手机号池支持批量 `+1` / `-1`，也可以直接在表格的“可用次数”列选择精确次数（沿用 `0–3` 次范围，选择后自动保存），或勾选多个号码后“批量设置”。手机号表格不再提供“操作”列；点击“期限”列中的“不过期”或截止时间即可单独维护过期时间。期限默认**不过期**，可以选择“按天设置”，从保存时起计算 `N × 24` 小时，也可重新设为“不过期”。只修改次数时保留原过期时间，只修改期限时保留原可用次数；重复导入号码也不会清除已有期限。表格显示截止时间和向上取整的剩余天数，支持“已过期”筛选。

到期号码不再参与短信分配，剩余次数和已绑定账号记录不会被删除；增加次数不会解除过期或失效状态。任务占用中的号码不能减少次数或修改有效期，避免影响正在接码的任务。动态接码平台由平台管理，不提供固定号码的次数/过期设置。

API：`PATCH /api/phones/settings`，传入 `phone_ids` 数组（单个或多个），以及可选的 `available_uses`、`expiration_days`。省略字段表示不修改；`expiration_days: 0` 表示不过期，正整数表示从保存时起的天数。旧的 `PATCH /api/phones/available-uses` 增减接口保持兼容。

账号表的三个状态筛选分别表示：

- GPT 注册：`已注册`、`未注册`、`注册中`、`注册失败`；
- Codex 授权：`已授权`、`未授权`、`授权中`、`授权失败`；
- 手机接码：`已接码`、`未接码`、`接码中`、`接码失败`。

未成功验证的候选手机号不会写入“手机”列；成功后才以 `手机号码----手机接码API` 展示。验证码等待、浏览器人工协助、停止和失败原因通过账号行的“操作”和“日志”按钮处理。

### 3.7 使用 CLI

注册一个账号：

```bash
uv run --locked python main.py
```

批量注册 10 个账号、3 个并发线程，并在单个失败后继续：

```bash
uv run --locked python main.py -n 10 --workers 3 --continue-on-fail
```

查看详细日志：

```bash
uv run --locked python main.py -n 1 --verbose
```

常用参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-n, --count` | 注册数量 | `1` |
| `--workers` | 并发线程数 | `1` |
| `--delay` | 注册之间的间隔秒数 | `0` |
| `--continue-on-fail` | 失败后继续 | 关闭 |
| `--verbose` | 输出详细日志和错误堆栈 | 关闭 |

CLI 批次归档也写入 SQLite；返回的 `accounts/...` 路径是稳定的逻辑键，不代表项目中会创建同名目录。

### 3.8 Codex 补跑、查活和数据读取

对已注册账号单独补跑 Codex：

```bash
uv run --locked python scripts/operations/codex_oauth.py --email <已注册邮箱> --verbose
```

协议接口诊断（不会把 token 写入文件）：

```bash
uv run --locked python scripts/diagnostics/chatgpt_curl_cffi.py --token '<JWT>' --verbose
```

分析脱敏后的 HAR 摘要：

```bash
uv run --locked python scripts/diagnostics/analyze_har_protocol.py /path/to/capture.har -o /tmp/protocol-summary.json
```

无需启动 WebUI，也可以只读查询 SQLite：

```bash
sqlite3 data/turb_gpt.sqlite3 '.tables'
sqlite3 data/turb_gpt.sqlite3 "select json_extract(value_json, '$.id'), json_extract(value_json, '$.email') from storage_items where collection='gpt_accounts' order by position;"
sqlite3 data/turb_gpt.sqlite3 'select collection, count(*) from storage_items group by collection;'
```

Python 读取示例：

```python
from core.sqlite_store import connect

with connect() as conn:
    rows = conn.execute(
        "select json_extract(value_json, '$.id') as id, "
        "json_extract(value_json, '$.email') as email "
        "from storage_items where collection='gpt_accounts' order by position"
    ).fetchall()
```

可以用 `TURB_SQLITE_PATH=/path/to/turb_gpt.sqlite3` 指定数据库位置。导出账号、token 或凭证时，使用 WebUI 下载接口或显式导出工具，并由调用方指定目标路径；不要把导出文件放回仓库根目录。

### 3.9 常见问题

**配置保存后没有生效？** WebUI 保存的设置会写入 `.env` 并立即热加载；已经运行中的任务会继续使用启动时读取的值，新的任务才会使用新值。直接修改 `config/*.py` 只影响默认值，通常需要重启 CLI 或 WebUI 才能重新读取。

**邮箱页面为什么是空的？** “邮箱”只显示尚未注册 GPT 的邮箱账号。账号完成注册后会从该列表移除，显示在“GPT账号”页面；若未导入邮箱账号，列表也是空的。

**没有手机号池资源能否注册？** 可以。GPT 注册不读取手机号池，也不会自动开始 Codex 授权；注册完成后会显示“未授权”。

**注册后邮箱失效还能登录吗？** 不一定。未开启 2FA 的 Roxy 注册仍优先使用邮箱 OTP，不能假设每个账号都有密码。开启 2FA 的 Roxy 成功账号需要通过同浏览器的全新密码 + TOTP 登录验证；这不保证服务之后不会再次要求邮箱验证。请保留可用的邮箱接码渠道，后续授权或验活仍应以实际页面要求为准。

**GPT账号授权如何准备手机接码？** 可以使用手机号池中的手工号码，也可以在「设置 → 手机接码」顶部的“总览与开关”中开启一个动态平台来源。授权任务启动时不会预留号码；只有实际进入手机验证页面时，系统才取号。动态平台的余额和实时可用号码由平台在实际取号时返回，若平台余额不足或无号，任务会记录失败原因。未实际触发短信验证的手工号码不会扣减次数。

**Codex 失败但注册成功怎么办？** 账号会保留并在 GPT账号表显示“已注册 + 授权失败”，可直接点击该行“授权”重新补跑，或执行上面的 Codex CLI 命令。

**仍然出现代理或 SSL 错误？** 先区分网络连接失败与账号状态：日志会保留 `ERR_TUNNEL_CONNECTION_FAILED` / `ERR_SSL_PROTOCOL_ERROR` 等原始错误码，不把它们归类为账号停用。固定 Profile 若提示网络不一致，请先关闭对应 Roxy 窗口再重试，让程序同步本次任务的代理；不要用关闭 TLS 校验或静默直连掩盖问题。页面、认证响应明确返回删除/停用时，则优先报告账号原因，不再仅以 MFA 页面未跳转判定超时。

**与初版有哪些关键差异？** 参见 [初版对比与 Roxy 修复记录](docs/architecture/roxy-baseline-repair-2026-10-02.md)，其中区分已提交代码、未提交工作树、代码回归测试和真实网络验证。初版 Roxy 会跳过 2FA，不能用其“注册成功”证明 2FA 已完成。

**Roxy 无头模式仍弹出窗口？** 检查 `ROXY_OPEN_HEADLESS = True`，并确认本机 RoxyBrowser 版本支持对应 API 参数。

**Cloudflare Worker 和域名邮箱有什么区别？** `cloudflare` 使用 Worker API 创建地址并取码；`cloudflare_domain` 使用域名转发到 QQ IMAP，二者配置和收信链路不同，不能混用。

## 4. 开发指南

### 4.1 工程结构

```text
.
├── main.py                         # CLI 兼容入口
├── web.py                          # WebUI 启动入口
├── webui.sh                        # 单实例 WebUI 管理脚本
├── pyproject.toml                  # 项目元数据和依赖声明（uv）
├── uv.lock                         # 锁定的可复现依赖解析结果
├── .python-version                 # uv 使用的 Python 版本系列
├── apps/
│   ├── cli/main.py                 # CLI 参数和批处理编排
│   └── web/                        # Flask 管理服务、任务 API、配置编辑
├── config/                         # 可热加载的配置模块
├── core/                           # 邮箱、OAuth、浏览器客户端、SQLite 数据访问
├── registration/
│   ├── application/                # 注册用例、任务服务、领域模型
│   ├── drivers/                    # protocol/Roxy/Cloak/Browser Use/Skyvern 适配器
│   └── ports/                      # 与具体驱动无关的端口
├── web/                            # React/Vite 前端、源码和 dist 构建产物
├── sentinel/                       # Sentinel JavaScript 运行组件
├── scripts/
│   ├── operations/                 # 运维和 Codex 补跑脚本
│   └── diagnostics/                # 协议、接口和 HAR 诊断脚本
├── tests/                          # pytest 测试
├── docs/                           # 架构和历史决策文档
├── data/turb_gpt.sqlite3           # 本地运行时数据，不提交
└── L_API.md                        # 本地 L 接码接口说明
```

### 4.2 模块边界

- `apps/web` 只负责 HTTP、任务入口和展示所需的数据整形，不直接实现浏览器步骤。
- `registration/application` 负责注册任务生命周期；`registration/drivers` 通过统一接口实现具体浏览器或协议流程。
- `core` 提供可复用客户端和业务服务；`core/db.py` 是业务数据访问层，`core/sqlite_store.py` 是通用 SQLite 存储适配器。
- Web 管理服务和 CLI 都调用注册应用层，避免为两个入口复制注册逻辑。
- 浏览器驱动之间不共享具体实现；新增驱动时优先新增 `registration/drivers/<name>/`，并在 registry 中注册。

### 4.3 前端开发

安装并启动 Vite 开发服务器：

```bash
cd web
npm ci
npm run dev
```

Vite dev 固定运行在 `http://127.0.0.1:5555`，将 `/api`
代理到运行中的 Flask 后端 `http://127.0.0.1:6666`。生产构建和
本地预览：

```bash
npm run build
npm run preview
```

Vite preview 同样固定运行在 `http://127.0.0.1:5555`，并使用相同代理访问
`6666` 后端。`web/dist/` 是 preview 使用的构建产物；`web/node_modules/`
只属于本地开发环境，不应提交。

### 4.4 测试与质量检查

在项目根目录执行：

```bash
uv sync --locked
PYTHONDONTWRITEBYTECODE=1 uv run --locked pytest -q
bash -n webui.sh
git diff --check
```

测试使用 `pytest.ini` 中的 `tests/` 路径。涉及 SQLite 的测试会使用临时数据库，不应修改本地真实 `data/turb_gpt.sqlite3`。

提交前至少确认：

1. 未提交 `.env`、数据库、token、凭证或导出文件。
2. 新增功能有对应测试或清晰的手工验证步骤。
3. WebUI 仍只使用前端 `127.0.0.1:5555` 和后端 `127.0.0.1:6666`，没有启动第二组生产实例。
4. 浏览器驱动能通过统一注册接口调用，未把驱动细节泄漏到 Web/API 层。

### 4.5 SQLite 数据约定

`core/sqlite_store.py` 提供 JSON 集合和文件内容的统一读写 API：

- `storage_collections` / `storage_items` 保存结构化业务数据。
- `storage_files` 保存日志、凭证、批次归档等二进制或文本内容。
- 业务实体只有 `email_accounts`（尚未注册的邮箱）、`gpt_accounts`（已注册 GPT 账号）和 `phones`（手机号）；邮箱来源和账号授权状态均为实体字段。
- `registration_jobs`、`relay_jobs`、`relay_sub2_services` 和导出状态是任务及集成记录，不是另一套账号或手机号。
- 只读写当前 SQLite 集合，不生成旧 JSON/TXT 文件镜像或旧查询视图；导出由显式下载接口处理。

新增业务数据时复用这三个实体及现有任务记录，不要重新引入按来源拆分的账号集合、根目录 JSON、日志目录或静态 HTML 快照。

### 4.6 贡献流程

1. 从 `main` 创建功能分支。
2. 只修改与目标相关的模块，并补充测试和文档。
3. 本地运行测试、前端构建和 `git diff --check`。
4. 提交信息使用清晰的动词短语，提交前检查 `git status`，确认没有敏感文件。
5. Pull Request 中说明行为变化、数据影响、验证命令和已知限制。

## 5. 开源协议

本项目采用 [MIT License](LICENSE)。除许可证正文要求外，使用者还需要自行遵守：

- ChatGPT/OpenAI、邮箱、短信、浏览器云服务和代理服务的服务条款；
- 账号注册、自动化访问、数据保存和跨境传输适用的法律法规；
- 目标网络、代理和第三方 API 的授权范围。

项目按“现状”提供，不对第三方服务可用性、账号状态、注册成功率或外部接口兼容性作保证。第三方服务的密钥、账号和网络资源由使用者自行负责。

Copyright (c) 2026 Turb GPT Free Register contributors. 详见 [LICENSE](LICENSE)。
