"""MCWorldHost 救活 / 续期：动态发现 server action id，然后买 runtime。

背景（2026-11-16）：站点重建后硬编码的 action id 全部失效（HTTP 404
"Server action not found."），导致既签不了到、也买不了运行时间，
服务器 runtime_expires 过期后业务全停（AFK 也不计币）。

本脚本的做法：
  1. Playwright 真浏览器登录（过 Cloudflare 挑战）
  2. 扫描 dashboard/shop/coins/afk 四页的 RSC payload + 所有 chunk，
     把 claimDailyLoginAction / spendCoinsAction / redeemCodeAction 等 id 挖出来
  3. 用挖到的 id 调 spendCoins 买 runtime（优先 7 天，余额不足再 24h）
  4. 把发现的 id 和结果写进 state 文件，供后续脚本复用

用法：
  python mcworldhost_rescue.py --dry-run     # 只发现 id + 读状态，不买东西
  python mcworldhost_rescue.py               # 发现 + 尝试续期
环境变量：
  MCWH_EMAIL / MCWH_PASSWORD   必填
  MCWH_PROXY                   可选 socks5://127.0.0.1:40000（走 WARP）
  MCWH_BUY=runtime_7d|runtime_24h  可选，强制指定买哪个
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
PAGES = ["/dashboard", "/dashboard/coins", "/dashboard/shop", "/dashboard/afk"]
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                          "mcwh_actions.json")

# 已知（旧）id，仅作兜底比对
LEGACY = {
    "claimDailyLoginAction": "00db7bd04a2bd65c362187785705f27fc230af9819",
    "spendCoinsAction": "40a6b10028adbf9c211c7386e979767ebdfd1afdee",
    "redeemCodeAction": "40dd66f2759ec291d191fa5c9af0741d690c40026a",
}


def log(m):
    print(m, flush=True)


# ─────────────────────────────────────────────────────────
#  action id 发现
# ─────────────────────────────────────────────────────────
def _fetch_text(page, path):
    try:
        return page.evaluate("async (p) => (await (await fetch(p)).text())", path)
    except Exception as exc:  # noqa: BLE001
        log(f"  fetch {path} failed: {type(exc).__name__}")
        return ""


def discover(page):
    """返回 {name: id}；name 来自 RSC 里紧邻 action id 的标识符。"""
    found = {}

    # ① 页面 RSC payload：action id 常以 "xxxxxxxxxx" 40~50 位 hex 出现，
    #    附近的字符串常量里带名字（如 "claimDailyLoginAction"）
    for path in PAGES:
        html = _fetch_text(page, path)
        if not html:
            continue
        for m in re.finditer(r'[0-9a-f]{40,50}', html):
            aid = m.group(0)
            seg = html[max(0, m.start() - 400):m.start() + 400]
            nm = re.search(r'\\?"([A-Za-z][A-Za-z0-9_]*Action)\\?"', seg)
            if nm:
                found[nm.group(1)] = aid

    # ② 客户端 chunk：createServerReference("<id>", callServer, undef, undef, "<name>")
    html = _fetch_text(page, "/dashboard")
    chunks = sorted(set(re.findall(r'/_next/static/chunks/[^"\'\\ )]+\.js', html)))
    log(f"  扫描 {len(chunks)} 个 chunk…")
    for c in chunks:
        js = _fetch_text(page, c)
        if not js:
            continue
        for m in re.finditer(r'createServerReference\)\(?"([0-9a-f]{40,50})"', js):
            seg = js[m.start():m.start() + 300]
            nm = re.search(r'"([A-Za-z][A-Za-z0-9_]*)"\)', seg)
            if nm:
                found.setdefault(nm.group(1), m.group(1))

    # ③ 兜底：旧 id 如果还在页面里出现，说明没变
    for name, old in LEGACY.items():
        if name in found:
            continue
        for path in PAGES:
            if old in _fetch_text(page, path):
                found[name] = old
                break

    # ④ 新站关键：action id 不在静态资源里，只在真实点击时由客户端 chunk
    #    动态拼出并塞进 Next-Action 头。所以必须真的点一次 Buy 才能捕获。
    if "spendCoinsAction" not in found:
        found.update(_discover_by_click(page))
    return found


def _discover_by_click(page):
    """打开商店页，点一次 Buy，从请求头里抓 Next-Action。

    新站结构下这是唯一可靠的办法：id 是 40 位 hex，在点击前不出现在任何静态文件里。
    """
    caught = {}
    handler = lambda r: (caught.setdefault(
        "id", r.headers.get("next-action")) or caught.setdefault("body", r.post_data)
    ) if (r.method == "POST" and r.url.rstrip("/").endswith("/dashboard/shop")) else None
    page.on("request", handler)
    try:
        log("  ④ 尝试真实点击以捕获 action id…")
        page.goto(f"{BASE}/dashboard/shop", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        clicked = False
        # 优先点可购买的那个按钮（未锁的 runtime 商品）
        for sel in [
            'button:has-text("Buy")',
            'button:has-text("Kaufen")',
            '[role="button"]:has-text("Buy")',
        ]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=8000)
                    clicked = True
                    log(f"     点击了 {sel}")
                    break
            except Exception as exc:  # noqa: BLE001
                log(f"     {sel} 点击失败 {type(exc).__name__}")
        if not clicked:
            log("     ⚠️ 页面上没有可点的 Buy 按钮（商品可能全部已锁）")
        page.wait_for_timeout(8000)
    finally:
        try:
            page.remove_listener("request", handler)
        except Exception:  # noqa: BLE001
            pass

    if caught.get("id"):
        log(f"     ✅ 捕获到 action id: {caught['id'][:16]}…（body={str(caught.get('body'))[:80]}）")
        return {"spendCoinsAction": caught["id"]}
    return {}


# ─────────────────────────────────────────────────────────
#  业务
# ─────────────────────────────────────────────────────────
def call_action(page, path, action_id, args):
    js = """
    async ([path, id, args]) => {
      const r = await fetch(path, {
        method: 'POST',
        headers: {'Next-Action': id, 'Accept': 'text/x-component',
                  'Content-Type': 'text/plain;charset=UTF-8'},
        body: JSON.stringify(args)
      });
      const t = await r.text();
      return {status: r.status, body: t.slice(0, 4000)};
    }
    """
    return page.evaluate(js, [path, action_id, args])


def flight_error(text):
    for m in re.finditer(r'\\?"error\\?"\s*:\s*\\?"([^"\\]+)', text):
        v = m.group(1)
        if not v.startswith("$"):
            return v
    return None


def read_state(page):
    st = {}
    coins = _fetch_text(page, "/dashboard/coins")
    m = re.search(r'\\?"balance\\?":\s*(\d+)', coins)
    if m:
        st["balance"] = int(m.group(1))
    m = re.search(r'\\?"claimedToday\\?":\s*(true|false)', coins)
    if m:
        st["claimed"] = m.group(1) == "true"

    shop = _fetch_text(page, "/dashboard/shop")
    for item in ("runtime_7d", "runtime_24h"):
        seg_m = re.search(r'\\?"id\\?":\s*\\?"' + item + r'\\?"(.{0,700}?)(?=\\?"id\\?":|\Z)',
                          shop, re.S)
        if not seg_m:
            continue
        seg = seg_m.group(1)
        # 新站字段：cost（旧站是 price）
        m = re.search(r'\\?"(?:cost|price)\\?":\s*(\d+)', seg)
        if m:
            st[f"price_{item}"] = int(m.group(1))
        m = re.search(r'\\?"locked\\?":\s*(true|false)', seg)
        if m:
            st[f"locked_{item}"] = m.group(1) == "true"
        m = re.search(r'\\?"lockReason\\?":\s*\\?"([^"\\]*)', seg)
        if m:
            st[f"lockreason_{item}"] = m.group(1)
        # 新站关键：已激活的 7d 把到期时间挂在自己身上
        m = re.search(r'\\?"expiresAt\\?":\s*\\?"([^"\\]+)', seg)
        if m:
            st[f"expires_{item}"] = m.group(1)

    dash = _fetch_text(page, "/dashboard")
    m = re.search(r'\\?"runtimeExpiresAt\\?":\s*\\?"([^"\\]+)', dash)
    if m:
        st["runtime_expires"] = m.group(1)
    m = re.search(r'\\?"isSuspended\\?":\s*(true|false)', dash)
    if m:
        st["suspended"] = m.group(1) == "true"

    # 新站：dashboard 的 runtimeExpiresAt 可能为 null（新建服务器），
    # 此时以「已激活 7d 商品的 expiresAt」为准
    if not st.get("runtime_expires") and st.get("expires_runtime_7d"):
        st["runtime_expires"] = st["expires_runtime_7d"]
        st["expires_source"] = "shop:runtime_7d"
    return st


def hours_left(exp):
    if not exp:
        return None
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
        return (t - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只发现 id 和读状态，不购买")
    args = ap.parse_args()

    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "").strip()
    if not email or not password:
        log("❌ 缺 MCWH_EMAIL / MCWH_PASSWORD")
        return 3

    pw, browser, page = open_session(BASE, wait_seconds=150)
    rc = 0
    try:
        sign_in(page, BASE, email, password, timeout=120000)
        log(f"✅ 已登录：{page.url}")

        st = read_state(page)
        log(f"📊 状态: {json.dumps(st, ensure_ascii=False)}")
        hl = hours_left(st.get("runtime_expires"))
        if hl is not None:
            log(f"⏳ 运行时间剩余 {hl:.1f} 小时"
                + ("（已过期！）" if hl < 0 else ""))
        else:
            log("⏳ 到期时间读不到（新站新建服务器常见），将以余额变化判定购买结果")

        ids = discover(page)
        log(f"🔎 发现 {len(ids)} 个 action id：")
        for k, v in sorted(ids.items()):
            log(f"    {k} = {v[:16]}…")
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ids": ids, "discovered_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f, indent=2)
        except Exception as exc:  # noqa: BLE001
            log(f"  （state 写入失败：{exc}）")

        if args.dry_run:
            log("--dry-run：不购买，结束")
            return 0

        spend = ids.get("spendCoinsAction")
        if not spend:
            log("❌ 没挖到 spendCoinsAction，无法购买（把上面的 id 列表发我）")
            return 4

        balance = st.get("balance") or 0
        want = os.environ.get("MCWH_BUY", "").strip()
        plan = []
        if want:
            plan = [want]
        else:
            p7 = st.get("price_runtime_7d", 500)
            p24 = st.get("price_runtime_24h", 100)
            if balance >= p7:
                plan = ["runtime_7d", "runtime_24h"]
            elif balance >= p24:
                plan = ["runtime_24h", "runtime_7d"]
            else:
                log(f"❌ 余额 {balance} 不够买任何运行时间")
                return 5

        for item in plan:
            price = st.get(f"price_{item}")
            if price is not None and balance < price:
                log(f"⏭️ {item} 需要 {price}，余额 {balance} 不够，跳过")
                continue

            # 基线：到期时间 + 余额
            base_exp = st.get("runtime_expires")
            base_bal = st.get("balance")

            r = call_action(page, "/dashboard/shop", spend, [item])
            err = flight_error(r["body"])
            log(f"🛒 买 {item}: HTTP {r['status']}"
                + (f" | error={err}" if err else f" | 回包 {r['body'][:160]}"))

            # ⚠️ HTTP 200 不等于成功（新站会返回 200 + 无效果）
            time.sleep(5)
            st2 = read_state(page)
            new_exp = st2.get("runtime_expires")
            new_bal = st2.get("balance")
            hl2 = hours_left(new_exp)

            got = False
            why = ""
            if base_exp and new_exp and base_exp != new_exp:
                got, why = True, f"到期 {base_exp} → {new_exp}"
            elif base_bal is not None and new_bal is not None and new_bal < base_bal:
                got = True
                why = f"余额 {base_bal} → {new_bal}（扣 {base_bal - new_bal}）"
            elif st2.get(f"locked_{item}") and not st.get(f"locked_{item}"):
                got, why = True, f"{item} 已变为锁定（激活中）"
            else:
                why = (f"到期 {base_exp}→{new_exp or '读不到'}，"
                       f"余额 {base_bal}→{new_bal}，均无变化")

            log(f"📊 新状态: {json.dumps(st2, ensure_ascii=False)}")
            if hl2 is not None:
                log(f"⏳ 新到期：{new_exp}（剩 {hl2:.1f} 小时）")

            if got:
                log(f"✅ {item} 购买成功：{why}")
                st = st2
                return 0
            log(f"❌ {item} 判定未生效：{why}")
            page.screenshot(path=f"mcwh_debug_buy_{item}.png", full_page=True)
            st = st2
        return 6
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
