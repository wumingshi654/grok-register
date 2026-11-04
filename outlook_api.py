"""Outlook external API client, mailbox pool, and task runtime.

Supports:
1. Account fetching (accounts mode) via GET /api/external/accounts with X-API-Key,
   local concurrency reservation, and round-robin or random selection.
2. Email and OTP polling via GET /api/external/emails?email=...&folder=...&top=...
   with X-API-Key and local regex code extraction.
3. Post-registration auto-disable via PUT /api/accounts/{id} (status=inactive)
   and group moving on failure/timeout via POST /api/accounts/batch-update-group,
   authenticated via POST /api/extension/login (with Web password) and GET /api/csrf-token.
4. Temporary mailbox (temp mode) via GET /api/temp-emails and
   GET /api/temp-emails/{email}/messages.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import random
import re
import secrets
import threading
import time
from typing import Callable, Optional, Union
import urllib.parse

import requests

from outlook_mail import extract_verification_code

_API_HANDLE_PREFIX = "outlook:api:"
LogCallback = Optional[Callable[[str], None]]


def _clean_api_base(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base and not (base.startswith("http://") or base.startswith("https://")):
        if base.startswith("localhost") or base.startswith("127.0.0.1") or ":" in base.split("/")[0]:
            base = "http://" + base
        else:
            base = "https://" + base
    for suffix in (
        "/api/external/accounts",
        "/api/external/emails",
        "/api/external",
        "/api/temp-emails",
        "/api/csrf-token",
        "/api/extension/login",
    ):
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)].rstrip("/")
            break
    return base


def _canonical_url(base_url: str, path: str = "") -> str:
    base = _clean_api_base(base_url)
    if not path:
        return base
    if not path.startswith("/"):
        path = "/" + path
    if base.lower().endswith("/api") and path.lower().startswith("/api/"):
        base = base[:-4]
    return base + path


class OutlookApiError(RuntimeError):
    """Outlook API request or business failure."""


@dataclass
class OutlookApiAccount:
    email: str
    id: Optional[Union[int, str]] = None
    status: str = "active"
    group_id: Optional[Union[int, str]] = None
    raw: dict = field(default_factory=dict)


class OutlookApiClient:
    """HTTP client for external Outlook account management and mail querying."""

    def __init__(
        self,
        api_base: str,
        api_key: str = "",
        web_password: str = "",
        timeout: float = 15.0,
        log_callback: LogCallback = None,
    ) -> None:
        self.api_base = _clean_api_base(api_base)
        self.api_key = str(api_key or "").strip()
        self.web_password = str(web_password or "").strip()
        self.timeout = max(3.0, float(timeout or 15.0))
        self.log_callback = log_callback
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })
        self._csrf_token: Optional[str] = None
        self._session_lock = threading.Lock()
        self._logged_in = False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    def _log(self, message: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:
                pass

    def _auth_headers(self, additional: Optional[dict] = None) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if additional:
            headers.update(additional)
        return headers

    def _parse_json(self, resp: requests.Response, context: str = "") -> Any:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raw_text = str(getattr(resp, "text", "") or "")
            snippet = raw_text[:300].strip().replace("\r", "").replace("\n", " ")
            raise OutlookApiError(
                f"Outlook API [{context or getattr(resp, 'url', '')}] 请求失败 (HTTP {getattr(resp, 'status_code', 0)}): {snippet or exc}"
            ) from exc

        try:
            return resp.json()
        except Exception as exc:
            content_type = ""
            if hasattr(resp, "headers") and resp.headers:
                try:
                    content_type = str(resp.headers.get("Content-Type", "")).lower()
                except Exception:
                    pass
            raw_text = str(getattr(resp, "text", "") or "").strip()
            snippet = raw_text[:300].replace("\r", "").replace("\n", " ")
            if not raw_text:
                raise OutlookApiError(
                    f"Outlook API [{context or getattr(resp, 'url', '')}] 返回了空响应 (HTTP {getattr(resp, 'status_code', 0)})"
                ) from exc
            if raw_text.startswith("<") or "html" in content_type:
                raise OutlookApiError(
                    f"Outlook API [{context or getattr(resp, 'url', '')}] 返回了 HTML 页面而非 JSON (HTTP {getattr(resp, 'status_code', 0)})。\n"
                    f"当前请求地址: {getattr(resp, 'url', '')}\n"
                    f"可能原因: 1. API Base URL 填写不正确或被反向代理/前端单页路由拦截；2. 需确认服务端口和路径是否正确。\n"
                    f"响应内容预览: {snippet}"
                ) from exc
            raise OutlookApiError(
                f"Outlook API [{context or getattr(resp, 'url', '')}] 返回了非 JSON 格式响应 (HTTP {getattr(resp, 'status_code', 0)})。\n"
                f"当前请求地址: {getattr(resp, 'url', '')}\n"
                f"响应内容预览: {snippet}"
            ) from exc

    def login(self) -> bool:
        """Authenticate via Web password using /api/extension/login and obtain session."""
        if not self.web_password:
            return False
        with self._session_lock:
            login_url = _canonical_url(self.api_base, "/api/extension/login")
            try:
                resp = self._session.post(
                    login_url,
                    json={"password": self.web_password},
                    timeout=self.timeout,
                )
                if resp.status_code == 404:
                    # Fallback to standard web login
                    login_url = _canonical_url(self.api_base, "/login")
                    resp = self._session.post(
                        login_url,
                        json={"password": self.web_password},
                        timeout=self.timeout,
                    )
                resp.raise_for_status()
                try:
                    data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
                except Exception:
                    data = {}
                launch_url = data.get("launch_url") if isinstance(data, dict) else None
                if launch_url:
                    exchange_url = urllib.parse.urljoin(self.api_base + "/", str(launch_url).lstrip("/"))
                    exch_resp = self._session.get(exchange_url, timeout=self.timeout, allow_redirects=True)
                    exch_resp.raise_for_status()
                self._logged_in = True
                self._csrf_token = None
                self._fetch_csrf_token_locked()
                return True
            except Exception as exc:
                self._log(f"[!] Outlook API Web 登录失败: {exc}")
                raise OutlookApiError(f"Outlook API Web 登录失败: {exc}") from exc

    def _fetch_csrf_token_locked(self) -> str:
        csrf_url = _canonical_url(self.api_base, "/api/csrf-token")
        try:
            resp = self._session.get(csrf_url, timeout=self.timeout)
            resp.raise_for_status()
            token = ""
            try:
                data = resp.json()
                if isinstance(data, dict):
                    token = (
                        data.get("csrf_token")
                        or data.get("csrfToken")
                        or data.get("token")
                        or (data.get("data") if isinstance(data.get("data"), dict) else {}).get("csrf_token")
                        or ""
                    )
            except Exception:
                token = ""
        except Exception:
            token = ""
        if not token:
            token = (
                self._session.cookies.get("csrf_token")
                or self._session.cookies.get("csrftoken")
                or self._session.cookies.get("XSRF-TOKEN")
                or ""
            )
        self._csrf_token = str(token or "")
        return self._csrf_token

    def get_csrf_token(self, force_refresh: bool = False) -> str:
        with self._session_lock:
            if not self._csrf_token or force_refresh:
                if not self._logged_in and self.web_password:
                    self._session_lock.release()
                    try:
                        self.login()
                    finally:
                        self._session_lock.acquire()
                else:
                    self._fetch_csrf_token_locked()
            return self._csrf_token or ""

    def _web_request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Execute an authenticated management request with CSRF token and auto re-login."""
        if not self._logged_in and self.web_password:
            self.login()
        url = _canonical_url(self.api_base, path)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json, text/plain, */*")
        token = self.get_csrf_token()
        if token:
            headers["X-CSRFToken"] = token
            headers["X-CSRF-Token"] = token

        resp = self._session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        if resp.status_code in (401, 403) and self.web_password:
            # Session expired, re-login and retry once
            self.login()
            headers["X-CSRFToken"] = self.get_csrf_token()
            headers["X-CSRF-Token"] = self.get_csrf_token()
            resp = self._session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        return resp

    def get_accounts(self, group_id: Optional[Union[int, str]] = None) -> list[OutlookApiAccount]:
        """Fetch account list via GET /api/external/accounts."""
        url = _canonical_url(self.api_base, "/api/external/accounts")
        headers = self._auth_headers()
        params = {}
        if group_id not in (None, ""):
            params["group_id"] = str(group_id).strip()
            params["group"] = str(group_id).strip()

        resp = self._session.get(url, headers=headers, params=params, timeout=self.timeout)
        data = self._parse_json(resp, context=f"GET {url}")
        raw_list = []
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            if data.get("success") is False and (data.get("message") or data.get("error")):
                msg = data.get("message") or data.get("error")
                raise OutlookApiError(f"Outlook API 获取账号失败: {msg}")
            raw_list = (
                data.get("accounts")
                or data.get("data")
                or data.get("items")
                or data.get("list")
                or []
            )

        accounts: list[OutlookApiAccount] = []
        for item in raw_list:
            if isinstance(item, dict):
                email = str(item.get("email") or item.get("username") or "").strip()
                if not email or "@" not in email:
                    continue
                acc_id = item.get("id") or item.get("account_id") or item.get("_id")
                status = str(item.get("status") or "active").strip().lower()
                acc_group = item.get("group_id") or item.get("groupId") or item.get("group")
                accounts.append(
                    OutlookApiAccount(
                        email=email,
                        id=acc_id,
                        status=status,
                        group_id=acc_group,
                        raw=item,
                    )
                )
            elif isinstance(item, str):
                email = item.strip()
                if email and "@" in email:
                    accounts.append(OutlookApiAccount(email=email))
        return accounts

    def get_emails(
        self,
        email: str,
        folder: str = "inbox",
        top: int = 10,
    ) -> list[dict]:
        """Fetch emails via GET /api/external/emails?email=...&folder=...&top=..."""
        url = _canonical_url(self.api_base, "/api/external/emails")
        headers = self._auth_headers()
        params = {
            "email": str(email or "").strip(),
            "folder": str(folder or "inbox").strip(),
            "top": max(1, int(top or 10)),
        }
        resp = self._session.get(url, headers=headers, params=params, timeout=self.timeout)
        data = self._parse_json(resp, context=f"GET {url}")
        if isinstance(data, list):
            return [msg for msg in data if isinstance(msg, dict)]
        if isinstance(data, dict):
            if data.get("success") is False and (data.get("message") or data.get("error")):
                self._log(f"[!] Outlook API 获取邮件提示: {data.get('message') or data.get('error')}")
            raw = (
                data.get("emails")
                or data.get("messages")
                or data.get("data")
                or data.get("items")
                or []
            )
            if isinstance(raw, list):
                return [msg for msg in raw if isinstance(msg, dict)]
        return []

    def update_account_status(
        self,
        account_id: Union[int, str],
        status: str = "inactive",
    ) -> bool:
        """Update account status via PUT /api/accounts/{id}."""
        if account_id in (None, ""):
            return False
        path = f"/api/accounts/{account_id}"
        resp = self._web_request("PUT", path, json={"status": str(status or "inactive")})
        resp.raise_for_status()
        return True

    def batch_update_group(
        self,
        account_ids: list[Union[int, str]],
        group_id: Union[int, str],
    ) -> bool:
        """Move accounts to specified group via POST /api/accounts/batch-update-group."""
        valid_ids = [aid for aid in account_ids if aid not in (None, "")]
        if not valid_ids or group_id in (None, ""):
            return False
        parsed_group: Union[int, str] = group_id
        try:
            parsed_group = int(str(group_id).strip())
        except (ValueError, TypeError):
            parsed_group = str(group_id).strip()

        resp = self._web_request(
            "POST",
            "/api/accounts/batch-update-group",
            json={"account_ids": valid_ids, "group_id": parsed_group},
        )
        resp.raise_for_status()
        return True

    def get_temp_emails(self) -> list[Union[dict, str]]:
        """Fetch temporary email list via GET /api/temp-emails."""
        url = _canonical_url(self.api_base, "/api/temp-emails")
        headers = self._auth_headers()
        resp = self._session.get(url, headers=headers, timeout=self.timeout)
        data = self._parse_json(resp, context=f"GET {url}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if data.get("success") is False and (data.get("message") or data.get("error")):
                self._log(f"[!] Outlook API 获取临时邮箱提示: {data.get('message') or data.get('error')}")
            return (
                data.get("temp_emails")
                or data.get("emails")
                or data.get("data")
                or data.get("items")
                or []
            )
        return []

    def generate_temp_email(self) -> Optional[str]:
        """Optionally generate a new temp email via POST /api/temp-emails/generate."""
        try:
            resp = self._web_request("POST", "/api/temp-emails/generate", json={})
            resp.raise_for_status()
            data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
            if isinstance(data, dict):
                return data.get("email") or data.get("address") or (data.get("data") if isinstance(data.get("data"), dict) else {}).get("email")
        except Exception as exc:
            self._log(f"[!] 生成临时邮箱失败: {exc}")
        return None

    def get_temp_email_messages(self, email: str) -> list[dict]:
        """Fetch messages for temporary email via GET /api/temp-emails/{email}/messages."""
        quoted_email = urllib.parse.quote(str(email or "").strip(), safe="")
        url = _canonical_url(self.api_base, f"/api/temp-emails/{quoted_email}/messages")
        headers = self._auth_headers()
        resp = self._session.get(url, headers=headers, timeout=self.timeout)
        data = self._parse_json(resp, context=f"GET {url}")
        if isinstance(data, list):
            return [msg for msg in data if isinstance(msg, dict)]
        if isinstance(data, dict):
            if data.get("success") is False and (data.get("message") or data.get("error")):
                self._log(f"[!] Outlook API 获取临时邮件提示: {data.get('message') or data.get('error')}")
            raw = (
                data.get("messages")
                or data.get("emails")
                or data.get("data")
                or data.get("items")
                or []
            )
            if isinstance(raw, list):
                return [msg for msg in raw if isinstance(msg, dict)]
        return []


class OutlookApiAccountPool:
    """In-memory concurrency reservation pool for accounts fetched from API."""

    def __init__(
        self,
        accounts: list[OutlookApiAccount],
        strategy: str = "round_robin",
    ) -> None:
        if not accounts:
            raise ValueError("Outlook API 邮箱池没有有效账号")
        self._accounts = list(accounts)
        self._by_email = {acc.email.lower(): acc for acc in self._accounts}
        self.strategy = "random" if str(strategy or "").strip().lower() == "random" else "round_robin"
        self._in_use: set[str] = set()
        self._completed: set[str] = set()
        self._round_robin_idx = 0
        self._lock = threading.RLock()

    @property
    def count(self) -> int:
        return len(self._accounts)

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._accounts) - len(self._completed)

    def get_account(self, email: str) -> Optional[OutlookApiAccount]:
        return self._by_email.get(str(email or "").strip().lower())

    def acquire(self) -> OutlookApiAccount:
        with self._lock:
            available = [
                acc for acc in self._accounts
                if acc.email.lower() not in self._in_use and acc.email.lower() not in self._completed
            ]
            if not available:
                if len(self._completed) >= len(self._accounts):
                    raise RuntimeError("Outlook 邮箱池已耗尽，所有账号均已使用完成")
                raise RuntimeError("Outlook 邮箱池暂无可用账号，所有剩余账号均处于并发预留中")

            if self.strategy == "random":
                chosen = random.choice(available)
            else:
                chosen = None
                total = len(self._accounts)
                for _ in range(total):
                    candidate = self._accounts[self._round_robin_idx % total]
                    self._round_robin_idx = (self._round_robin_idx + 1) % total
                    if candidate.email.lower() not in self._in_use and candidate.email.lower() not in self._completed:
                        chosen = candidate
                        break
                if chosen is None:
                    chosen = available[0]

            self._in_use.add(chosen.email.lower())
            return chosen

    def release_unsubmitted(self, email: str) -> None:
        """Release reservation if browser fails prior to form submission."""
        with self._lock:
            self._in_use.discard(str(email or "").strip().lower())

    def mark_completed(self, email: str) -> None:
        """Mark account as completed (either succeeded or permanently failed)."""
        with self._lock:
            key = str(email or "").strip().lower()
            self._in_use.discard(key)
            self._completed.add(key)


class OutlookApiTaskRuntime:
    """Outlook task runtime implementing the same lease interface as OutlookTaskRuntime."""

    def __init__(
        self,
        api_client: OutlookApiClient,
        mode: str = "accounts",
        strategy: str = "round_robin",
        group_id: Union[int, str] = "",
        failed_group_id: Union[int, str] = "",
        mail_folder: str = "inbox",
        mail_top: int = 10,
        auto_disable_on_success: bool = True,
        move_group_on_failure: bool = True,
        log_callback: LogCallback = None,
        cancelled_exception: Optional[type] = None,
    ) -> None:
        self.api_client = api_client
        self.mode = "temp" if str(mode or "").strip().lower() == "temp" else "accounts"
        self.strategy = strategy
        self.group_id = str(group_id or "").strip()
        self.failed_group_id = str(failed_group_id or "").strip()
        self.mail_folder = str(mail_folder or "inbox").strip()
        self.mail_top = max(1, int(mail_top or 10))
        self.auto_disable_on_success = bool(auto_disable_on_success)
        self.move_group_on_failure = bool(move_group_on_failure)
        self.log_callback = log_callback
        self.cancelled_exception = cancelled_exception

        self._lock = threading.RLock()
        self._leases: dict[str, OutlookApiAccount] = {}
        self._closed = False

        self._initialize_pool()

    def _log(self, message: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:
                pass

    def _initialize_pool(self) -> None:
        if getattr(self.api_client, "web_password", None) and not getattr(self.api_client, "is_logged_in", False):
            try:
                self.api_client.login()
            except Exception as exc:
                self._log(f"[*] Outlook API Web 登录提示: {exc}")

        if self.mode == "accounts":
            accounts = self.api_client.get_accounts(group_id=self.group_id)
            if self.group_id:
                # In-memory filter as well in case API did not filter
                target = str(self.group_id).strip().lower()
                filtered = [
                    acc for acc in accounts
                    if acc.group_id is None or str(acc.group_id).strip().lower() == target
                ]
                if filtered:
                    accounts = filtered
            # Exclude already inactive accounts if any
            active_accounts = [acc for acc in accounts if acc.status != "inactive"]
            if active_accounts:
                accounts = active_accounts
            if not accounts:
                group_hint = f" (分组: {self.group_id})" if self.group_id else ""
                raise OutlookApiError(f"Outlook API 未获取到可用账号{group_hint}，请检查账号池中是否存在 active 状态账号")
            self._pool = OutlookApiAccountPool(accounts, strategy=self.strategy)
            self._log(f"[*] Outlook API (accounts 模式) 已加载 {len(accounts)} 个可用账号")
        else:
            raw_temp = self.api_client.get_temp_emails()
            accounts: list[OutlookApiAccount] = []
            for item in raw_temp:
                if isinstance(item, dict):
                    email = str(item.get("email") or item.get("address") or "").strip()
                    if email and "@" in email:
                        accounts.append(OutlookApiAccount(email=email, id=item.get("id"), raw=item))
                elif isinstance(item, str) and "@" in item:
                    accounts.append(OutlookApiAccount(email=item.strip()))
            if not accounts:
                generated = self.api_client.generate_temp_email()
                if generated:
                    accounts.append(OutlookApiAccount(email=generated))
            if not accounts:
                raise OutlookApiError("Outlook API (temp 模式) 未获取到任何临时邮箱")
            self._pool = OutlookApiAccountPool(accounts, strategy=self.strategy)
            self._log(f"[*] Outlook API (temp 模式) 已加载 {len(accounts)} 个临时邮箱")

    def _raise_if_cancelled(self, cancel_callback=None) -> None:
        if not cancel_callback or not cancel_callback():
            return
        if self.cancelled_exception is not None:
            raise self.cancelled_exception("用户停止注册")
        raise RuntimeError("任务已停止")

    @property
    def count(self) -> int:
        return self._pool.count

    @property
    def remaining(self) -> int:
        return self._pool.remaining

    def acquire(self) -> tuple[str, str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Outlook API 任务运行时已关闭")
            account = self._pool.acquire()
            handle = _API_HANDLE_PREFIX + secrets.token_urlsafe(24)
            self._leases[handle] = account
            return account.email, handle

    def release_before_submit(self, handle: str, email: str) -> bool:
        with self._lock:
            key = str(handle or "")
            account = self._leases.get(key)
            if account is None:
                return False
            if account.email.lower() != str(email or "").lower():
                raise RuntimeError("Outlook 邮箱会话与目标邮箱不匹配")
            del self._leases[key]
            self._pool.release_unsubmitted(account.email)
            return True

    def _extract_code_from_messages(self, messages: list[dict]) -> Optional[str]:
        patterns = [
            r"(?:confirmation|verification|security|one[-\s]?time)\s*(?:code|pin|passcode)\s*(?:is\s*[:：]?|[:：])\s*([A-Z0-9]{3}-[A-Z0-9]{3}|(?=[A-Z0-9]{0,7}\d)[A-Z0-9]{4,8})",
            r"(?:your\s+code|code|pin|passcode)\s*(?:is\s*[:：]?|[:：])\s*([A-Z0-9]{3}-[A-Z0-9]{3}|(?=[A-Z0-9]{0,7}\d)[A-Z0-9]{4,8})",
            r"(?:验证码|确认码|校验码|一次性密码)\s*(?:是|为|[:：])?\s*([A-Z0-9]{3}-[A-Z0-9]{3}|(?=[A-Z0-9]{0,7}\d)[A-Z0-9]{4,8})",
            r"([A-Z0-9]{3}-[A-Z0-9]{3}|(?=[A-Z0-9]{0,7}\d)[A-Z0-9]{4,8})[\s\S]{0,80}?(?:is\s+your\s+(?:confirmation|verification|security)?\s*(?:code|pin|passcode)|(?:confirmation|verification)\s*code|作为您的验证码)",
        ]
        for msg in messages:
            subject = str(msg.get("subject") or "")
            text = str(msg.get("text") or msg.get("body") or msg.get("content") or "")
            html = str(msg.get("html") or "")
            sender = str(msg.get("sender") or msg.get("from") or msg.get("from_address") or "")
            combined = "\n".join(str(v or "") for v in (subject, text, html))
            for pattern in patterns:
                m = re.search(pattern, combined, re.I | re.S)
                if m:
                    return m.group(1).replace("-", "").strip().upper() if "-" not in m.group(1) else m.group(1).strip().upper()

            import mail_service
            body = text if text.strip() else html
            code = mail_service.extract_verification_code(body, subject=subject)
            if not code:
                from outlook_mail import extract_verification_code as outlook_extract
                code = outlook_extract(subject=subject, text=text, html=html, sender=sender)
            if code:
                return code
        return None

    def wait_for_code(
        self,
        handle: str,
        email: str,
        timeout: int = 180,
        poll_interval: int = 3,
        log_callback=None,
        cancel_callback=None,
        resend_callback=None,
    ) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("Outlook API 任务运行时已关闭")
            key = str(handle or "")
            account = self._leases.get(key)
            if account is None:
                raise RuntimeError("Outlook 邮箱会话不存在、已使用或已失效")
            if account.email.lower() != str(email or "").lower():
                raise RuntimeError("Outlook 邮箱会话与目标邮箱不匹配")
            del self._leases[key]

        target_email = account.email
        deadline = time.monotonic() + max(float(timeout or 180), 5.0)
        interval = max(1.0, float(poll_interval or 3))
        resend_attempted = False

        try:
            while time.monotonic() < deadline:
                self._raise_if_cancelled(cancel_callback)
                try:
                    if self.mode == "accounts":
                        messages = self.api_client.get_emails(
                            email=target_email,
                            folder=self.mail_folder,
                            top=self.mail_top,
                        )
                    else:
                        messages = self.api_client.get_temp_email_messages(email=target_email)

                    code = self._extract_code_from_messages(messages)
                    if code:
                        self._pool.mark_completed(target_email)
                        return str(code)
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[!] Outlook API 轮询邮件异常: {exc}")

                # Check if resend is appropriate halfway through timeout
                if resend_callback and not resend_attempted and (deadline - time.monotonic() <= timeout / 2.0):
                    resend_attempted = True
                    try:
                        resend_callback()
                    except Exception as exc:
                        if log_callback:
                            log_callback(f"[!] 触发验证码重新发送失败: {exc}")

                sleep_until = min(deadline, time.monotonic() + interval)
                while time.monotonic() < sleep_until:
                    self._raise_if_cancelled(cancel_callback)
                    time.sleep(min(0.2, max(0.0, sleep_until - time.monotonic())))

            self._raise_if_cancelled(cancel_callback)
            # Timeout reached: perform timeout action
            self.on_failure(target_email, reason="timeout")
            from registration_flow import VerificationCodeUnavailable
            raise VerificationCodeUnavailable(f"Outlook 在 {timeout}s 内未收到验证码邮件")
        except Exception:
            self._raise_if_cancelled(cancel_callback)
            raise

    def on_success(self, email: str) -> None:
        """Called upon successful registration to auto-disable the account."""
        account = self._pool.get_account(email)
        self._pool.mark_completed(email)
        if not account or account.id in (None, ""):
            return
        if self.auto_disable_on_success:
            try:
                self.api_client.update_account_status(account.id, "inactive")
                self._log(f"[+] Outlook API 账号 {email} (ID: {account.id}) 状态已自动置为 inactive")
            except Exception as exc:
                self._log(f"[!] Outlook API 自动停用账号 {email} 失败: {exc}")

    def on_failure(self, email: str, reason: str = "") -> None:
        """Called upon registration timeout or failure to move account to designated group."""
        account = self._pool.get_account(email)
        self._pool.mark_completed(email)
        if not account or account.id in (None, ""):
            return
        if self.move_group_on_failure and self.failed_group_id:
            try:
                self.api_client.batch_update_group([account.id], self.failed_group_id)
                self._log(f"[*] Outlook API 账号 {email} (ID: {account.id}) 已移动至失败分组 {self.failed_group_id}")
            except Exception as exc:
                self._log(f"[!] Outlook API 移动账号 {email} 分组失败: {exc}")

    def status(self) -> dict:
        with self._lock:
            return {
                "count": self.count,
                "remaining": self.remaining,
                "leased": len(self._leases),
                "closed": self._closed,
                "mode": self.mode,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._leases.clear()


def create_outlook_api_task_runtime(
    config: dict,
    log_callback: LogCallback = None,
    cancelled_exception: Optional[type] = None,
) -> OutlookApiTaskRuntime:
    """Create Outlook API task runtime from application config dictionary."""
    api_base = str(config.get("outlook_api_base") or "").strip()
    api_key = str(config.get("outlook_api_key") or "").strip()
    web_password = str(config.get("outlook_web_password") or "").strip()
    mode = str(config.get("outlook_api_mode") or "accounts").strip().lower()
    strategy = str(config.get("outlook_account_strategy") or "round_robin").strip().lower()
    group_id = str(config.get("outlook_group_id") or "").strip()
    failed_group_id = str(config.get("outlook_failed_group_id") or "").strip()
    mail_folder = str(config.get("outlook_mail_folder") or "inbox").strip()
    mail_top = int(config.get("outlook_mail_top", 10) or 10)
    auto_disable = bool(config.get("outlook_auto_disable_on_success", True))
    move_group = bool(config.get("outlook_move_group_on_failure", True))

    client = OutlookApiClient(
        api_base=api_base,
        api_key=api_key,
        web_password=web_password,
        log_callback=log_callback,
    )
    return OutlookApiTaskRuntime(
        api_client=client,
        mode=mode,
        strategy=strategy,
        group_id=group_id,
        failed_group_id=failed_group_id,
        mail_folder=mail_folder,
        mail_top=mail_top,
        auto_disable_on_success=auto_disable,
        move_group_on_failure=move_group,
        log_callback=log_callback,
        cancelled_exception=cancelled_exception,
    )


def probe_outlook_api(config: dict) -> dict:
    """Preflight check for Outlook external API connectivity and accounts."""
    api_base = str(config.get("outlook_api_base") or "").strip()
    if not api_base:
        raise ValueError("缺少 outlook_api_base 配置")
    api_key = str(config.get("outlook_api_key") or "").strip()
    web_password = str(config.get("outlook_web_password") or "").strip()
    mode = str(config.get("outlook_api_mode") or "accounts").strip().lower()
    group_id = str(config.get("outlook_group_id") or "").strip()

    client = OutlookApiClient(api_base=api_base, api_key=api_key, web_password=web_password)
    login_ok = False
    if web_password:
        try:
            login_ok = client.login()
        except Exception as exc:
            login_ok = False

    if mode == "accounts":
        if not api_key:
            raise ValueError("Outlook accounts 模式需要配置 outlook_api_key")
        accounts = client.get_accounts(group_id=group_id)
        return {
            "ok": True,
            "mode": "accounts",
            "count": len(accounts),
            "web_login": login_ok,
            "accounts": [{"email": acc.email, "id": acc.id, "status": acc.status} for acc in accounts[:10]],
        }
    else:
        temp_emails = client.get_temp_emails()
        return {
            "ok": True,
            "mode": "temp",
            "count": len(temp_emails),
            "web_login": login_ok,
            "emails": temp_emails[:10],
        }
