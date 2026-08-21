# GPTMail 邮箱来源 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GPTMail as an on-demand registration email source, configured by a WebUI-managed API key.

**Architecture:** `core/gptmail_client.py` will own GPTMail HTTP calls, temporary mailbox context, and OTP polling. `core/email_provider.py` will route the new `gptmail` source beside existing providers. The existing dynamic configuration UI will render one new secret environment-backed key after it is added to `EDITABLE_FIELDS`.

**Tech Stack:** Python 3.10+, `requests`, standard-library `unittest` and `unittest.mock`, Flask WebUI configuration layer.

## Global Constraints

- GPTMail endpoint is fixed at `https://mail.chatgpt.org.uk`; do not expose a Base URL, prefix, or domain field.
- Use `GET /api/generate-email`, `GET /api/emails`, and `GET /api/email/{id}`, always sending `X-API-Key`.
- Never use or fall back to a public test key; a missing `GPTMAIL_API_KEY` must produce a clear Chinese configuration error.
- Do not persist GPTMail addresses in the local email pool and do not call the delete or clear remote endpoints.
- Keep existing Outlook, generic API, and Cloudflare-domain providers unchanged.

---

## File Structure

- Create `core/gptmail_client.py`: GPTMail protocol adapter, in-memory account context, OTP polling.
- Modify `core/email_provider.py`: recognize, acquire, resolve, poll, and release the `gptmail` source.
- Modify `config/email.py`: define `GPTMAIL_API_KEY` and load it from `.env`.
- Modify `config/env_loader.py`: register the key as a documented secret environment variable.
- Modify `apps/web/config_editor.py`: expose one masked `GPTMAIL_API_KEY` setting in the existing “邮箱 / OTP” group.
- Create `tests/test_gptmail_client.py`: client HTTP, missing-key, and OTP polling tests.
- Create `tests/test_email_provider_gptmail.py`: provider routing tests.
- Create `tests/test_gptmail_config.py`: configuration metadata and environment override tests.
- Modify `.env.example` and `README.md`: user setup instructions.

### Task 1: GPTMail client and OTP polling

**Files:**
- Create: `core/gptmail_client.py`
- Create: `tests/test_gptmail_client.py`

**Interfaces:**
- Consumes: `config.email.GPTMAIL_API_KEY`, `OTP_MAX_WAIT`, `OTP_POLL_INTERVAL`, `OTP_SETTLE_SECONDS`, `core.otp_utils.looks_like_openai_email`, and `core.otp_utils.extract_otp`.
- Produces: `GPTMailError`, `GPTMailAccount(email: str)`, `pick_account() -> GPTMailAccount`, `get_account_context(email: str) -> GPTMailAccount | None`, `fetch_latest_otp(email: str, after_ts: float | None = None, max_wait: int | None = None, poll_interval: int | None = None, settle_seconds: int | None = None) -> str`, and `release_account(email: str, ...) -> None`.

- [x] **Step 1: Write the failing client tests**

Create `tests/test_gptmail_client.py` with a fake HTTP response and these behavior tests:

```python
import unittest
from unittest.mock import Mock, patch

from core import gptmail_client


class GPTMailClientTests(unittest.TestCase):
    def setUp(self):
        gptmail_client._CONTEXT_CACHE.clear()

    def test_pick_account_requires_configured_api_key(self):
        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", ""):
            with self.assertRaisesRegex(gptmail_client.GPTMailError, "请填写 GPTMail API Key"):
                gptmail_client.pick_account()

    @patch("core.gptmail_client.requests.get")
    def test_pick_account_generates_random_mailbox_with_key(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"success": True, "data": {"email": "fresh@gptmail.test"}}
        get.return_value = response
        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123"):
            account = gptmail_client.pick_account()

        self.assertEqual(account.email, "fresh@gptmail.test")
        get.assert_called_once_with(
            "https://mail.chatgpt.org.uk/api/generate-email",
            headers={"Accept": "application/json", "X-API-Key": "key-123"},
            params=None,
            timeout=20,
        )

    @patch("core.gptmail_client.time.sleep")
    @patch("core.gptmail_client.requests.get")
    def test_fetch_latest_otp_reads_only_new_openai_email(self, get, sleep):
        inbox = Mock(status_code=200)
        inbox.json.return_value = {
            "success": True,
            "data": {"emails": [
                {"id": "old", "timestamp": 100, "from_address": "noreply@openai.com", "subject": "Code 111111"},
                {"id": "new", "timestamp": 205, "from_address": "noreply@openai.com", "subject": "Code 654321"},
            ]},
        }
        detail = Mock(status_code=200)
        detail.json.return_value = {
            "success": True,
            "data": {"id": "new", "timestamp": 205, "from_address": "noreply@openai.com", "subject": "Code 654321", "content": "Your code is 654321"},
        }
        get.side_effect = [inbox, detail]
        with patch.object(gptmail_client._email_cfg, "GPTMAIL_API_KEY", "key-123"):
            code = gptmail_client.fetch_latest_otp(
                "fresh@gptmail.test", after_ts=200, max_wait=1, poll_interval=1, settle_seconds=0
            )

        self.assertEqual(code, "654321")
```

- [x] **Step 2: Run the tests to verify they fail because the module is absent**

Run: `python -m unittest tests.test_gptmail_client -v`

Expected: import failure naming `core.gptmail_client`.

- [x] **Step 3: Implement the minimal client**

Create `core/gptmail_client.py` with the following rules:

```python
BASE_URL = "https://mail.chatgpt.org.uk"
REQUEST_TIMEOUT = 20
_CONTEXT_CACHE: dict[str, GPTMailAccount] = {}


class GPTMailError(RuntimeError):
    pass


@dataclass
class GPTMailAccount:
    email: str


def _headers() -> dict[str, str]:
    api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
    if not api_key:
        raise GPTMailError("GPTMail API Key 未配置，请在 WebUI「配置 → 邮箱 / OTP」中填写 GPTMail API Key。")
    return {"Accept": "application/json", "X-API-Key": api_key}


def _get(path: str, params: dict | None = None) -> dict:
    try:
        response = requests.get(BASE_URL + path, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GPTMailError(f"GPTMail 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
        message = payload.get("error") if isinstance(payload, dict) else response.text[:160]
        raise GPTMailError(f"GPTMail 请求失败 ({path}): HTTP {response.status_code}; {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GPTMailError(f"GPTMail 响应缺少对象 data ({path})")
    return data
```

Implement `pick_account()` using `_get("/api/generate-email")`, require a non-empty string `data["email"]`, cache the resulting account, and return it. Implement `get_account_context()` as a case-insensitive cache lookup and `release_account()` by removing the cached address. Implement `fetch_latest_otp()` by: polling `/api/emails` with `params={"email": email}` until the configured deadline; sorting `data["emails"]` descending by numeric `timestamp`; ignoring messages whose timestamp is below `after_ts - 30`; skipping non-OpenAI messages via `looks_like_openai_email`; loading `/api/email/{id}`; mapping GPTMail fields to `from`, `text`, and `html` aliases for `extract_otp`; and returning the latest candidate only after the supplied/default settle period. Raise `GPTMailError` with the last request or empty-inbox reason after timeout.

- [x] **Step 4: Run client tests to verify they pass**

Run: `python -m unittest tests.test_gptmail_client -v`

Expected: all three `GPTMailClientTests` pass.

- [x] **Step 5: Commit the client deliverable**

```bash
git add core/gptmail_client.py tests/test_gptmail_client.py
git commit -m "feat: add GPTMail email client"
```

### Task 2: Provider routing for GPTMail

**Files:**
- Modify: `core/email_provider.py`
- Create: `tests/test_email_provider_gptmail.py`

**Interfaces:**
- Consumes: Task 1’s `pick_account`, `get_account_context`, `fetch_latest_otp`, and `release_account`.
- Produces: `parse_email_sources()` accepts `gptmail`; `acquire_email()`, `resolve_email_source()`, `wait_for_otp()`, and `release_email()` route GPTMail addresses correctly.

- [x] **Step 1: Write the failing provider tests**

Create `tests/test_email_provider_gptmail.py`:

```python
import unittest
from unittest.mock import patch

from core import email_provider


class GPTMailProviderTests(unittest.TestCase):
    def test_parse_sources_keeps_gptmail_in_order(self):
        self.assertEqual(email_provider.parse_email_sources("outlook,gptmail,generic_api"), ["outlook", "gptmail", "generic_api"])

    @patch("core.gptmail_client.pick_account")
    def test_acquire_email_uses_gptmail_client(self, pick_account):
        pick_account.return_value.email = "fresh@gptmail.test"
        with patch("core.email_provider.parse_email_sources", return_value=["gptmail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@gptmail.test")

    @patch("core.gptmail_client.get_account_context", return_value=object())
    def test_resolve_email_source_recognizes_cached_gptmail_address(self, get_context):
        self.assertEqual(email_provider.resolve_email_source("fresh@gptmail.test"), "gptmail")
        get_context.assert_called_once_with("fresh@gptmail.test")

    @patch("core.gptmail_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="gptmail")
    def test_release_email_clears_gptmail_context(self, resolve, release):
        self.assertEqual(email_provider.release_email("fresh@gptmail.test", status="failed"), "gptmail")
        release.assert_called_once_with("fresh@gptmail.test", status="failed", note=None)
```

- [x] **Step 2: Run the tests to verify the new source is rejected**

Run: `python -m unittest tests.test_email_provider_gptmail -v`

Expected: the parser assertion fails because `gptmail` is not in `_VALID_SOURCES`.

- [x] **Step 3: Implement source dispatch**

In `core/email_provider.py`, change the source tuple and branch ordering exactly as follows:

```python
_VALID_SOURCES = ("outlook", "generic_api", "cloudflare_domain", "gptmail")


def _pick_from_source(source: str) -> str:
    if source == "gptmail":
        from core.gptmail_client import pick_account
        return pick_account().email
    # retain the existing cloudflare, generic_api, and outlook branches
```

Before DB lookups in `resolve_email_source()`, add:

```python
from core.gptmail_client import get_account_context as get_gptmail_context
if get_gptmail_context(email):
    return "gptmail"
```

Add a `gptmail` branch in `wait_for_otp()` that imports and calls `core.gptmail_client.fetch_latest_otp(email, after_ts=after_ts)`. Add a matching branch in `release_email()` that calls `core.gptmail_client.release_account(email, status=status, note=note)` before returning the source.

- [x] **Step 4: Run provider tests to verify they pass**

Run: `python -m unittest tests.test_email_provider_gptmail -v`

Expected: all four `GPTMailProviderTests` pass.

- [x] **Step 5: Commit provider routing**

```bash
git add core/email_provider.py tests/test_email_provider_gptmail.py
git commit -m "feat: route GPTMail email source"
```

### Task 3: Web configuration and user documentation

**Files:**
- Modify: `config/email.py`
- Modify: `config/env_loader.py`
- Modify: `apps/web/config_editor.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `tests/test_gptmail_config.py`

**Interfaces:**
- Consumes: existing `apply_env_overrides`, `SECRET_ENV_KEYS`, and `EDITABLE_FIELDS` patterns.
- Produces: runtime `config.email.GPTMAIL_API_KEY` and one masked WebUI setting in group `邮箱 / OTP` with key `GPTMAIL_API_KEY`.

- [x] **Step 1: Write the failing configuration tests**

Create `tests/test_gptmail_config.py`:

```python
import unittest
from pathlib import Path

from config import email
from apps.web.config_editor import EDITABLE_FIELDS


class GPTMailConfigTests(unittest.TestCase):
    def test_email_config_declares_gptmail_api_key_with_empty_default(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn('GPTMAIL_API_KEY = env_str("GPTMAIL_API_KEY", "")', source)

    def test_webui_exposes_gptmail_key_as_secret_env_field(self):
        field = next(item for item in EDITABLE_FIELDS if item["key"] == "GPTMAIL_API_KEY")
        self.assertEqual(field["group"], "邮箱 / OTP")
        self.assertTrue(field["secret"])
        self.assertEqual(field["storage"], "env")
```

- [x] **Step 2: Run the tests to verify they fail because the key is missing**

Run: `python -m unittest tests.test_gptmail_config -v`

Expected: attribute lookup or field search fails for `GPTMAIL_API_KEY`.

- [x] **Step 3: Add the configuration and documentation**

In `config/email.py`, declare and load the key:

```python
GPTMAIL_API_KEY = env_str("GPTMAIL_API_KEY", "")

apply_env_overrides(globals(), {
    # keep every existing mapping unchanged
    "GPTMAIL_API_KEY": "str",
})
```

Add `"GPTMAIL_API_KEY": "GPTMail API Key"` to `config/env_loader.py`’s `SECRET_ENV_KEYS`. Add this field beside the existing email secrets in `apps/web/config_editor.py`:

```python
{
    "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
    "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
    "storage": "env", "secret": True,
},
```

Add `GPTMAIL_API_KEY=` in the `.env.example` “邮箱 / OTP” section with a Chinese comment. Update `README.md` to list GPTMail under email sources and add a “GPTMail 临时邮箱” subsection that instructs users to set `EMAIL_SOURCE = "gptmail"` and add the key through WebUI “配置 → 邮箱 / OTP” or `.env`. Explicitly document that the key is mandatory and the service address is fixed.

- [x] **Step 4: Run configuration tests to verify they pass**

Run: `python -m unittest tests.test_gptmail_config -v`

Expected: both `GPTMailConfigTests` pass.

- [x] **Step 5: Run the full unit-test suite and inspect the configuration diff**

Run: `python -m unittest discover -s tests -v && git diff --check`

Expected: all tests pass and `git diff --check` prints no whitespace errors.

- [x] **Step 6: Commit the configuration and documentation deliverable**

```bash
git add config/email.py config/env_loader.py apps/web/config_editor.py .env.example README.md tests/test_gptmail_config.py
git commit -m "feat: configure GPTMail email provider"
```

### Review follow-up: OTP settle and WebUI preflight

**Files:**
- Modify: `core/gptmail_client.py`
- Modify: `apps/web/app.py`
- Modify: `tests/test_gptmail_client.py`
- Create: `tests/test_webui_gptmail.py`

- [x] Add a regression test proving that a repeated identical inbox message does not restart the nonzero OTP settle window.
- [x] Reset the settle timer only for a newer message timestamp or a changed OTP at the same timestamp.
- [x] Add WebUI tests that reject a missing GPTMail Key before jobs are queued, omit local-pool warnings for a configured GPTMail source, and exclude GPTMail from Outlook-pool totals.
- [x] Add the preflight and summary handling, then run all 16 tests, compile Python sources, and check the diff for whitespace errors.
