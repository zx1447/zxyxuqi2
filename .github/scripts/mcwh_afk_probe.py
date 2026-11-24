"""MCWorldHost AFK 诊断：真浏览器（有界面 Chromium + Xvfb）+ WARP 出口，登录后停在挂机页观察。

只观察，不伪造任何状态、不做模拟点击：
  · 记录挂机页发出的广告/统计相关请求（域名、状态码）
  · 每 60 秒记录页面上的余额与今日计数文字
  · 结束时保存截图与摘要，供 artifact 下载
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
AFK_PAGE = "/dashboard/afk"
MINUTES = int(os.environ.get("PROBE_MINUTES", "10"))
AD_HINT = re.compile(r"adsterra|highperformanceformat|ad|track|stat|afk|ping|heartbeat|earn|coin|reward|beacon", re.I)


def log(msg):
    print(msg, flush=True)


def main():
    email = os.environ.get("MCWH_EMAIL", "")
    password = os.environ.get("MCWH_PASSWORD", "")
    if not email or not password:
        log("❌ 缺 MCWH_EMAIL / MCWH_PASSWORD")
        return 3

    pw, browser, page = open_session(BASE)
    try:
        sign_in(page, BASE, email, password)
        log(f"✅ 登录完成，url={page.url}")

        hits = {}
        def on_response(resp):
            url = resp.url
            if AD_HINT.search(url) and not url.endswith((".css", ".woff2", ".png", ".jpg", ".svg", ".ico")):
                host = re.sub(r"^https?://([^/]+).*", r"\1", url)
                key = f"{resp.request.method} {host} {resp.status}"
                hits[key] = hits.get(key, 0) + 1

        page.on("response", on_response)
        page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()
        page.wait_for_timeout(10000)
        page.screenshot(path="mcwh_debug_afk_start.png", full_page=True)

        start_txt = page.locator("body").inner_text()[:3000]
        log("---- 挂机页文字（开始）----")
        log(start_txt[:1200])

        for minute in range(1, MINUTES + 1):
            page.wait_for_timeout(60000)
            try:
                txt = page.locator("body").inner_text()[:3000]
            except Exception as exc:  # noqa: BLE001
                txt = f"<读取失败 {type(exc).__name__}>"
            nums = re.findall(r"(?i)(?:balance|coins?|guthaben|münzen|today|heute)[^\n]{0,40}", txt)
            log(f"[{minute}min] url={page.url} visible={page.evaluate('document.visibilityState')} "
                f"hidden={page.evaluate('document.hidden')} 关键行={nums[:4]}")

        page.screenshot(path="mcwh_debug_afk_end.png", full_page=True)
        end_txt = page.locator("body").inner_text()[:3000]
        log("---- 挂机页文字（结束）----")
        log(end_txt[:1200])
        log("---- 广告/统计相关请求汇总 ----")
        if not hits:
            log("（没有任何匹配请求）")
        for k, v in sorted(hits.items(), key=lambda x: -x[1])[:40]:
            log(f"{v:4d}  {k}")
        return 0
    finally:
        try:
            browser.close()
        finally:
            pw.stop()


if __name__ == "__main__":
    sys.exit(main())
