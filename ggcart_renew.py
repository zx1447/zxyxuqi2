#!/usr/bin/env python3
"""GGCart 免费服精准续期（Community Free，2 GB，7 天周期）。

站点规则（2026-11-23 站点公告 + 实测）：
  · 续期只在当前周期的最后 48 小时内开放。
  · 控制台 /dashboard/discord → Your claims → Community 的 Active 记录 → 「Renew weekly」。
  · 按钮一直可点，但窗口外后端会拒绝。所以这里先读到期时间，窗口外绝不点击。

流程：登录（邮箱密码）→ 读 Community 到期时间（UTC）→
      剩余 > 48h：只打印，退出 0（不发写请求）
      剩余 ≤ 48h：点击 Renew weekly → 刷新 → 到期时间变晚才算成功。
环境变量：GG_EMAIL、GG_PASS（不写进代码）。
退出码：0 正常（含窗口外）；2 续期失败/解析失败；3 缺凭据。
"""
import datetime as dt
import os
import re
import sys

from playwright.sync_api import sync_playwright

EMAIL = os.environ.get("GG_EMAIL", "").strip()
PWD = os.environ.get("GG_PASS", "")
WINDOW = dt.timedelta(hours=48)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
EXPIRY_RE = re.compile(
    r"Expires\s+(\d{1,2})/(\d{1,2})/(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)")


def parse_expiry(text):
    """页面显示的是 UTC（context timezone_id=UTC）。返回 aware datetime 或 None。"""
    m = EXPIRY_RE.search(text)
    if not m:
        return None
    mo, d, y, hh, mm, ss, ap = m.groups()
    hour = int(hh) % 12 + (12 if ap == "PM" else 0)
    return dt.datetime(int(y), int(mo), int(d), hour, int(mm), int(ss), tzinfo=dt.timezone.utc)


def login_and_read(page):
    page.goto("https://ggcart.in/auth", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", PWD)
    page.click("button:has-text('Sign in')")
    page.wait_for_url(re.compile(r".*/dashboard.*"), timeout=60000)
    page.goto("https://ggcart.in/dashboard/discord", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    return page.inner_text("body")


def main():
    if not EMAIL or not PWD:
        print("❌ 缺 GG_EMAIL / GG_PASS")
        return 3
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, timezone_id="UTC",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            text = login_and_read(page)
            exp = parse_expiry(text)
            now = dt.datetime.now(dt.timezone.utc)
            if exp is None:
                print("❌ 没读到到期时间（页面结构可能变了）")
                return 2
            remain = exp - now
            print(f"Community Free 到期 {exp:%Y-%m-%d %H:%M} UTC，剩余 {remain}", flush=True)
            if remain < dt.timedelta(0):
                print("❌ 已过期：本脚本不处理重新激活", flush=True)
                return 2
            if remain > WINDOW:
                print(f"⏳ 未进入续期窗口（需剩余 ≤ 48h，窗口开放于 {exp - WINDOW:%Y-%m-%d %H:%M} UTC），不点击", flush=True)
                return 0

            print("🔁 进入续期窗口，点击 Renew weekly", flush=True)
            btn = page.locator("button:has-text('Renew weekly')").first
            with page.expect_response(lambda r: "/_serverFn/" in r.url, timeout=60000) as info:
                btn.click()
            resp = info.value
            print(f"续期请求: HTTP {resp.status}", flush=True)
            page.wait_for_timeout(4000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(7000)
            new_exp = parse_expiry(page.inner_text("body"))
            print(f"续期后到期: {exp:%Y-%m-%d %H:%M} → {new_exp:%Y-%m-%d %H:%M} UTC" if new_exp else
                  "❌ 续期后没读到到期时间", flush=True)
            if new_exp and new_exp > exp:
                print("✅ 续期成功（到期时间已延长）", flush=True)
                return 0
            print("❌ 续期未生效（到期时间未变）", flush=True)
            return 2
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
