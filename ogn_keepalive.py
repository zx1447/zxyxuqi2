#!/usr/bin/env python3
"""OpenGameNode 面板（Calagopus）守护：真浏览器登录 → 读服务器与 ogn-session。

登录：有界面 Chromium（Xvfb），走正常的登录页流程，Turnstile 由页面自己完成。
      如果页面要求交互式挑战，本脚本直接失败退出，不自动解题。
之后的所有 API 调用都在页面内用 fetch 发出（同源、带会话 cookie）。

模式（环境变量 OGN_MODE）：
  probe（默认）：只读。打印服务器与 ogn-session 的结构，不做任何写操作。
  act：离线就 start；会话到期前且有续期额度时才 extend。
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://panel.opengamenode.com"
LOGIN = f"{BASE}/auth/login"
EMAIL = os.environ.get("OGN_EMAIL", "").strip()
PASSWORD = os.environ.get("OGN_PASSWORD", "")
MODE = os.environ.get("OGN_MODE", "probe").strip().lower()
TURNSTILE_WAIT = int(os.environ.get("OGN_TURNSTILE_WAIT", "120"))


def log(msg):
    print(msg, flush=True)


def wait_turnstile(page):
    """等待页面自己的 Turnstile 产生 token。不点击、不解题。"""
    deadline = time.time() + TURNSTILE_WAIT
    while time.time() < deadline:
        try:
            tok = page.evaluate(
                "() => { const i = document.querySelector('input[name=\"cf-turnstile-response\"]');"
                " return i ? i.value : ''; }")
        except Exception:  # noqa: BLE001
            tok = ""
        if tok:
            return True
        page.wait_for_timeout(3000)
    return False


def login(page):
    page.goto(LOGIN, wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("textbox").first.fill(EMAIL)
    page.get_by_role("button", name=re.compile(r"continue|weiter|continuar", re.I)).first.click()
    page.wait_for_timeout(2500)
    if not wait_turnstile(page):
        raise RuntimeError("Turnstile 没有自动通过（可能需要交互式挑战），停止")
    pw_box = page.locator("input[type='password']").first
    pw_box.fill(PASSWORD)
    page.get_by_role("button", name=re.compile(r"login|sign in|anmelden|log in", re.I)).first.click()
    page.wait_for_timeout(6000)
    if "/auth/login" in page.url:
        raise RuntimeError(f"仍在登录页（url={page.url}），登录未完成")
    log(f"✅ 登录完成 url={page.url}")


def api(page, method, path, body=None):
    js = """
    async ([m, p, b]) => {
      const r = await fetch(p, {method: m, credentials: 'include',
        headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
        body: b === null ? undefined : JSON.stringify(b)});
      const t = await r.text();
      return {status: r.status, body: t.slice(0, 4000)};
    }
    """
    return page.evaluate(js, [method, path, body])


def main():
    if not EMAIL or not PASSWORD:
        log("❌ 缺 OGN_EMAIL / OGN_PASSWORD")
        return 3
    os.environ.setdefault("DISPLAY", ":99")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--no-sandbox"])
        context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        try:
            login(page)
            lst = api(page, "GET", "/api/client/servers")
            log(f"servers status={lst['status']}")
            log(lst["body"][:1500])
            data = json.loads(lst["body"]) if lst["status"] == 200 else {}
            servers = (data.get("data") or data.get("servers", {}).get("data") or []) if isinstance(data, dict) else []
            for s in servers:
                uuid = s.get("uuid")
                name = s.get("name")
                st = api(page, "GET", f"/api/client/servers/{uuid}/resources")
                log(f"[{name}] resources status={st['status']} {st['body'][:600]}")
                ss = api(page, "GET", f"/api/client/servers/{uuid}/ogn-session")
                log(f"[{name}] ogn-session status={ss['status']}")
                log(ss["body"][:2500])
                if MODE == "act":
                    log("（act 模式的写操作在看到字段结构后再启用）")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
