"""MCWorldHost 购买 runtime：用页面抓到的 Next-Action id 直接 POST。

站点（2026-11-16 起）的购买是 Next.js Server Action：
  POST /dashboard/shop
  Next-Action: 405281bd003bbbf17f25a1190a2641292fce6cc951
  Accept: text/x-component
  Content-Type: text/plain;charset=UTF-8
  body: ["runtime_7d"]
本脚本同时支持「自动发现」：点一次 Buy 按钮拦请求拿 id，然后复用。
"""
import json, os, sys, time, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in

BASE = "https://mcworldhost.de"
SHOP = "/dashboard/shop"
KNOWN_ACTION = os.environ.get("MCWH_SPEND_ACTION", "").strip()

def log(m): print(m, flush=True)

def hours_left(exp):
    if not exp: return None
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(exp.replace("Z","+00:00"))
        return (t - dt.datetime.now(dt.timezone.utc)).total_seconds()/3600
    except Exception: return None


def hl_fmt(hl, nd=2):
    """安全格式化小时数：None/非数字时返回 '?'，避免 NoneType.__format__ 崩溃。"""
    try:
        return f"{float(hl):.{nd}f}"
    except (TypeError, ValueError):
        return "?"


def main():
    # 商品优先级：MCWH_ITEM > MCWH_BUY > 默认 runtime_7d
    item = (os.environ.get("MCWH_ITEM") or os.environ.get("MCWH_BUY")
            or "runtime_7d").strip()
    log(f"🎯 目标商品: {item}")
    email, password = os.environ["MCWH_EMAIL"], os.environ["MCWH_PASSWORD"]
    pw, browser, page = open_session(BASE, wait_seconds=150)
    caught = {}
    page.on("request", lambda r: caught.update({"id": r.headers.get("next-action"),
                                                "post": r.post_data}) if r.method=="POST" and r.url.endswith("/dashboard/shop") else None)
    try:
        sign_in(page, BASE, email, password, timeout=120000)
        log(f"✅ 登录 {page.url}")

        def dash():
            html = page.evaluate("async()=>(await(await fetch('/dashboard')).text())")
            m = re.search(r'\\?"runtimeExpiresAt\\?":\s*\\?"([^"\\]+)', html)
            return m.group(1) if m else None

        def balance():
            """读 coins 余额；到期时间读不到时用它判断购买是否生效。"""
            try:
                html = page.evaluate("async()=>(await(await fetch('/dashboard/coins')).text())")
                m = re.search(r'\\?"balance\\?":\s*(\d+)', html)
                return int(m.group(1)) if m else None
            except Exception:  # noqa: BLE001
                return None
        exp0 = dash(); bal0 = balance()
        log(f"⏳ 购买前到期: {exp0}（剩 {hl_fmt(hours_left(exp0))} h）｜余额 {bal0}")

        action = KNOWN_ACTION
        if not action:
            page.goto(BASE+SHOP, wait_until="domcontentloaded"); page.wait_for_timeout(5000)
            try:
                page.get_by_role("button", name=re.compile("^Buy$", re.I)).first.click()
                page.wait_for_timeout(6000)
            except Exception as e:
                log(f"⚠️ 点击捕获失败（{type(e).__name__}），用内置 id")
            action = caught.get("id") or "405281bd003bbbf17f25a1190a2641292fce6cc951"
        log(f"🔑 使用 Next-Action: {action}")

        res = page.evaluate("""
        async ([act, item]) => {
          const r = await fetch('/dashboard/shop', {
            method:'POST',
            headers:{'Next-Action':act,'Accept':'text/x-component',
                     'Content-Type':'text/plain;charset=UTF-8'},
            body: JSON.stringify([item])
          });
          const t = await r.text();
          return {status:r.status, body:t.slice(0,3000)};
        }""", [action, item])
        log(f"🛒 POST {item} → HTTP {res['status']}")
        log(f"   回包: {res['body'][:600]}")

        time.sleep(6)
        exp1 = dash(); bal1 = balance()
        hl = hours_left(exp1)
        log(f"⏳ 购买后到期: {exp1}（剩 {hl_fmt(hl)} h）｜余额 {bal1}")

        # 判定购买是否生效：
        #   1) 到期时间有变化 → 成功
        #   2) 到期时间读不到（官网改版常见）→ 看余额是否扣减
        if exp0 is not None and exp1 is not None:
            ok = exp0 != exp1
            why = f"到期 {exp0} → {exp1}"
        elif bal0 is not None and bal1 is not None:
            ok = bal1 < bal0
            why = f"余额 {bal0} → {bal1}（扣了 {bal0 - bal1}）"
        else:
            ok = False
            why = "到期时间与余额都无法读取，无法判定"
        log(("✅ 续期成功：" if ok else "❌ 购买未生效：") + why)
        page.screenshot(path="mcwh_debug_buy.png", full_page=True)
        return 0 if ok else 7
    finally:
        try: browser.close(); pw.stop()
        except Exception: pass

if __name__ == "__main__":
    sys.exit(main())
