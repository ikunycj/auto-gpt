# Turb GPT Free Register

ChatGPT / OpenAI 账号注册与 Codex OAuth 授权工具。项目提供本地 Web 管理服务和 CLI 两种入口，注册驱动、邮箱来源和浏览器自动化实现均以独立模块组织；运行时业务数据统一保存在 SQLite。

> 本项目仅适用于你拥有合法授权的测试、研发和自动化场景。使用前请确认符合目标服务的服务条款、当地法律和第三方服务商的使用政策。

## 1. 项目简介

### 1.1 项目定位

项目解决的是一套本地注册任务的编排问题：准备邮箱素材，选择注册驱动，执行邮箱/手机验证，保存账号状态，并按需继续完成 Codex OAuth。管理服务和注册执行服务相互独立：

- **Web 管理服务**：Flask 后端位于 `apps/web/`，负责鉴权、任务编排、配置编辑和 API；React/Vite 前端位于 `web/`。
- **注册模拟服务**：注册用例位于 `registration/application/`，浏览器和协议适配器位于 `registration/drivers/`，可单独替换或扩展。
- **基础业务模块**：`core/` 提供邮箱、短信、OAuth、浏览器客户端和业务数据访问能力。
- **唯一运行时数据源**：`data/turb_gpt.sqlite3` 保存账号、邮箱池、任务、凭证、日志和批次归档。

项目基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 改造，并已将历史 JSON/TXT/日志镜像收敛到 SQLite。历史文件仍可作为一次性导入输入，但正常运行不会在项目目录生成散落数据文件。

### 1.2 运行入口

| 入口 | 适用场景 | 启动命令 |
|---|---|---|
| WebUI | 日常配置、批量任务、账号和日志管理 | `./webui.sh start` |
| CLI | 自动化脚本、无界面批量任务 | `python main.py` |
| Codex 补跑 | 对已注册账号单独重试 OAuth | `python scripts/operations/codex_oauth.py ...` |

生产 WebUI 只使用 `http://127.0.0.1:5000` 一个监听器。开发前端使用 Vite 的 `5173` 端口，并将 API 代理到 `5000`。

## 2. 功能概览

### 2.1 注册驱动

注册驱动通过 `config/roxybrowser.py` 的 `REGISTRATION_DRIVER` 选择，当前支持五种实现：

| 驱动 | 说明 | 典型依赖 |
|---|---|---|
| `protocol` | `curl_cffi` 协议流程，结合 Sentinel/PoW | 代理、协议客户端 |
| `roxy` | RoxyBrowser 指纹浏览器 + Selenium | 本机 RoxyBrowser API |
| `cloak` | CloakBrowser + Playwright 适配层 | Cloak binary，支持免费版 |
| `browser_use` | Browser Use Cloud stealth Chromium | `BROWSER_USE_API_KEY` |
| `skyvern` | Skyvern Browser Sessions 云端浏览器 | `SKYVERN_API_KEY` |

浏览器驱动通过 `registration/drivers/registry.py` 注册，业务用例不直接依赖某个浏览器实现。这样可以为不同环境切换驱动，也可以在不改动 WebUI 和任务服务的情况下添加新驱动。

### 2.2 邮箱与验证码

`config/email.py` 中的 `EMAIL_SOURCE` 支持多个来源组合：

- **Outlook**：素材格式为 `email----password----clientId----refreshToken`。
- **通用 API**：素材格式为 `email----code_url`。
- **GPTMail**：运行时创建临时邮箱并通过 API 取码。
- **Cloudflare Worker**：使用 Worker API 创建地址并轮询验证码，标识为 `cloudflare`。
- **Cloudflare 域名邮箱**：本地生成地址，通过 QQ IMAP 收信，标识为 `cloudflare_domain`。
- **MailNest / CloudMail**：按对应服务的 API 配置获取临时邮箱。

邮箱素材可以从 WebUI 导入，也可以显式指定历史文本作为一次性导入入口。导入后，邮箱池正文存储在 SQLite，不依赖根目录文本文件持续运行。

### 2.3 Codex OAuth 与接码

- 注册完成后可选自动执行 Codex OAuth：`ENABLE_CODEX_AUTO = True`。
- 授权驱动支持 `protocol`、`roxy`、`cloak`、`browser_use`、`skyvern` 和 `same_as_registration`。
- 支持 CPA 管理接口或本地 PKCE 授权地址。
- 手机验证支持 GrizzlySMS、本地 L 服务和 H 服务，可执行取号、发送、收码、提交和失败重试。
- Codex 凭证、授权回执和重试日志写入 SQLite 的 `codex_credentials`、`codex_retry_logs` 等文件类别。

### 2.4 WebUI 管理能力

React WebUI 由 Flask 在生产环境提供，主要页面包括：

- **注册**：设置数量和线程，启动任务并查看实时日志。
- **账号**：查看状态、备注、套餐、查活、归档、批量删除和敏感信息复制。
- **Codex 授权**：查看、下载、删除 SQLite 中的凭证，并执行补跑。
- **邮箱池**：按来源和状态筛选，导入、标记可用/已用/失败和删除。
- **配置**：编辑邮箱、浏览器、代理、Codex、短信和人工节奏配置，并热加载常用设置。
- **Relay**：管理接码账号、手机号、任务、Sub2API 同步和状态检查。

### 2.5 数据与安全特性

- SQLite 是运行时唯一事实来源，数据库文件默认位于 `data/turb_gpt.sqlite3`，权限按 `0600` 处理。
- 项目不再生成 `accounts/`、`codex_accounts/`、`logs/`、`run/`、`注册日志/` 或 `codex_接码日志/` 等旧物理目录。
- WebUI 列表接口默认不返回完整 token、TOTP secret 等敏感字段，下载或复制时按需读取。
- `.env`、数据库、凭证和运行时文件均被 `.gitignore` 排除，禁止提交到仓库。

## 3. 使用指南

### 3.1 环境准备

要求：

- Python 3.10 或更高版本。
- Node.js 18 或更高版本（仅前端开发/构建需要）。
- 可用的网络出口和代理；使用云端浏览器时还需要对应服务的 API Key。
- 使用 RoxyBrowser 时，本机 Roxy API 必须可访问。

安装 Python 依赖并创建本地配置：

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env
```

不要把真实密钥写入 `config/*.py`、README、Issue 或提交记录。优先将密钥放入根目录 `.env`；WebUI 配置页保存密钥时也会写入 `.env`。

常用 `.env` 字段包括：

```dotenv
WEBUI_AUTH_CODE=设置一个本地授权码
WEBUI_SESSION_SECRET=可选的固定 session 密钥
BROWSER_USE_API_KEY=可选
SKYVERN_API_KEY=可选
ROXY_API_TOKEN=可选
CPA_MANAGEMENT_KEY=可选
SMS_API_KEY=可选
```

### 3.2 配置邮箱来源

推荐使用 WebUI「邮箱池」页面导入素材。也可以复制示例文件后再导入：

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

在 `config/email.py` 选择来源，例如：

```python
EMAIL_SOURCE = "outlook,generic_api,mailnest"
```

GPTMail、Cloudflare、Cloudflare 域名邮箱、MailNest 和 CloudMail 的密钥、地址、项目代码等字段可在 WebUI 配置页填写；字段说明也可直接查看 `config/email.py` 和 `.env.example`。

### 3.3 选择注册驱动

最小配置示例：

```python
# config/roxybrowser.py
REGISTRATION_DRIVER = "protocol"
```

RoxyBrowser：

```python
REGISTRATION_DRIVER = "roxy"
ROXY_API_BASE = "http://127.0.0.1:50100"
ROXY_API_TOKEN = "你的 Roxy API Key"
ROXY_WORKSPACE_ID = "你的 workspaceId"
ROXY_PROJECT_ID = "你的 projectId"
ROXY_ONE_PROFILE_PER_ACCOUNT = True
ROXY_DELETE_PROFILE_AFTER_RUN = True
ROXY_OPEN_HEADLESS = False
```

CloakBrowser：

```python
REGISTRATION_DRIVER = "cloak"
```

其无头、代理、GeoIP、语言、时区和 fingerprint seed 配置位于 `config/cloakbrowser.py`。使用 Browser Use Cloud 或 Skyvern 时，分别设置：

```python
REGISTRATION_DRIVER = "browser_use"  # 或 "skyvern"
```

并在 `.env` 设置对应 API Key。Browser Use 默认通过远端 CDP 连接；Skyvern 使用 Browser Sessions。详细字段以 `config/browser_use.py`、`config/skyvern.py` 为准。

### 3.4 配置代理和 Codex

代理池在 `config/proxy.py`：

```python
PROXY_POOL = [
    "http://user:password@host:port",
]
```

不需要 Codex 时保持默认关闭：

```python
# config/codex.py
ENABLE_CODEX_AUTO = False
```

需要自动授权时：

```python
ENABLE_CODEX_AUTO = True
CODEX_OAUTH_DRIVER = "same_as_registration"
SMS_PROVIDER = "l"       # grizzly / l / h
SMS_SERVICE = "openai"
SMS_COUNTRY = "国家代码"
SMS_MAX_RETRIES = 10
SMS_CODE_WAIT = 120
```

如果使用 CPA：

```python
CODEX_AUTH_URL_SOURCE = "cpa"
CPA_MANAGEMENT_URL = "https://你的-cpa-地址"
CPA_MANAGEMENT_KEY = "你的 CPA 管理密钥"
```

短信服务的地址和鉴权字段见 `config/codex.py` 与 [L_API.md](L_API.md)。

### 3.5 启动 WebUI

推荐使用根目录管理脚本：

```bash
./webui.sh start
./webui.sh status
./webui.sh logs
./webui.sh restart
./webui.sh stop
```

服务固定监听：<http://127.0.0.1:5000>。同一工作区只允许一个 WebUI 进程，不能通过参数改成其他端口。

可选环境变量：

```bash
OPEN_BROWSER=1 ./webui.sh start
AUTH_CODE=你的授权码 ./webui.sh start
VERBOSE=1 ./webui.sh start
```

也可以前台启动：

```bash
python web.py --open-browser
```

前台启动同样使用 `127.0.0.1:5000`。启动前请先执行 `./webui.sh status`，避免多个进程同时读写同一份数据。

### 3.6 使用 CLI

注册一个账号：

```bash
python main.py
```

批量注册 10 个账号、3 个并发线程，并在单个失败后继续：

```bash
python main.py -n 10 --workers 3 --continue-on-fail
```

查看详细日志：

```bash
python main.py -n 1 --verbose
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

### 3.7 Codex 补跑、查活和数据读取

对已注册账号单独补跑 Codex：

```bash
python scripts/operations/codex_oauth.py --email <已注册邮箱> --verbose
```

无需启动 WebUI，也可以只读查询 SQLite：

```bash
sqlite3 data/turb_gpt.sqlite3 '.tables'
sqlite3 data/turb_gpt.sqlite3 'select id, email, status from v_registered_accounts order by position;'
sqlite3 data/turb_gpt.sqlite3 'select collection, count(*) from storage_items group by collection;'
```

Python 读取示例：

```python
from core.sqlite_store import connect

with connect() as conn:
    rows = conn.execute(
        "select id, email, status from v_registered_accounts"
    ).fetchall()
```

可以用 `TURB_SQLITE_PATH=/path/to/turb_gpt.sqlite3` 指定数据库位置。导出账号、token 或凭证时，使用 WebUI 下载接口或显式导出工具，并由调用方指定目标路径；不要把导出文件放回仓库根目录。

### 3.8 常见问题

**配置保存后没有生效？** 通过 WebUI 保存的常用字段会热加载；直接修改 `config/*.py` 后需要重启 CLI 或 WebUI。

**没有接码平台能否注册？** 可以，将 `ENABLE_CODEX_AUTO` 设为 `False`。接码只用于 Codex 手机验证，不影响主注册流程。

**Codex 失败但注册成功怎么办？** 账号会保留并标记 Codex 失败，可在 WebUI 账号页补跑，或执行上面的 Codex CLI 命令。

**Roxy 无头模式仍弹出窗口？** 检查 `ROXY_OPEN_HEADLESS = True`，并确认本机 RoxyBrowser 版本支持对应 API 参数。

**Cloudflare Worker 和域名邮箱有什么区别？** `cloudflare` 使用 Worker API 创建地址并取码；`cloudflare_domain` 使用域名转发到 QQ IMAP，二者配置和收信链路不同，不能混用。

## 4. 开发指南

### 4.1 工程结构

```text
.
├── main.py                         # CLI 兼容入口
├── web.py                          # WebUI 启动入口
├── webui.sh                        # 单实例 WebUI 管理脚本
├── apps/
│   ├── cli/main.py                 # CLI 参数和批处理编排
│   └── web/                        # Flask 管理服务、鉴权、配置编辑
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

- `apps/web` 只负责 HTTP、鉴权、任务入口和展示所需的数据整形，不直接实现浏览器步骤。
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

Vite 默认运行在 `http://127.0.0.1:5173`，`/api`、`/login` 和 `/logout` 代理到运行中的 Flask `127.0.0.1:5000`。生产构建：

```bash
npm run build
```

`web/dist/` 是 Flask 生产服务使用的构建产物；`web/node_modules/` 只属于本地开发环境，不应提交。

### 4.4 测试与质量检查

在项目根目录执行：

```bash
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 pytest -q
bash -n webui.sh
git diff --check
```

测试使用 `pytest.ini` 中的 `tests/` 路径。涉及 SQLite 的测试会使用临时数据库，不应修改本地真实 `data/turb_gpt.sqlite3`。

提交前至少确认：

1. 未提交 `.env`、数据库、token、凭证或导出文件。
2. 新增功能有对应测试或清晰的手工验证步骤。
3. WebUI 仍只监听 `127.0.0.1:5000`，没有启动第二个生产实例。
4. 浏览器驱动能通过统一注册接口调用，未把驱动细节泄漏到 Web/API 层。

### 4.5 SQLite 与迁移约定

`core/sqlite_store.py` 提供 JSON 集合和文件内容的统一读写 API：

- `storage_collections` / `storage_items` 保存结构化业务数据。
- `storage_files` 保存日志、凭证、批次归档等二进制或文本内容。
- `v_registered_accounts`、`v_outlook_pool`、`v_registration_jobs`、`v_relay_*` 提供常用查询视图。
- 历史 JSON/TXT 只在集合不存在时执行一次性幂等导入。
- 生产写入使用 `mirror=False`，兼容文件镜像只允许测试或调用方明确指定的外部导出路径。

新增业务数据时，优先设计 SQLite 集合/文件类别和迁移逻辑，不要重新引入根目录 JSON、日志目录或静态 HTML 快照。

### 4.6 贡献流程

1. 从 `main` 创建功能分支。
2. 只修改与目标相关的模块，并补充测试和文档。
3. 本地运行测试、前端构建和 `git diff --check`。
4. 提交信息使用清晰的动词短语，提交前检查 `git status`，确认没有敏感文件。
5. Pull Request 中说明行为变化、迁移影响、验证命令和已知限制。

## 5. 开源协议

本项目采用 [MIT License](LICENSE)。除许可证正文要求外，使用者还需要自行遵守：

- ChatGPT/OpenAI、邮箱、短信、浏览器云服务和代理服务的服务条款；
- 账号注册、自动化访问、数据保存和跨境传输适用的法律法规；
- 目标网络、代理和第三方 API 的授权范围。

项目按“现状”提供，不对第三方服务可用性、账号状态、注册成功率或外部接口兼容性作保证。第三方服务的密钥、账号和网络资源由使用者自行负责。

Copyright (c) 2026 Turb GPT Free Register contributors. 详见 [LICENSE](LICENSE)。
