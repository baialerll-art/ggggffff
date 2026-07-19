from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE_URL = os.getenv("SITE_URL", "https://api-ai.onyxaxis.org").rstrip("/")
LOGIN_URL = f"{BASE_URL}/sign-in"
PROFILE_PATH = "/profile"
TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "20000"))
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "screenshots"))
HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}

logging.basicConfig(level=logging.DEBUG if os.getenv("DEBUG", "").lower() in {"1", "true", "yes"} else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("axis-ai-checkin")


@dataclass(frozen=True)
class Account:
    name: str
    username: str
    password: str


@dataclass
class Result:
    name: str
    status: str
    message: str
    screenshot: Path | None = None
    elapsed_seconds: float = 0.0
    telegram_error: str | None = None


class CheckinError(RuntimeError):
    pass


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少环境变量 {name}")
    return value


def load_accounts() -> list[Account]:
    raw = required_env("ACCOUNTS_JSON")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ACCOUNTS_JSON 不是有效 JSON: {exc.msg}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("ACCOUNTS_JSON 必须是非空 JSON 数组")

    accounts: list[Account] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个账号必须是 JSON 对象")
        name = str(item.get("name", f"账号{index}")).strip()
        username = str(item.get("username", "")).strip()
        password = item.get("password")
        if not name or not username or not isinstance(password, str) or not password:
            raise ValueError(f"第 {index} 个账号必须包含非空 name、username、password")
        accounts.append(Account(name=name, username=username, password=password))
    return accounts


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:80] or "account"


def visible_locator(page: Page, selectors: Iterable[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=1000):
                return locator
        except Exception:
            continue
    return None


def fill_first(page: Page, selectors: Iterable[str], value: str, field_name: str) -> None:
    locator = visible_locator(page, selectors)
    if locator is None:
        raise CheckinError(f"找不到{field_name}输入框")
    locator.fill(value)


def click_first(page: Page, selectors: Iterable[str], description: str, timeout: int = TIMEOUT_MS) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=1000):
                locator.click(timeout=timeout)
                return
        except Exception:
            continue
    raise CheckinError(f"找不到或无法点击{description}")


def body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def login(page: Page, account: Account) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)

    fill_first(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[autocomplete="username"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="邮箱"]',
        'input[type="text"]',
    ], account.username, "账号")
    fill_first(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
        'input[placeholder*="password" i]',
        'input[placeholder*="密码"]',
    ], account.password, "密码")
    click_first(page, [
        'button[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Login")',
        'button:has-text("登录")',
        'input[type="submit"]',
    ], "登录按钮")

    try:
        page.wait_for_url(lambda url: "/sign-in" not in url, timeout=TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        text = body_text(page)
        if re.search(r"invalid|incorrect|wrong|失败|错误|密码", text, re.IGNORECASE):
            raise CheckinError("登录失败，网站返回了登录错误") from exc
        raise CheckinError("登录后页面未离开 /sign-in") from exc
    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)


def open_profile(page: Page) -> None:
    if PROFILE_PATH:
        page.goto(f"{BASE_URL}/{PROFILE_PATH.lstrip('/')}", wait_until="networkidle", timeout=TIMEOUT_MS)
        return

    profile_link = visible_locator(page, [
        'a:has-text("个人资料")',
        'button:has-text("个人资料")',
        'a:has-text("Profile")',
        'button:has-text("Profile")',
        '[aria-label*="个人资料"]',
        '[aria-label*="Profile" i]',
    ])
    if profile_link is None:
        raise CheckinError("找不到个人资料入口；可通过 PROFILE_PATH 配置直接访问个人资料路径")
    profile_link.click(timeout=TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)


def locate_checkin_button(page: Page):
    exact_names = [
        "立即签到", "Check in now", "Check In Now", "签到", "Check in", "Check-in",
    ]
    for name in exact_names:
        locator = page.get_by_role("button", name=name, exact=True).first
        try:
            if locator.is_visible(timeout=1000):
                return locator
        except Exception:
            continue

    for selector in [
        'button:has-text("立即签到")',
        'button:has-text("Check in now")',
        'button:has-text("Check in")',
        'button:has-text("签到")',
        '[role="button"]:has-text("立即签到")',
        '[role="button"]:has-text("签到")',
    ]:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=1000):
                return locator
        except Exception:
            continue
    return None


def is_already_checked_in(page: Page) -> bool:
    text = body_text(page)
    if re.search(r"已签到|今日已签到|already checked in|checked in today|checked in|checked-in", text, re.IGNORECASE):
        active = locate_checkin_button(page)
        if active is None:
            return True
        try:
            if active.is_disabled():
                return True
        except Exception:
            pass
    for selector in [
        'button:has-text("已签到")',
        'button:has-text("Already checked")',
        'button:has-text("Checked in")',
    ]:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    return False


def perform_checkin(page: Page) -> str:
    if is_already_checked_in(page):
        return "already_checked_in"
    button = locate_checkin_button(page)
    if button is None:
        raise CheckinError("找不到每日签到按钮")
    button.click(timeout=TIMEOUT_MS)
    try:
        page.wait_for_timeout(1200)
        page.wait_for_function(
            """() => /已签到|今日已签到|already checked in|checked in today|checked in|checked-in/i.test(document.body.innerText)""",
            timeout=TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        if not is_already_checked_in(page):
            raise CheckinError("点击签到后未检测到已签到状态") from exc
    return "success"


def capture(page: Page, account_name: str, suffix: str = "after-checkin") -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{safe_filename(account_name)}-{suffix}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def telegram_request(token: str, method: str, *, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} 返回失败: {payload.get('description', 'unknown error')}")
    return payload


def send_message(token: str, chat_id: str, text: str) -> None:
    telegram_request(token, "sendMessage", data={"chat_id": chat_id, "text": text})


def send_photo(token: str, chat_id: str, photo: Path, caption: str) -> None:
    with photo.open("rb") as handle:
        telegram_request(token, "sendPhoto", data={"chat_id": chat_id, "caption": caption}, files={"photo": (photo.name, handle, "image/png")})


def notify(token: str, chat_id: str, result: Result) -> None:
    status_label = {"success": "签到成功", "already_checked_in": "今日已签到", "failed": "签到失败"}.get(result.status, result.status)
    text = f"Axis AI 签到通知\n账号：{result.name}\n结果：{status_label}\n耗时：{result.elapsed_seconds:.1f} 秒\n说明：{result.message}"
    if result.screenshot and result.screenshot.exists():
        try:
            send_photo(token, chat_id, result.screenshot, text)
            return
        except Exception as exc:
            result.telegram_error = f"截图发送失败: {type(exc).__name__}"
    send_message(token, chat_id, text + "\n截图未能发送。")


def run_account(browser: Browser, account: Account) -> Result:
    started = time.monotonic()
    context: BrowserContext | None = None
    page: Page | None = None
    result = Result(name=account.name, status="failed", message="未完成")
    try:
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN", timezone_id="Asia/Hong_Kong")
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        login(page, account)
        open_profile(page)
        result.status = perform_checkin(page)
        result.message = "页面已显示签到完成状态" if result.status == "success" else "页面显示今天已经签到"
        result.screenshot = capture(page, account.name)
    except Exception as exc:
        result.status = "failed"
        result.message = f"{type(exc).__name__}: {str(exc)[:240]}"
        if page is not None:
            try:
                result.screenshot = capture(page, account.name, "failure")
            except Exception:
                pass
        LOGGER.error("账号 %s 执行失败: %s", account.name, result.message)
    finally:
        result.elapsed_seconds = time.monotonic() - started
        if context is not None:
            context.close()
    return result


def main() -> int:
    try:
        accounts = load_accounts()
        token = required_env("TELEGRAM_BOT_TOKEN")
        chat_id = required_env("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        LOGGER.error("配置错误: %s", exc)
        return 2

    results: list[Result] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        try:
            for account in accounts:
                LOGGER.info("开始处理账号: %s", account.name)
                result = run_account(browser, account)
                results.append(result)
                try:
                    notify(token, chat_id, result)
                except Exception as exc:
                    result.telegram_error = f"通知发送失败: {type(exc).__name__}"
                    LOGGER.error("账号 %s Telegram 通知失败: %s", account.name, result.telegram_error)
        finally:
            browser.close()

    summary_lines = ["Axis AI 每日签到汇总"]
    for result in results:
        label = {"success": "成功", "already_checked_in": "已签到", "failed": "失败"}.get(result.status, result.status)
        summary_lines.append(f"- {result.name}: {label}")
    try:
        send_message(token, chat_id, "\n".join(summary_lines))
    except Exception as exc:
        LOGGER.error("汇总通知发送失败: %s", type(exc).__name__)

    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
