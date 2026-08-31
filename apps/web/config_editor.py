# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：暴露运行时开关/数值/默认值，以及为自部署兼容而需要调整的
       外部接口路径/方法；client_id / scope / sentinel 版本等不可变协议常量
       仍然不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入项目根 `.env`，不再修改 `config/*.py`。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 用 `.env` 覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {"PROXY_POOL"}


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "注册驱动", "help": "本机推荐 chrome_cdp；浏览器驱动优先邮箱 OTP，仅在 OpenAI 没有 OTP 入口且强制创建密码时使用密码兜底；protocol 始终是纯邮箱 OTP 流程",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Cloak License", "help": "Pro license；留空使用免费 binary",
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Cloak用户目录", "help": "留空使用临时上下文；填写路径则持久化 cookies/cache",
    },
    {
        "key": "CLOAK_EXTRA_ARGS", "file": "cloakbrowser.py", "type": "list_str_multiline", "group": "代理浏览器",
        "label": "Cloak Chromium 参数", "help": "每行一个额外启动参数，例如 --disable-gpu；通常保持为空",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "代理浏览器",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "Browser Use API Key", "help": "保存在 .env（BROWSER_USE_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_CONNECT_MODE", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "连接方式", "help": "cdp_url=直接连接官方 CDP；sdk=预留的 REST 会话模式",
    },
    {
        "key": "BROWSER_USE_API_BASE", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "REST API 地址", "help": "Browser Use REST API 根地址；通常保持默认值",
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "代理浏览器",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "Profile ID", "help": "可选。填写则复用 Browser Use profile 的 cookies/localStorage；批量建议留空",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "代理浏览器",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_NAVIGATION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "代理浏览器",
        "label": "页面导航超时(秒)", "help": "页面打开和跳转的最长等待时间",
    },
    {
        "key": "BROWSER_USE_SESSION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "代理浏览器",
        "label": "云端keepAlive(分钟)", "help": "传给 Browser Use connect URL 的 timeout/keepAlive；程序会自动限制到 1-240，建议 240",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "代理浏览器",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "代理浏览器",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "代理浏览器",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "代理浏览器",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },

    # ---- Skyvern Cloud Browser ----
    {
        "key": "SKYVERN_API_KEY", "file": "skyvern.py", "type": "str", "group": "代理浏览器",
        "label": "Skyvern API Key", "help": "保存在 .env（SKYVERN_API_KEY），用于创建 Skyvern Browser Session",
        "storage": "env", "secret": True,
    },
    {
        "key": "SKYVERN_API_BASE", "file": "skyvern.py", "type": "str", "group": "代理浏览器",
        "label": "API 地址", "help": "默认 https://api.skyvern.com",
    },
    {
        "key": "SKYVERN_BROWSER_SESSION_TIMEOUT", "file": "skyvern.py", "type": "int", "group": "代理浏览器",
        "label": "Session 超时(分钟)", "help": "创建 Skyvern Browser Session 时传入的 timeout",
    },
    {
        "key": "SKYVERN_BROWSER_PROFILE_ID", "file": "skyvern.py", "type": "str", "group": "代理浏览器",
        "label": "Browser Profile ID", "help": "可选，复用 Skyvern browser profile",
    },
    {
        "key": "SKYVERN_PROXY_LOCATION", "file": "skyvern.py", "type": "str", "group": "代理浏览器",
        "label": "代理地区", "help": "可填 jp/us/gb 等简写；会自动转为 Skyvern 枚举，如 jp→RESIDENTIAL_JP；留空不传",
    },
    {
        "key": "SKYVERN_BROWSER_TYPE", "file": "skyvern.py", "type": "str", "group": "代理浏览器",
        "label": "浏览器类型", "help": "Skyvern 支持 msedge / chrome / stealth-chromium；旧值 chromium-headful 会自动转为 stealth-chromium",
    },
    {
        "key": "SKYVERN_AD_BLOCKER", "file": "skyvern.py", "type": "bool", "group": "代理浏览器",
        "label": "广告拦截", "help": "创建 Skyvern Browser Session 时启用 ad_blocker",
    },
    {
        "key": "SKYVERN_GENERATE_BROWSER_PROFILE", "file": "skyvern.py", "type": "bool", "group": "代理浏览器",
        "label": "保存浏览器Profile", "help": "Session 结束时是否让 Skyvern 生成/保存 browser profile",
    },
    {
        "key": "SKYVERN_KEEP_BROWSER_OPEN", "file": "skyvern.py", "type": "bool", "group": "代理浏览器",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不主动关闭 Skyvern Browser Session",
    },
    {
        "key": "SKYVERN_START_URL", "file": "skyvern.py", "type": "str", "group": "代理浏览器",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Roxy API Key", "help": "保存在 .env（ROXY_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_METHOD", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "获取团队请求方法", "help": "Roxy 工作区列表请求方法，通常为 GET；仅在自部署 API 要求其他方法时调整",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_OPEN_METHOD", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "打开接口方法", "help": "打开环境请求方法，常见为 POST；按 Roxy 版本 API 要求填写",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_CLOSE_METHOD", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "关闭接口方法", "help": "关闭环境请求方法，常见为 POST",
    },
    {
        "key": "ROXY_CREATE_PATH", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "创建接口路径", "help": "默认 /browser/create；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_CREATE_METHOD", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "创建接口方法", "help": "创建环境请求方法，常见为 POST",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_RANDOM_OS_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "创建环境随机OS", "help": "创建 Roxy 环境时每次在 Windows / macOS 中随机，不固定 macOS",
    },
    {
        "key": "ROXY_RANDOM_OS_CHOICES", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "随机OS范围", "help": "逗号分隔，默认 Windows,macOS；Roxy 支持 Windows / macOS / Linux / IOS / Android",
    },
    {
        "key": "ROXY_DEFAULT_OS", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "默认OS", "help": "关闭随机OS时使用；可填 Windows、macOS、Linux、IOS 或 Android",
    },
    {
        "key": "ROXY_DEFAULT_OS_VERSION", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "默认OS版本", "help": "可选固定版本，例如 15.3.2；留空使用 Roxy 默认版本",
    },
    {
        "key": "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "创建环境随机名称", "help": "创建 Roxy 环境时自动生成不同名称，避免固定 gpt-free-register",
    },
    {
        "key": "ROXY_PROFILE_NAME_PREFIX", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "随机名称前缀", "help": "默认 rb；实际名称格式类似 rb-时间戳-随机码",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "代理浏览器",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "ROXY_DELETE_METHOD", "file": "roxybrowser.py", "type": "str", "group": "代理浏览器",
        "label": "删除接口方法", "help": "删除环境请求方法，常见为 POST",
    },
    {
        "key": "ROXY_SELENIUM_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "代理浏览器",
        "label": "Selenium 超时(秒)", "help": "Roxy 浏览器页面和元素操作的最长等待时间",
    },
    {
        "key": "ROXY_API_RETRIES", "file": "roxybrowser.py", "type": "int", "group": "代理浏览器",
        "label": "API 重试次数", "help": "Roxy 临时网络错误的重试次数；创建接口通常不重试",
    },
    {
        "key": "ROXY_API_RETRY_DELAY", "file": "roxybrowser.py", "type": "int", "group": "代理浏览器",
        "label": "API 重试间隔(秒)", "help": "Roxy API 重试之间的基础等待时间",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex授权驱动", "help": "本地推荐 chrome_cdp；protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；chrome_cdp=正常启动系统 Chrome 后通过 CDP 接管；browser_use=用 Browser Use Cloud；skyvern=用 Skyvern；same_as_registration=跟随注册驱动",
    },
    {
        "key": "CODEX_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "Codex 请求超时(秒)", "help": "Codex OAuth 网络请求的最长等待时间",
    },
    {
        "key": "CHROME_CDP_EXECUTABLE_PATH", "file": "codex.py", "type": "str", "group": "代理浏览器",
        "label": "系统 Chrome 路径", "help": "留空自动查找 Google Chrome；找不到时填写完整可执行文件路径",
    },
    {
        "key": "CHROME_CDP_START_TIMEOUT", "file": "codex.py", "type": "int", "group": "代理浏览器",
        "label": "Chrome CDP 启动超时", "help": "等待系统 Chrome 开放本地 CDP 的最长秒数",
    },
    {
        "key": "CHROME_CDP_PAGE_TIMEOUT", "file": "codex.py", "type": "int", "group": "代理浏览器",
        "label": "Chrome 页面超时", "help": "系统 Chrome 页面导航和元素操作的最长秒数",
    },
    {
        "key": "CHROME_CDP_KEEP_BROWSER_OPEN", "file": "codex.py", "type": "bool", "group": "代理浏览器",
        "label": "保留 Chrome", "help": "仅调试时开启；任务结束后不关闭本次系统 Chrome，也不删除临时 Profile",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "代理浏览器",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "FLOW_TRIGGER_URL", "file": "flow_trigger.py", "type": "str", "group": "Flow Trigger",
        "label": "Flow 接口地址", "help": "注册成功后调用的 Flow HTTP 地址；启用功能时必填",
    },
    {
        "key": "FLOW_TRIGGER_BEARER", "file": "flow_trigger.py", "type": "str", "group": "Flow Trigger",
        "label": "Flow Bearer Token", "help": "写入 Authorization: Bearer 头；保存到 .env，不会写入源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "FLOW_TRIGGER_COOKIE", "file": "flow_trigger.py", "type": "str", "group": "Flow Trigger",
        "label": "Flow Cookie", "help": "可选 Cookie 请求头；保存到 .env，不会写入源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "FLOW_TRIGGER_TIMEOUT", "file": "flow_trigger.py", "type": "int", "group": "Flow Trigger",
        "label": "Flow 请求超时(秒)", "help": "调用 Flow 接口的最长等待时间",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    {
        "key": "ENABLE_HUMANIZE_BROWSER_ACTIONS", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "浏览器动作随机化", "help": "Roxy/Cloak 点击、输入、页面观察使用随机鼠标落点和逐字输入，降低机械操作痕迹",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "使用自动邮箱渠道", "help": "开启后从你勾选的渠道获取邮箱并自动收取验证码；关闭后使用下方的手动邮箱",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "关闭自动邮箱渠道时使用。填写你能正常收信的邮箱，收到验证码后回到 GPT账号页面提交",
    },
    {
        "key": "REGISTER_PASSWORD", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "注册密码", "help": "建议留空：注册优先使用邮箱 OTP；仅在 OpenAI 没有 OTP 入口且强制创建密码时使用，留空则为当前账号生成独立随机密码。纯协议注册始终使用邮箱 OTP",
        "storage": "env", "secret": True,
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "验证码最长等待时间（秒）", "help": "超过这个时间仍未收到验证码，本次注册会标记为失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "检查新邮件间隔（秒）", "help": "系统每隔多少秒检查一次新验证码邮件",
    },
    {
        "key": "OTP_SETTLE_SECONDS", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "重复邮件等待时间（秒）", "help": "收到第一封邮件后短暂等待，避免误用较早的验证码；一般保持默认",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "默认邮箱渠道", "help": "勾选要默认使用的渠道，并在页面上调整优先顺序",
    },
    {
        "key": "EMAIL_IMPORT_SEPARATORS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "导入分隔符", "help": "导入账号或邮箱时用于分开各项内容。多个分隔符用逗号隔开，默认支持 ---、----、| 和 ====",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "从 GPTMail 服务网站取得并粘贴到这里",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Worker 服务地址", "help": "填写服务管理员提供的完整访问地址，例如 https://mail.example.com",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Worker 访问密钥", "help": "服务允许匿名访问时留空；需要鉴权时填写管理员提供的密钥",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook 收信方式", "help": "不确定时填写 auto；只有服务管理员明确要求时才改为 direct 或 remote",
    },
    {
        "key": "OUTLOOK_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook 收信服务地址", "help": "通常保持默认；只有你使用自己的收信服务时才修改",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮件转发域名", "help": "填写已在 Cloudflare 开启邮件转发的域名，例如 mydomain.com",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "接收邮件的 QQ 邮箱", "help": "填写 Cloudflare 邮件最终转发到的 QQ 邮箱，例如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_SERVER", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱收信服务器", "help": "通常保持默认的 imap.qq.com",
    },
    {
        "key": "QQ_IMAP_PORT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "QQ 邮箱收信端口", "help": "通常保持默认的 993",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱授权码", "help": "在 QQ 邮箱设置中开启 IMAP 后生成；这里不是 QQ 登录密码",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest API Key", "help": "登录 MailNest 后从账户中复制；请同时确认账户余额充足",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_PROJECT_CODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest 项目代码", "help": "在 MailNest 购买邮箱页面查看；OpenAI 邮箱通常为 chatgpt001",
    },
    {
        "key": "CLOUDMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 服务地址", "help": "填写服务管理员提供的完整访问地址，例如 https://mail.example.com",
    },
    {
        "key": "CLOUDMAIL_ADMIN_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 管理员邮箱", "help": "没有 Token 时填写，用于生成 Token 并读取可用域名",
        "storage": "env",
    },
    {
        "key": "CLOUDMAIL_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 管理员密码", "help": "没有 Token 时填写，随后点击上方的“生成 Token”",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_TOKEN_PATH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token路径", "help": "固定使用 /api/public/genToken；如部署版本不同可修改",
    },
    {
        "key": "CLOUDMAIL_AUTH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token", "help": "由服务管理员提供，或使用上方的管理员邮箱和密码生成",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "CloudMail 可用域名", "help": "通常点击上方“获取域名”自动填写；也可以每行手动填写一个域名",
    },
    {
        "key": "CLOUDMAIL_AUTO_ADD_USER", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "CloudMail自动创建用户", "help": "生成随机邮箱后调用 /api/public/addUser 创建用户",
    },
    {
        "key": "CLOUDMAIL_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "CloudMail随机名前缀长度", "help": "生成邮箱 local-part 的长度，建议 10-16",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "代理浏览器",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "代理浏览器",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "代理浏览器",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },
    {
        "key": "REJECT_CLOUD_PROXY", "file": "browser.py", "type": "bool", "group": "代理浏览器",
        "label": "拒绝云代理出口", "help": "检测到云厂商代理组织时拒绝该出口并重新选代理；关闭后允许继续使用",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "每行一个代理 URL，留空行会被忽略；为空则不使用代理",
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent网络模式", "help": "用于查套餐和生成 Agent Token；auto=本地代理可用则走代理、未监听则直连；proxy=强制代理；direct=强制直连",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent专用代理", "help": "用于查套餐和生成 Agent Token；留空时 auto/proxy 从代理池选择。可能包含认证信息，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent超时(秒)", "help": "查套餐和生成 Agent Token 的单次请求超时，建议 10-20 秒；独立于注册请求超时",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐/Agent最大尝试次数", "help": "查套餐和生成 Agent Token 遇到网络错误、429、5xx 等临时错误时的重试次数，建议 2 次",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent重试间隔(秒)", "help": "查套餐和生成 Agent Token 的重试间隔，按尝试次数递增；服务端 Retry-After 优先",
    },
    {
        "key": "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "新账号资格复查延迟(秒)", "help": "新注册 free 账号未发现试用资格或首次查询失败时复查一次；0 表示关闭",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询并发数", "help": "自动、手动和批量查套餐共用；Agent Token 生成使用独立队列；建议 2-4 个线程",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询队列上限", "help": "防止异常批量操作无限堆积，建议 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求最小间隔(秒)", "help": "限制查套餐和生成 Agent Token 的请求启动频率，降低 429 风险",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求随机抖动(秒)", "help": "在查套餐和生成 Agent Token 的最小间隔上增加随机延迟，避免请求过于规律",
    },
    # ---- 提链 ----
    {
        "key": "EXTRACT_LINK_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务地址", "help": "填写提链服务 API 地址",
    },
    {
        "key": "EXTRACT_LINK_CDK", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链 CDK", "help": "创建提链任务和监听任务事件使用；成功提链扣 1 次",
        "storage": "env", "secret": True,
    },
    {
        "key": "EXTRACT_LINK_TYPE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链类型", "help": "支持 pix / upi / kakao_pay / ideal",
    },
    {
        "key": "EXTRACT_LINK_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链并发数", "help": "批量提链后台线程数，建议 1-4",
    },
    {
        "key": "EXTRACT_LINK_QUEUE_LIMIT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链队列上限", "help": "后台待处理任务的最大数量，防止批量提交无限堆积",
    },
    {
        "key": "EXTRACT_LINK_REQUEST_TIMEOUT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链请求超时(秒)", "help": "创建提链任务 HTTP 请求的最长等待时间",
    },
    {
        "key": "EXTRACT_LINK_EVENT_TIMEOUT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链事件超时(秒)", "help": "等待提链服务完成事件的最长时间",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_AUTO_EXPORT", "file": "sub2api.py", "type": "bool", "group": "Codex",
        "label": "Agent sub2 自动同步", "help": "生成 Codex Agent Token 成功后自动同步到 sub2api",
    },
    {
        "key": "SUB2API_SYNC_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 同步模式", "help": "api=直接上传接口；file=写本地json；both=接口+本地json",
    },
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API基址", "help": "sub2api 服务地址；Agent Token 上传和 Codex OAuth 共用，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    {
        "key": "SUB2API_OUTPUT_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 本地路径", "help": "仅 SUB2API_SYNC_MODE=file/both 时使用；相对路径按项目根目录解析",
    },
    {
        "key": "SUB2API_PROXY_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 代理键", "help": "可选；写入 account.proxy_key，并在 proxies 为空时初始化 proxies[0].proxy_key",
    },
    {
        "key": "SUB2API_API_URL", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 兼容完整接口", "help": "旧版上传接口完整 URL；填写后优先于 API 基址拼接",
    },
    {
        "key": "SUB2API_API_TOKEN", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 兼容 Token", "help": "旧配置名，作为 SUB2API_API_KEY 的兼容回退；保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_AUTH_HEADER", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 鉴权请求头", "help": "默认 x-api-key；自部署 sub2 服务可改为 Authorization 等",
    },
    {
        "key": "SUB2API_API_AUTH_PREFIX", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 鉴权前缀", "help": "例如 Bearer；x-api-key 通常留空",
    },
    {
        "key": "SUB2_CODEX_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 Codex API 基址", "help": "Codex OAuth 对接专用地址；留空时复用 sub2 API 基址",
    },
    {
        "key": "SUB2_CODEX_AUTH_URL_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 授权地址路径", "help": "生成 Codex 授权链接的接口路径；默认 /api/v1/admin/openai/generate-auth-url",
    },
    {
        "key": "SUB2_CODEX_CALLBACK_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 回调路径", "help": "提交 OAuth callback 的接口路径；默认 /api/v1/admin/openai/create-from-oauth",
    },
    {
        "key": "SUB2_CODEX_API_TOKEN", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 Codex Token", "help": "Codex 对接专用 Token；为空时复用 sub2 API Key，保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "SUB2_CODEX_AUTH_HEADER", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 Codex 鉴权头", "help": "留空时复用 sub2 API 鉴权头",
    },
    {
        "key": "SUB2_CODEX_AUTH_PREFIX", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 Codex 鉴权前缀", "help": "留空时复用 sub2 API 鉴权前缀",
    },
    {
        "key": "SUB2_CODEX_CALLBACK_PAYLOAD_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 回调载荷模式", "help": "create_from_oauth=创建账号；exchange_code=仅换 token",
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_CALLBACK_SUBMIT_RETRIES", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 回调重试次数", "help": "提交 OAuth callback 遇到超时、409 或 5xx 时的重试次数",
    },
    {
        "key": "CPA_CALLBACK_SUBMIT_RETRY_DELAY", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 回调重试间隔(秒)", "help": "每次 CPA callback 重试前的基础等待时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "当前接码平台", "help": "选择手机号池动态来源：grizzly=GrizzlySMS，l=L 服务，h=H 服务。一次只启用一个平台",
    },
    {
        "key": "SMS_POOL_PLATFORM_ENABLED", "file": "codex.py", "type": "bool", "group": "接码平台",
        "label": "加入手机号池", "help": "开启后，该平台会显示在手机号池顶部，作为“平台自动取号”特殊来源；关闭后只使用手工导入号码",
    },
    {
        "key": "SMS_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "Grizzly API 地址", "help": "GrizzlySMS handler API 地址；自部署或兼容服务可在此替换",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "传给接码平台的 country；GrizzlySMS 常用：美国=187；H 通道作为 H_API.md 的 country",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务/项目代码", "help": "GrizzlySMS/L 作为 service；H 通道作为 H_API.md 的 projectId",
    },
    {
        "key": "SMS_MAX_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "短信最高价格", "help": "可选，透传给支持价格过滤的接码平台；留空表示不限",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_POLL_INTERVAL", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "短信轮询间隔(秒)", "help": "调用接码平台查询验证码的间隔",
    },
    {
        "key": "SMS_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "短信请求超时(秒)", "help": "接码平台 HTTP 请求的最长等待时间",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "GrizzlySMS API密钥", "help": "GrizzlySMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H API 地址", "help": "H 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "H_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 授权码", "help": "保存在 .env（H_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 号码前缀", "help": "H 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "H_PHONE_ACQUIRE_MODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 取号方式", "help": "reusable=优先复用历史可用号码；new=每次都取一个新号码",
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# 邮箱配置在运行时仍共用历史 ``group=邮箱 / OTP``，但 WebUI 需要更细的
# 层级才能把不同供应商的凭证、路径和收件参数隔离展示。这里仅给 API
# 返回值补充元数据，不改变旧配置字段和写入协议。
_EMAIL_FIELD_CHANNEL = {
    # 共同的注册/验证码行为
    "USE_EMAIL_SERVICE": "general",
    "REGISTER_EMAIL": "general",
    "REGISTER_PASSWORD": "general",
    "REGISTER_NAME": "general",
    "OTP_MAX_WAIT": "general",
    "OTP_POLL_INTERVAL": "general",
    "OTP_SETTLE_SECONDS": "general",
    "EMAIL_SOURCE": "general",
    "EMAIL_IMPORT_SEPARATORS": "general",
    # 持久化邮箱池与运行时供应商
    "OUTLOOK_FETCH_MODE": "outlook",
    "OUTLOOK_API_BASE": "outlook",
    "EMAIL_DOMAIN": "cloudflare_domain",
    "QQ_EMAIL": "cloudflare_domain",
    "QQ_IMAP_SERVER": "cloudflare_domain",
    "QQ_IMAP_PORT": "cloudflare_domain",
    "QQ_IMAP_PASSWORD": "cloudflare_domain",
    "GPTMAIL_API_KEY": "gptmail",
    "MAIL_NEST_API_KEY": "mailnest",
    "MAIL_NEST_PROJECT_CODE": "mailnest",
    "CLOUDMAIL_API_BASE": "cloudmail",
    "CLOUDMAIL_ADMIN_EMAIL": "cloudmail",
    "CLOUDMAIL_PASSWORD": "cloudmail",
    "CLOUDMAIL_TOKEN_PATH": "cloudmail",
    "CLOUDMAIL_AUTH_TOKEN": "cloudmail",
    "CLOUDMAIL_DOMAINS": "cloudmail",
    "CLOUDMAIL_AUTO_ADD_USER": "cloudmail",
    "CLOUDMAIL_RANDOM_LOCAL_LENGTH": "cloudmail",
    # Cloudflare Worker 临时邮箱
    "CLOUDFLARE_API_BASE": "cloudflare",
    "CLOUDFLARE_API_KEY": "cloudflare",
    "CLOUDFLARE_AUTH_MODE": "cloudflare",
    "CLOUDFLARE_CUSTOM_AUTH": "cloudflare",
    "CLOUDFLARE_PATH_ACCOUNTS": "cloudflare",
    "CLOUDFLARE_PATH_MESSAGES": "cloudflare",
    "CLOUDFLARE_PATH_DOMAINS": "cloudflare",
    "CLOUDFLARE_PATH_TOKEN": "cloudflare",
    "CLOUDFLARE_DEFAULT_DOMAINS": "cloudflare",
    "CLOUDFLARE_REQUEST_TIMEOUT": "cloudflare",
    "CLOUDFLARE_NAME_LENGTH": "cloudflare",
}

# 接码配置沿用历史 ``group=接码平台``，但前端按供应商模块隔离展示。
# 国家、项目代码和重试参数是所有供应商共用的运行策略，放在总览页；
# 各供应商的地址、密钥和号码参数只出现在对应模块。
_SMS_FIELD_CHANNEL = {
    "SMS_PROVIDER": "general",
    "SMS_POOL_PLATFORM_ENABLED": "general",
    "SMS_COUNTRY": "general",
    "SMS_SERVICE": "general",
    "SMS_MAX_PRICE": "general",
    "SMS_MAX_RETRIES": "general",
    "SMS_CODE_WAIT": "general",
    "SMS_POLL_INTERVAL": "general",
    "SMS_REQUEST_TIMEOUT": "general",
    "SMS_API_BASE": "grizzly",
    "SMS_API_KEY": "grizzly",
    "H_API_BASE": "h",
    "H_ADMIN_AUTH_CODE": "h",
    "H_PHONE_PREFIX": "h",
    "H_PHONE_ACQUIRE_MODE": "h",
    "L_API_BASE": "l",
    "L_ADMIN_AUTH_CODE": "l",
    "L_PHONE_PREFIX": "l",
}


# 浏览器设置沿用历史 ``group=代理浏览器``，但不同驱动的凭证、会话和
# 指纹参数必须在 WebUI 中隔离展示。模块只影响元数据和页面分组，不改变
# .env 键名、默认值或保存协议。
_BROWSER_FIELD_MODULE = {
    "REGISTRATION_DRIVER": "general",
    **{
        field["key"]: "roxy"
        for field in EDITABLE_FIELDS
        if field["group"] == "代理浏览器" and field["key"].startswith("ROXY_")
    },
    **{
        field["key"]: "cloak"
        for field in EDITABLE_FIELDS
        if field["group"] == "代理浏览器" and field["key"].startswith("CLOAK_")
    },
    **{
        field["key"]: "browser_use"
        for field in EDITABLE_FIELDS
        if field["group"] == "代理浏览器" and field["key"].startswith("BROWSER_USE_")
    },
    **{
        field["key"]: "skyvern"
        for field in EDITABLE_FIELDS
        if field["group"] == "代理浏览器" and field["key"].startswith("SKYVERN_")
    },
    **{
        field["key"]: "system_chrome"
        for field in EDITABLE_FIELDS
        if field["group"] == "代理浏览器" and field["key"].startswith("CHROME_CDP_")
    },
    **{
        field["key"]: "locale"
        for field in EDITABLE_FIELDS
        if field["group"] == "代理浏览器"
        and field["key"]
        in {"BROWSER_LOCALE_PROFILE", "AUTO_BROWSER_LOCALE_FROM_IP", "IP_GEO_TIMEOUT", "REJECT_CLOUD_PROXY"}
    },
}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = "env"
        item["value"] = value
        email_channel = _EMAIL_FIELD_CHANNEL.get(key)
        if email_channel:
            item["email_section"] = "email"
            item["email_channel"] = email_channel
        browser_module = _BROWSER_FIELD_MODULE.get(key)
        if browser_module:
            item["browser_section"] = "browser"
            item["browser_module"] = browser_module
        sms_channel = _SMS_FIELD_CHANNEL.get(key)
        if sms_channel:
            item["sms_section"] = "sms"
            item["sms_channel"] = sms_channel
        out.append(item)
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 .env 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写项目根 `.env`。"""
    from config.env_loader import write_env_values, load_env

    updated, ignored = [], []
    env_updates: dict[str, str] = {}

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "env_updated": env_updated}
