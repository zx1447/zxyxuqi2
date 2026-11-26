"""MCWorldHost 每日流水线：签到 → （够 500 就续 7 天）→ 挂 AFK 刷币。

三条硬规则（2026-11 定）：
  1. 只买「运行时间延长」—— runtime_7d / runtime_24h，其它商品一律拒单（ALLOWED_ITEMS）。
  2. 单次挂机拉满 6 小时（GitHub job 上限 360 分钟，故设 340 分钟留装环境余量）。
  3. 连续 2 轮（约 8 分钟）没有 +币 就判定「拿不到积分」，立刻收工等下一轮，
     不再无效空转。

为什么要代理出口：GitHub runner 是机房 IP，Adsterra 直接 403，AFK 页的广告探测
拿不到 ok → 服务端一分不计（诊断跑 43673423044 实证：浏览器开满 6 分钟 285→285）。
现走订阅的 VLESS 隧道出口（mcwh_subscription_proxy.py 生成 xray 配置），
此前 WARP 出口实测每分钟稳定 +3。

币是「真实广告展示」换来的，本脚本绝不伪造 ad-status postMessage。
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in, _debug_snapshot  # noqa: E402

BASE = "https://mcworldhost.de"
COINS_PAGE = "/dashboard/coins"
AFK_PAGE = "/dashboard/afk"
RELOAD_EVERY = 240      # 站点要求每 5 分钟刷新一次以重新 arm
DEFAULT_MAX_MINUTES = 8
DAY_CAP = 500

# Next.js server-action id 是部署指纹的一部分：站点每次重新部署都会换 id
# （2026-11-26 实证：旧 id → claim 返回 HTTP 404）。所以运行时从页面 chunk
# 里自动发现；下面的值只是发现失败时的兜底（2026-11 版）。
FALLBACK_IDS = {
    "claimDailyLoginAction": "00db7bd04a2bd65c362187785705f27fc230af9819",
    "spendCoinsAction": "40a6b10028adbf9c211c7386e979767ebdfd1afdee",
}
ACTION_NAMES = ("claimDailyLoginAction", "spendCoinsAction")

_DISCOVER_JS = """
async (names) => {
  const urls = new Set();
  for (const u of performance.getEntriesByType('resource')) {
    if (u.name && u.name.includes('/_next/')) urls.add(u.name);
  }
  for (const path of ['/dashboard/coins', '/dashboard/shop']) {
    try {
      const html = await (await fetch(path)).text();
      for (const m of html.matchAll(/src="([^"]*\\/_next\\/[^"]+\\.js[^"]*)"/g)) {
        urls.add(new URL(m[1], location.origin).href);
      }
    } catch (e) {}
  }
  const found = {};
  for (const url of urls) {
    let js = '';
    try { js = await (await fetch(url)).text(); } catch (e) { continue; }
    for (const name of names) {
      if (found[name]) continue;
      const idx = js.indexOf('"' + name + '"');
      if (idx < 0) continue;
      const win = js.slice(Math.max(0, idx - 220), idx);
      const ids = win.match(/[0-9a-f]{40}/g);
      if (ids && ids.length) found[name] = ids[ids.length - 1];
    }
    if (names.every(n => found[n])) break;
  }
  return found;
}
"""


def discover_action_ids(page):
    """在页面里扫已加载/引用的 Next.js chunk，找出当前部署的 action id。"""
    try:
        found = page.evaluate(_DISCOVER_JS, list(ACTION_NAMES)) or {}
    except Exception as exc:
        log(f"action-id discovery failed: {type(exc).__name__}; using fallback ids")
        found = {}
    ids = {n: found.get(n) or FALLBACK_IDS.get(n, "") for n in ACTION_NAMES}
    log(f"action ids discovered: {len(found)}/{len(ACTION_NAMES)}")
    return ids


def log(msg):
    print(msg, flush=True)


def call_action(page, path, action_id, args):
    """Fire a Next.js server action from inside the page (same-origin, cf_clearance present)."""
    js = """
    async ([path, id, args]) => {
      const r = await fetch(path, {
        method: 'POST',
        headers: {
          'Next-Action': id,
          'Accept': 'text/x-component',
          'Content-Type': 'text/plain;charset=UTF-8'
        },
        body: JSON.stringify(args)
      });
      const t = await r.text();
      return {status: r.status, body: t.slice(0, 6000)};
    }
    """
    return page.evaluate(js, [path, action_id, args])


def _page_html(page, path):
    try:
        return page.evaluate(
            "async (p) => (await (await fetch(p)).text())", path
        )
    except Exception as exc:
        log(f"fetch {path} failed: {type(exc).__name__}")
        return ""


def read_state(page):
    """Scrape balance/claim from /dashboard/coins, boost lock from /dashboard/shop,
    runtimeExpiresAt from /dashboard."""
    st = {}
    coins = _page_html(page, COINS_PAGE)
    m = re.search(r'\\?"balance\\?":\s*(\d+)', coins)
    if m:
        st["balance"] = int(m.group(1))
    m = re.search(r'\\?"claimedToday\\?":\s*(true|false)', coins)
    if m:
        st["claimed"] = m.group(1) == "true"

    shop = _page_html(page, "/dashboard/shop")
    m = re.search(r'\\?"id\\?":\s*\\?"runtime_7d\\?"(.{0,400}?)\\?"locked\\?":\s*(true|false)', shop, re.S)
    if m:
        st["runtime_7d_locked"] = m.group(2) == "true"

    dash = _page_html(page, "/dashboard")
    m = re.search(r'\\?"runtimeExpiresAt\\?":\s*\\?"([^"\\]+)', dash)
    if m:
        st["runtime_expires"] = m.group(1)
    m = re.search(r'\\?"isSuspended\\?":\s*(true|false)', dash)
    if m:
        st["suspended"] = m.group(1) == "true"
    m = re.search(r'\\?"dailyReward\\?":\s*(\d+),\s*\\?"streak', dash)
    if m:
        st["daily_reward"] = int(m.group(1))
    return st


NO_EARN_STRIKES = 2          # 连续 2 轮（约 8 分钟）没进账就判定「拿不到积分」


def farm(page, deadline):
    """挂机直到 deadline；连续 NO_EARN_STRIKES 轮没有 +币 就认输返回。

    返回 (本轮赚到多少, 退出原因, 期间见过的最大广告加载标记数)。
    """
    try:
        page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded")
    except Exception as exc:
        log(f"afk nav failed: {type(exc).__name__}")
        return 0, "nav-failed", 0
    page.wait_for_timeout(12000)
    st0 = afk_state(page)
    start = st0.get("balance")
    log(f"AFK watch started: balance={start}, today={st0.get('earnedToday')}/{st0.get('maxEarnPerDay') or DAY_CAP}, ad_markers={st0.get('_ad_markers')}")
    if start is None:
        return 0, "no-balance-readout", st0.get("_ad_markers", 0)
    # 开跑就已经满额，直接收工，不用先耗一个周期去发现
    if st0.get("dayLimitReached") is True or (st0.get("earnedToday") or 0) >= DAY_CAP:
        log("今日 500 币已满 -> 本轮不挂机，等明天")
        return 0, "day-cap", st0.get("_ad_markers", 0)

    last = start
    strikes = 0
    max_ad = st0.get("_ad_markers", 0)
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        page.wait_for_timeout(min(RELOAD_EVERY, remaining) * 1000)
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        if "/dashboard/afk" not in page.url:
            log("kicked off the AFK route; navigating back")
            try:
                page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded")
            except Exception:
                continue

        st = afk_state(page)
        max_ad = max(max_ad, st.get("_ad_markers", 0))
        if st.get("dayLimitReached") is True:
            log(f"DAILY CAP REACHED: {st.get('earnedToday')}/{st.get('maxEarnPerDay') or DAY_CAP}")
            log("今日 500 币已满 -> 停手，等明天")
            return last - start, "day-cap", max_ad
        today = st.get("earnedToday")
        b = st.get("balance")
        if today is not None and today >= DAY_CAP:
            log(f"DAILY CAP REACHED: {today}/{DAY_CAP} -> 停手")
            return last - start, "day-cap", max_ad

        if b is None:
            log("balance readout missing; retry next cycle")
            continue
        if b > last:
            if strikes:
                log(f"earning recovered after {strikes} silent cycle(s)")
            strikes = 0
            log(f"balance {last} -> {b} (+{b - last})")
            last = b
        elif b < last:
            log(f"balance dropped {last} -> {b} (purchase elsewhere?)")
            last = b
            strikes = 0
        else:
            strikes += 1
            log(f"no coins this cycle ({strikes}/{NO_EARN_STRIKES}) - balance {b}, ad_markers={st.get('_ad_markers')}")
            if strikes >= NO_EARN_STRIKES:
                log("NO CREDIT -> give up this run, wait for the next scheduled one")
                _debug_snapshot(page, "afk_nocredit")
                if max_ad == 0:
                    log("ad script never appeared in the AFK page (proxy/CDN issue?)")
                return last - start, "no-credit", max_ad
    return last - start, "time-up", max_ad


def afk_state(page):
    """读 AFK 页服务端下发的权威额度字段（比抓页面文字可靠）。

    返回 dict：balance / earned_today / earned_hour / day_limit_reached /
    hour_limit_reached / max_earn_per_day。读不到时返回 {}。
    """
    try:
        html = page.evaluate(
            "async () => (await (await fetch('/dashboard/afk')).text())"
        )
    except Exception:
        return {}
    st = {}

    def grab(key, cast=int):
        m = re.search(r'\\?"%s\\?":\s*("?[^,}\\]+)' % key, html)
        if not m:
            return
        v = m.group(1).strip().strip('"')
        if cast is bool:
            st[key] = (v == "true")
        else:
            try:
                st[key] = cast(v)
            except Exception:
                pass

    grab("balance")
    grab("earnedToday")
    grab("earnedHour")
    grab("maxEarnPerDay")
    grab("dayLimitReached", bool)
    grab("hourLimitReached", bool)
    # 广告脚本加载标记：页面上应能看到 Adsterra 相关引用；连续为 0 说明
    # 广告脚本没加载出来（出口/CDN 问题），服务端不会计币。
    st["_ad_markers"] = len(re.findall(r"highperformanceformat|invoke\.js|adstatus|ad-status", html, re.I))
    return st


def afk_counters(page):
    """兼容旧调用：返回 (balance, earned_today)。"""
    st = afk_state(page)
    return st.get("balance"), st.get("earnedToday")


# 只允许购买的品类：运行时间延长。任何其它商品（RAM/CPU/磁盘/槽位/皮肤/置顶）
# 一律拒绝下单，避免脚本把币花在非续期的地方。
ALLOWED_ITEMS = {"runtime_7d", "runtime_24h"}


def buy(page, item_id, ids, cost_cache=None):
    """Buy one runtime extension. Refuses anything outside ALLOWED_ITEMS."""
    if item_id not in ALLOWED_ITEMS:
        log(f"::error::refusing to buy {item_id!r} - only runtime extensions are allowed")
        return False, None
    res = call_action(page, "/dashboard/shop", ids["spendCoinsAction"], [item_id])
    body = (res.get("body") or "").replace('\\"', '"')
    if res.get("status") == 200 and '"ok":false' not in body:
        m = re.search(r'"balance":\s*(\d+)', body)
        return True, (int(m.group(1)) if m else None)
    log(f"purchase {item_id} FAILED: HTTP {res.get('status')} {body[:220]}")
    return False, None


def main():
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    max_minutes = min(8.0, max(0.0, float(os.environ.get("MCWH_AFK_MAX_MINUTES", DEFAULT_MAX_MINUTES))))
    buy_at = int(os.environ.get("MCWH_BUY_7D_AT", "500"))
    if not email or not password:
        log("Missing MCWH_EMAIL / MCWH_PASSWORD.")
        return 2

    log(f"proxy: {os.environ.get('MCWH_PROXY') or '(none - datacenter IP, AFK will NOT credit)'}")
    pw, browser, page = open_session(BASE)
    try:
        sign_in(page, BASE, email, password)
        log("signed in.")
        ids = discover_action_ids(page)

        page.goto(f"{BASE}{COINS_PAGE}", wait_until="domcontentloaded")
        st = read_state(page)
        log(f"state: {json.dumps(st, ensure_ascii=False)}")
        before = st.get("balance")
        if before is None:
            log("could not read balance; aborting.")
            return 1

        # ---------- 1. daily login ----------
        if st.get("claimed") is True:
            log("daily login already claimed today.")
        else:
            res = call_action(page, COINS_PAGE, ids["claimDailyLoginAction"], [])
            body = res.get("body", "")
            if res.get("status") == 200 and '"claimedToday":true' in body.replace('\\"', '"'):
                log(f"daily login claimed (reward was {st.get('daily_reward')}).")
            elif res.get("status") == 200:
                log("daily claim returned 200; rechecking account state, not assuming success.")
            else:
                log(f"daily claim failed: HTTP {res.get('status')}")
            page.reload(wait_until="domcontentloaded")
            st = read_state(page)

        balance = st.get("balance", before)
        log(f"balance after daily: {balance}")

        # ---------- 2. renew at 500 ----------
        bought = None
        import datetime as _dt
        try:
            expiry = _dt.datetime.fromisoformat(st["runtime_expires"].replace("Z", "+00:00"))
            hours_to_expiry = (expiry - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600
        except (KeyError, ValueError, TypeError):
            hours_to_expiry = None
        if balance >= buy_at and st.get("runtime_7d_locked") is False and hours_to_expiry is not None and hours_to_expiry <= 48:
            log(f"balance {balance} >= {buy_at}: buying 7 days extra runtime...")
            ok, bal = buy(page, "runtime_7d", ids)
            if ok:
                bought = bal if bal is not None else "?"
                log(f"bought runtime_7d OK, balance now {bought}")
            page.reload(wait_until="domcontentloaded")
            st = read_state(page)
            balance = st.get("balance", balance)
        elif balance >= buy_at:
            log(f"7d renewal skipped: expiry hours={hours_to_expiry}, locked={st.get('runtime_7d_locked')}; only renew within 48h when unlocked.")
        elif st.get("runtime_7d_locked"):
            log("a 7d runtime boost is already active; not buying again.")
        else:
            need = buy_at - balance
            log(f"balance {balance} < {buy_at}; need {need} more coins before renewing.")
            # 保命兜底：运行时间快耗尽又攒不到 500 时，先买 24h 续命（100 币），
            # 否则服务器到期会被停用，届时连签到和挂机都做不了。
            exp = st.get("runtime_expires")
            if exp and balance >= 100:
                import datetime as _dt
                try:
                    t = _dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    hours_left = (t - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600
                except Exception:
                    hours_left = 999
                if hours_left < 12:
                    log(f"runtime expires in {hours_left:.1f}h and balance<{buy_at}: buying 24h stopgap")
                    ok24, bal24 = buy(page, "runtime_24h", ids)
                    if ok24:
                        log(f"bought runtime_24h OK, balance now {bal24 if bal24 is not None else '?'}")
                    page.reload(wait_until="domcontentloaded")
                    st = read_state(page)
                    balance = st.get("balance", balance)

        exp = st.get("runtime_expires")
        if exp:
            log(f"runtime expires at {exp}")
            if buy_at - balance > 0:
                log("::warning::余额不足 500，未续期；运行时间耗尽后服务器会被停用")

        # ---------- 3. farm AFK ----------
        deadline = time.time() + max_minutes * 60
        earned, why, max_ad = farm(page, deadline)

        final = read_state(page)
        log("================ REPORT ================")
        log(f"balance {before} -> {final.get('balance')}")
        log(f"daily claimed: {final.get('claimed')}")
        log(f"7d runtime bought this run: {bought or 'no'}")
        log(f"AFK earned this run: +{earned} (exit: {why}, ad markers seen: {max_ad})")
        log(f"runtime expires: {final.get('runtime_expires')}")
        if why != "day-cap" and (why in ("nav-failed", "no-balance-readout", "no-credit") or earned <= 0):
            log(f"::error::AFK did not show verified credit growth (ad markers seen in page: {max_ad})")
            return 2
        return 0
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

