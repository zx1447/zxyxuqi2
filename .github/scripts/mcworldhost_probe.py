"""探测 mcworldhost 的购买交互：列出按钮 + 捕获 server action 请求头。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in

BASE = "https://mcworldhost.de"
def log(m): print(m, flush=True)

def main():
    email = os.environ["MCWH_EMAIL"]; password = os.environ["MCWH_PASSWORD"]
    pw, browser, page = open_session(BASE, wait_seconds=150)
    captured = []
    page.on("request", lambda r: captured.append({
        "url": r.url, "method": r.method,
        "headers": {k.lower(): v for k, v in r.headers.items()
                    if k.lower() in ("next-action", "content-type", "accept")},
        "post": (r.post_data or "")[:300]}) if r.method == "POST" else None)
    try:
        sign_in(page, BASE, email, password, timeout=120000)
        log(f"✅ 登录 {page.url}")
        for path in ["/dashboard/shop", "/dashboard/coins"]:
            log(f"\n===== {path} =====")
            page.goto(BASE + path, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            # 列出所有可见按钮 / 链接 / 文本
            try:
                btns = page.eval_on_selector_all(
                    "button, a[role=button], [data-slot=button]",
                    "els => els.map(e => ({t: (e.innerText||'').trim().slice(0,60), d: e.disabled, cls: (e.className||'').slice(0,60)}))")
                for b in btns[:40]:
                    log(f"  BTN {b}")
            except Exception as e:
                log(f"  btn err {e}")
            # 页面里含 runtime/coin 关键词的片段
            try:
                body = page.locator("body").inner_text()
                log("  ---- body text ----")
                for line in body.splitlines():
                    if line.strip(): log(f"    {line.strip()[:120]}")
            except Exception as e:
                log(f"  body err {e}")
        log("\n===== 捕获的 POST 请求 =====")
        for c in captured:
            log(json.dumps(c, ensure_ascii=False))
        # 尝试点 runtime_7d 按钮，捕获请求
        log("\n===== 尝试点击 runtime_7d 相关按钮 =====")
        page.goto(BASE + "/dashboard/shop", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        captured.clear()
        clicked = False
        for kw in ["7 day", "7天", "7 Days", "Runtime", "Buy"]:
            try:
                loc = page.get_by_role("button", name=__import__("re").compile(kw, __import__("re").I))
                n = loc.count()
                log(f"  keyword {kw!r}: {n} buttons")
                if n and not clicked:
                    log(f"  clicking first {kw!r} button")
                    loc.first.click()
                    page.wait_for_timeout(6000)
                    clicked = True
                    break
            except Exception as e:
                log(f"  {kw} err {type(e).__name__}")
        log("  --- 点击后捕获 ---")
        for c in captured:
            log(json.dumps(c, ensure_ascii=False))
        page.screenshot(path="mcwh_debug_probe.png", full_page=True)
    finally:
        try: browser.close(); pw.stop()
        except Exception: pass

if __name__ == "__main__":
    main()
