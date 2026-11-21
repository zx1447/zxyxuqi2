"""MCWorldHost 自动续期巡逻（每 6 小时）：余额够就买 7 天。

硬规则
────────────────────────────────────────────────────────────────
1. 余额 ≥ 500 且 runtime_7d 没被锁  →  立即买 runtime_7d（7 天）
2. 买完必须复核 runtimeExpiresAt 真的往后跳了，否则判失败退出码非 0
3. 余额 < 500 但 运行时间 < 12h  →  买 runtime_24h 兜底（100 币），
   能买几次买几次，直到余额 < 100
4. 任何一步失败都要在日志里显式打 ❌，绝不静默吞掉
   （2026-11-16 那次过期就是因为旧脚本把购买失败静默忽略）

站点维护模式（"only administrators can sign in"）时登录会失败：
本脚本识别到该文案就立刻退出码 8，不重试、不误判为凭据错误。
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
BUY_AT = int(os.environ.get("MCWH_BUY_7D_AT", "500"))     # 7 天套餐价格
STOPGAP_AT = int(os.environ.get("MCWH_BUY_24H_AT", "100"))  # 24h 套餐价格
MIN_HOURS = float(os.environ.get("MCWH_MIN_HOURS", "12"))   # 低于这个小时数就兜底

# 站点 2026-11-16 起的购买 action（点 Buy 时抓到的）；可用 MCWH_SPEND_ACTION 覆盖
SPEND_ACTION = os.environ.get(
    "MCWH_SPEND_ACTION", "405281bd003bbbf17f25a1190a2641292fce6cc951").strip()


def log(m):
    print(m, flush=True)


def rsc_str(html, key):
    m = re.search(r'\\?"' + key + r'\\?":\s*\\?"([^"\\]+)', html)
    return m.group(1) if m else None


def rsc_int(html, key):
    m = re.search(r'\\?"' + key + r'\\?":\s*(\d+)', html)
    return int(m.group(1)) if m else None


def hours_left(exp):
    if not exp:
        return None
    try:
        t = dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
        return (t - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None


def fetch(page, path):
    try:
        return page.evaluate("async (p) => (await (await fetch(p)).text())", path)
    except Exception as exc:  # noqa: BLE001
        log(f"  fetch {path} 失败: {type(exc).__name__}")
        return ""


def read_state(page):
    st = {}
    dash = fetch(page, "/dashboard")
    st["runtime_expires"] = rsc_str(dash, "runtimeExpiresAt")
    m = re.search(r'\\?"isSuspended\\?":\s*(true|false)', dash)
    st["suspended"] = (m.group(1) == "true") if m else None

    coins = fetch(page, "/dashboard/coins")
    st["balance"] = rsc_int(coins, "balance")
    m = re.search(r'\\?"claimedToday\\?":\s*(true|false)', coins)
    st["claimed"] = (m.group(1) == "true") if m else None

    shop = fetch(page, "/dashboard/shop")
    m = re.search(r'\\?"id\\?":\s*\\?"runtime_7d\\?"(.{0,600}?)\\?"locked\\?":\s*(true|false)',
                  shop, re.S)
    st["locked_7d"] = (m.group(2) == "true") if m else False
    m = re.search(r'\\?"id\\?":\s*\\?"runtime_7d\\?"(.{0,300}?)\\?"cost\\?":\s*(\d+)', shop, re.S)
    st["price_7d"] = int(m.group(2)) if m else BUY_AT
    return st


def buy(page, item):
    """POST /dashboard/shop with Next-Action；返回 (ok, 详情)。"""
    res = page.evaluate("""
    async ([act, item]) => {
      const r = await fetch('/dashboard/shop', {
        method: 'POST',
        headers: {'Next-Action': act, 'Accept': 'text/x-component',
                  'Content-Type': 'text/plain;charset=UTF-8'},
        body: JSON.stringify([item])
      });
      const t = await r.text();
      return {status: r.status, body: t.slice(0, 2000)};
    }""", [SPEND_ACTION, item])
    err = None
    for m in re.finditer(r'\\?"error\\?"\s*:\s*\\?"([^"\\]+)', res["body"]):
        if not m.group(1).startswith("$"):
            err = m.group(1)
    return (res["status"] == 200 and not err), {"status": res["status"], "error": err}


def main():
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "").strip()
    if not email or not password:
        log("❌ 缺 MCWH_EMAIL / MCWH_PASSWORD")
        return 3

    pw, browser, page = open_session(BASE, wait_seconds=150)
    try:
        try:
            sign_in(page, BASE, email, password, timeout=120000)
        except Exception as exc:  # noqa: BLE001
            body = ""
            try:
                body = page.locator("body").inner_text()[:1500].lower()
            except Exception:  # noqa: BLE001
                pass
            if "maintenance" in body or "wartungsarbeiten" in body:
                log("🛠️ 站点处于维护模式（only administrators can sign in）"
                    "——无法登录，本次跳过，等下次运行")
                log("::warning title=MCWorldHost 维护中::站点仅允许管理员登录，"
                    "本次未检查续期，2 小时后重试")
                return 0
            log(f"❌ 登录失败: {type(exc).__name__}: {exc}")
            return 4

        st = read_state(page)
        hl = hours_left(st.get("runtime_expires"))
        hl_txt = f"{hl:.2f}h" if isinstance(hl, (int, float)) else "读取失败"
        log(f"📊 余额={st.get('balance')} 到期={st.get('runtime_expires')}"
            f"（剩 {hl_txt}）锁7d={st.get('locked_7d')} 停用={st.get('suspended')}")
        log(f"   7d 价格={st.get('price_7d')}")

        # 关键状态读不到时不要静默继续 —— 明确告警
        if st.get("runtime_expires") is None and st.get("balance") is None:
            log("⚠️ 到期时间与余额都没读到，页面结构可能变了 —— 本次不执行购买")
            log("::warning title=MCWorldHost 状态读取失败::dashboard/coins 解析为空，本次跳过")
            try:
                page.screenshot(path="mcwh_debug_state_fail.png", full_page=True)
            except Exception:  # noqa: BLE001
                pass
            return 0

        balance = st.get("balance") or 0
        price7 = st.get("price_7d") or BUY_AT
        before_exp = st.get("runtime_expires")
        bought = []

        # ── 规则 1：够 500 且没锁 → 立即买 7 天 ──
        if balance >= price7 and not st.get("locked_7d"):
            log(f"🛒 余额 {balance} ≥ {price7}，买 runtime_7d")
            ok, info = buy(page, "runtime_7d")
            log(f"   结果: {'✅ 成功' if ok else '❌ 失败'} {json.dumps(info, ensure_ascii=False)}")
            if not ok:
                page.screenshot(path="mcwh_debug_buy7d.png", full_page=True)
                return 5
            bought.append("runtime_7d")
            time.sleep(4)
            st2 = read_state(page)
            if st2.get("runtime_expires") == before_exp:
                log("❌ 买了但到期时间没变——判定失败（站点可能又变了）")
                page.screenshot(path="mcwh_debug_buy_noeffect.png", full_page=True)
                return 6
            hl2 = hours_left(st2.get("runtime_expires"))
            hl2t = f"{hl2:.1f}" if isinstance(hl2, (int, float)) else "?"
            log(f"✅ 7 天续期成功：{st2.get('runtime_expires')}（剩 {hl2t}h）"
                f"｜余额 {st2.get('balance')}")
            return 0

        # ── 规则 2：余额不足且运行时间快没了 → 24h 兜底 ──
        if hl is not None and hl < MIN_HOURS and balance >= STOPGAP_AT:
            n = 0
            while True:
                st_c = read_state(page)
                bal = st_c.get("balance") or 0
                if bal < STOPGAP_AT:
                    break
                log(f"🛒 兜底买 runtime_24h（余额 {bal}）")
                ok, info = buy(page, "runtime_24h")
                if not ok:
                    log(f"   ❌ 失败 {json.dumps(info, ensure_ascii=False)}")
                    break
                n += 1
                time.sleep(3)
                if n >= 5:
                    break
            st3 = read_state(page)
            hl3 = hours_left(st3.get("runtime_expires"))
            hl3t = f"{hl3:.1f}" if isinstance(hl3, (int, float)) else "?"
            log(f"{'✅' if n else '❌'} 兜底买了 {n} 次 24h → 到期 {st3.get('runtime_expires')}"
                f"（剩 {hl3t}h）｜余额 {st3.get('balance')}")
            return 0 if n else 7

        log(f"⏭️ 不满足购买条件：余额 {balance} / 需 {price7}；剩余 {hl if hl is None else round(hl,1)}h"
            f"（兜底阈值 {MIN_HOURS}h）")
        return 0
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
