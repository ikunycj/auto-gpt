# Roxy 注册与授权：初版对比及修复记录

核验日期：2026-10-02（Asia/Shanghai）。本记录针对用户实际使用的 Roxy 注册、2FA、Codex 授权及授权后的验活链路，不表示所有注册驱动均已做真实端到端验证。

## 1. 比较基准与边界

- 第一次提交：`1cef7a6875524346f3b33cdb44a1bda75ad262cb`，2026-08-21，`Initial modular SQLite project structure`。
- 当前已提交基线：`9f7f1a076377bf2f87695084f7332e013c9701e8`，分支 `main`。
- 第三份比较对象是**修复开始时已有大量修改的工作树**，不是把所有差异都算作最近某次提交或本次修复。
- 没有 reset、clean、覆盖整仓或恢复旧数据库；保留原有未提交修改。本次未 commit/push，也未修改 `.env`。

初版到 HEAD 的重点文件差异：

| 文件 | 已提交差异 |
| --- | --- |
| `core/roxybrowser_client.py` | 无差异；本地现有网络逻辑变化主要位于未提交工作树 |
| `registration/drivers/roxy/implementation.py` | 144 行 diff，包含密码保存及注册/授权拆分等调整 |
| `core/roxy_codex_oauth.py` | 317 行 diff |
| `config/proxy.py` | 53 行 diff |
| `core/session.py` | 3 行 diff |

复查方式：

```sh
git diff 1cef7a6875524346f3b33cdb44a1bda75ad262cb HEAD -- \
  config/proxy.py core/session.py core/roxybrowser_client.py \
  core/roxy_codex_oauth.py registration/drivers/roxy/implementation.py
git diff HEAD -- \
  config/proxy.py core/session.py core/roxybrowser_client.py \
  core/roxy_codex_oauth.py core/chatgpt_browser_login.py \
  core/codex_relay_service.py registration/drivers/roxy/implementation.py
```

## 2. 初版确实更短，但不是所有步骤都执行了

| 环节 | 初版 | 修复前工作树的关键行为 | 本次处理 |
| --- | --- | --- | --- |
| 注册入口 | 优先邮箱 OTP | 开启 2FA 时走密码注册 | 未开 2FA 保留 OTP 优先；开 2FA 时保留必须建立密码的要求 |
| 2FA | `ENABLE_2FA` 开启也只写警告并跳过，`totp_secret=None` | 已增加 2FA 设置和严格登录验证 | 不恢复“跳过 2FA 仍当作完成”；保留真实验证 |
| 注册后密码/TOTP 验证 | 没有这条额外链路 | 从 Roxy 切换系统 Chrome，再把 Cookie 转为 HTTP 会话验证 | 在**同一个 Roxy 浏览器**清态并重新登录，不再额外创建 Chrome 或转 HTTP 验证 |
| 注册与 Codex | 可在注册后同浏览器继续授权 | 已分离为注册、手动授权 | 保留分离，不因授权失败丢失已独立完成的注册结果 |
| 代理 | 不能作为当前配置一致性的保证 | 任务代理、固定 Profile、HTTP 客户端可能不同源 | 任务只选路一次，浏览器读回确认，后续操作沿用实际线路 |
| MFA 失败解释 | 缺少完整终止账号错误识别 | 页面 URL 未变化时可能误报 MFA 超时 | 优先识别删除/停用，再判断 MFA 挑战与超时 |

因此，本次借鉴初版的**少切换浏览器、少切换网络实现**，不是机械回滚所有功能或降低成功条件。

## 3. 发现并修复的具体问题

### 3.1 固定 Profile 仍保存旧代理

通过本机 Roxy 的只读 `list_v3`、`connection_info` 核验：当时代理池有 1 项，使用 SOCKS5；配置为复用固定 Profile，而该 Profile 保存的代理与本次选择的代理不一致，且窗口处于关闭状态。

修复后的行为：

1. 显式代理优先，其次代理池，均未配置时使用本机网络。
2. `None` 表示尚未选路；空字符串 `""` 表示已经明确选择本机直连，后续不能再抽代理。
3. 固定 Profile 关闭时，通过配置的更新接口同步代理或清除旧代理，随后读回确认，再打开。
4. 固定 Profile 已开且网络一致时只读复用；网络不一致则提示关闭该窗口后重试，不强制重启或改写用户窗口。
5. 新 Profile 创建成功后同样读回网络配置；返回成功不等于代理已生效。
6. 配置代理无效或读回失败时明确报错，不静默回退直连，不关闭 TLS 校验。
7. 本机 Roxy 控制 API 禁用环境代理继承，不跟随账号代理绕行。

实机只读核验不等于已实测 `/browser/mdf` 更新；更新、打开及真实网页访问还需下一次授权/注册任务验证。

### 3.2 注册验证跨浏览器、跨 HTTP 实现

`verify_password_totp_login` 新增可复用浏览器参数；Roxy 注册传入当前 driver。严格模式要求：

- 清理旧登录态；
- 实际提交密码，不接受 OTP-only 或旧会话跳过；
- 实际完成本地 TOTP 挑战；
- 新会话包含 `accessToken`，且邮箱与目标账号一致。

失败不保存为“已验证成功”。不传 driver 的维护用途仍保留系统 Chrome 路径，未将它扩展为本轮全面重写范围。

注意：当前清态会影响 Profile 的 Cookie/登录态；固定 Profile 应专用于该自动化流程，不要承载无关业务会话。

### 3.3 OAuth 成功后又换路，或依赖已删除 Profile

- 授权作业只解析一次代理；OAuth 和即时凭证验证共用线路。
- 消费 OAuth 返回的实际核验线路，保留明确直连。
- 成功后内部保存 `_codex_verified_proxy`，后续刷新凭证/验活沿用该线路；临时 Profile 删除不再导致已知线路丢失。
- 公开账号 projection 和数据库装饰结果移除这个私有字段，不增加公开代理认证信息。
- 显式更改账号代理时清除旧线路快照与旧 Profile 关联。
- 旧账号只有 Profile 信息时仍做只读查询；查询失败不是直连，不凭空猜测。
- 存储的 Profile ID 不再因为当前全局固定 Profile ID 改变就被直接拒绝，仍必须精确匹配返回的 Profile。

### 3.4 账号删除/停用被 MFA 超时覆盖

统一检测页面正文、标题、提示、URL 以及认证响应中的已知终止账号错误。MFA 提交前、提交后和超时边界都优先检查；即使 URL/DOM 未跳转，也能读取允许的认证错误码。

认证响应观察只保存允许的错误码，不留响应正文、Token 或密码。fetch/XHR 观察不改变原请求的返回值和异常，CDP 响应读取仅有限重试。

网络异常保留原始 `ERR_*` 码，并提供分层说明；网络连接错误本身不判定为账号删除/停用。

### 3.5 脱敏和生命周期

- 按用户要求，不再以宽泛数字规则替换手机号，避免误伤 IP、Chrome 版本和栈地址。
- 密码、Token、TOTP secret、代理认证等敏感材料仍受保护。
- 新建临时 Profile 后，如果打开请求失败或缺调试地址，调用者还没拿到对象：由客户端回收本轮新建 Profile。
- 固定 Profile 的失败不会触发该临时资源回收逻辑；用户已打开的 Profile 不由任务结束逻辑关闭/删除。

## 4. 验证结果

### 已完成的代码验证

- 全量：`579 passed, 2 subtests passed in 12.23s`。
- `68` 个新增/修改 Python 文件语法检查通过。
- `bash -n webui.sh` 通过。
- `git diff --check` 通过。
- `npm run build` 通过。

覆盖包含：显式直连与代理池的区别、固定 Profile 关闭同步/已开拒绝、创建后读回、临时 Profile 清理、同浏览器严格密码/TOTP 验证、OAuth 到验活线路连续性、私有线路隐藏、账号错误的 DOM/URL/CDP 识别，以及用 Node VM 实际执行 fetch/XHR observer 的行为测试。

这些是本地代码测试，不能证明上游真实网页、代理供应商或账号当前可用。

### 实际网络只读探测

2026-10-02 再次通过当前配置代理请求两个公开页面，开启 TLS 校验、禁用环境代理、无重定向和直连回退：

| 请求 | 结果 |
| --- | --- |
| `https://auth.openai.com/` | HTTP 403，TLS 校验通过，约 0.43 秒 |
| `https://chatgpt.com/auth/login` | HTTP 403，TLS 校验通过，约 0.43 秒 |

这说明该次 HTTP 探测没有在 TLS 握手处失败，但请求仍被拒绝。它**既不证明代理在真实 Roxy 浏览器中不可用，也不证明浏览器能够完成登录**，更不能用来确认账号停用。未因此修改代理凭证、关闭证书校验或进行风控绕过。

### 运行与交付

- 重启前已检查注册任务、Codex 任务和账号维护状态，没有排队/运行/等待中的任务。
- 服务仅使用前端 `127.0.0.1:5555`、后端 `127.0.0.1:6666`，生命周期使用 `./webui.sh`。
- 未自动购买邮箱、请求短信、注册新账号或执行真实 OAuth；没有端到端成功的结论。

## 5. 下一次真实验证的顺序

1. 保持当前设置不变，先关闭网络不一致的固定 Roxy 窗口，让下一任务执行同步及读回确认。
2. 对一个确定未删除/停用、邮箱仍可接码的既有账号单独授权，先验证浏览器能打开授权页面。
3. 若仍报网络错误，按原始 `ERR_*` 码检查协议、端口、代理认证和目标可达性；不要用账号或 MFA 的修改掩盖网络问题。
4. 既有账号授权成功后，再单独测试一次新注册；开启 2FA 时确认日志经过同 Roxy 的全新密码 + TOTP 登录验证。
5. 成功后再测试凭证刷新与验活，确认没有重新抽取代理或依赖已删除的临时 Profile。

未覆盖：其他驱动的完整端到端流程、跨 Roxy 工作区的旧 Profile 迁移、将清态进一步限制到特定域，以及任务中途热加载全局 2FA 设置的配置快照一致性。
