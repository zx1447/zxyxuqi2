#!/usr/bin/env python3
"""Aurix Host（dash.aurix.host）AFK 赚币 + 精准续期守护。

两步：
  1) `--tokens`：真浏览器（Xvfb）里渲染 Turnstile 并点一下复选框，产出 gate / afk 两个 token
  2) 默认：纯 HTTP —— Discord OAuth 登录 → 过入场 gate → AFK 挂机 → 到期前用币续期

已实测的接口（2026-11-25）：
  GET  /auth/login                       302 → discord oauth2/authorize
  GET  /auth/callback?code=..&state=..   302 → /，下发 __Host-aurix_sid
  GET  /api/me                           {user:{coins}, csrf, gateUntil, usage}
  POST /api/adcheck                      {"blocked":false}
  POST /api/gate/start                   → {nonce, seconds:30}
  POST /api/gate/ping                    {"nonce","visible":true} → {watched,counted}
  POST /api/gate/complete                {"nonce","turnstile":"<gate token>"} → {gateUntil}
  POST /api/afk/start                    {"turnstile":"<afk token>"} → {id, maxTicks, coinsPerTick, nextTickAt, …}
  POST /api/afk/beat                     {"id"} → 同上，含 ticksPaid / earned / balance / ended
  GET  /api/servers                      → [{uuid, renewal:{expiresAt, secondsLeft}, renewalCost, renewalDays}]
  POST /api/servers/{uuid}/renew         {} → {balance, expiresAt}     ← 需要 x-csrf-token 头

已知规则（实测）：
  · 所有写操作必须带 `x-csrf-token`（= /api/me 的 csrf），否则 403
  · 入场 gate 是「按会话」的：换一次登录就要重过（30 秒计时 + turnstile），否则 428 Watch the entry ad first
  · AFK：20 分钟一轮（tickMinutes=5, coinsPerTick=2, maxTicks=4）→ 每次最多 +8 coins
  · 续期：renewalCost 20 coins / 14 天；币不够或未到窗口就不发请求

循环策略（按用户要求）：
  · AFK 挂到本轮工作流结束（默认 5 小时 = 300 分钟）
  · 每个会话 20 分钟，之后休息 44 分钟，再开下一个（20 + 44 = 64 分钟一个循环）
  · 每次 afk/start 都要一个新的 Turnstile token → 循环里按需产（job 里已装 Chromium）
  · 续期只在服务器剩余 ≤48 小时（=最后 2 天）且余额够时才发请求

环境变量：
  AURIX_DISCORD_TOKEN   必填
  AURIX_RUN_MINUTES     本轮循环总时长，默认 300
  AURIX_REST_MINUTES    每个会话之间休息，默认 44
  AURIX_RENEW_AT_HOURS  剩余多少小时以内才续期，默认 48（=最后 2 天）
  AURIX_MODE            probe = 只读
"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://dash.aurix.host"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TOKEN = os.environ.get("AURIX_DISCORD_TOKEN", "").strip()
TOKENS_FILE = os.environ.get("AURIX_TOKENS_FILE", "/tmp/aurix_tokens.json")
RENEW_AT_H = float(os.environ.get("AURIX_RENEW_AT_HOURS", "48") or 48)
RUN_MIN = float(os.environ.get("AURIX_RUN_MINUTES", "300") or 300)
REST_MIN = float(os.environ.get("AURIX_REST_MINUTES", "44") or 44)
MODE = os.environ.get("AURIX_MODE", "act").strip().lower()
SITEKEY = "0x4AAAAAAFzE2Y-C6wXTHOxu"


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


# ------------------------------------------------------------------ Turnstile
def make_tokens(path, actions=("gate", "afk")):
    """真浏览器里渲染 Turnstile，点一下复选框，取指定 action 的 token。"""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False,
                              args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        pg = ctx.new_page()
        pg.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(2000)
        for n, action in enumerate(actions):
            left, top = 20, 20 + n * 90
            pg.evaluate("""async ([sk, act, left, top]) => {
                await new Promise((res, rej) => {
                    if (window.turnstile) return res();
                    const s = document.createElement('script');
                    s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
                    s.onload = res; s.onerror = () => rej(new Error('turnstile script blocked'));
                    document.head.append(s);
                });
                const box = document.createElement('div');
                box.id = 'tsbox_' + act;
                box.style.cssText = `position:fixed;left:${left}px;top:${top}px;z-index:99999;`;
                document.body.append(box);
                window['__tok_' + act] = '';
                window.turnstile.render(box, {sitekey: sk, action: act, theme: 'dark',
                    callback: (t) => { window['__tok_' + act] = t; },
                    'error-callback': () => { window['__tok_' + act] = 'ERR'; }});
            }""", [SITEKEY, action, left, top])
            tok = ""
            for _ in range(6):
                pg.wait_for_timeout(6000)
                tok = pg.evaluate(f"() => window['__tok_{action}'] || ''")
                if tok and tok != "ERR":
                    break
                pg.mouse.click(left + 30, top + 33)   # 点复选框
            if not tok or tok == "ERR":
                raise RuntimeError(f"Turnstile({action}) 没拿到 token")
            out[action] = tok
            log(f"Turnstile {action} ✅ len={len(tok)}")
        b.close()
    if path:
        json.dump(out, open(path, "w"))
        log(f"token 已写入 {path}")
    return out


# ------------------------------------------------------------------ HTTP
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Aurix:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj), NR)
        self.csrf = ""

    def raw(self, url, method="GET", data=None, headers=None):
        h = {"User-Agent": UA, "Accept": "application/json"}
        if headers:
            h.update(headers)
        body = json.dumps(data).encode() if data is not None else None
        try:
            r = self.op.open(urllib.request.Request(url, data=body, method=method, headers=h), timeout=40)
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, {}, f"{type(e).__name__}: {e}"

    def api(self, path, method="GET", data=None, need_csrf=True):
        h = {"Origin": BASE, "Referer": BASE + "/#/earn", "Content-Type": "application/json"}
        if need_csrf and self.csrf:
            h["x-csrf-token"] = self.csrf
        st, _, body = self.raw(BASE + path, method, data, h)
        try:
            return st, (json.loads(body) if body else {})
        except Exception:  # noqa: BLE001
            return st, {"_raw": body[:300]}

    def login(self):
        if not TOKEN:
            raise RuntimeError("缺 AURIX_DISCORD_TOKEN")
        st, hd, _ = self.raw(BASE + "/auth/login")
        loc = hd.get("location", "")
        cid = re.search(r"client_id=(\d+)", loc)
        state = re.search(r"state=([^&]+)", loc)
        red = re.search(r"redirect_uri=([^&]+)", loc)
        if not (cid and state and red):
            raise RuntimeError(f"OAuth 起始失败 st={st} loc={loc[:120]}")
        q = urllib.parse.urlencode({"client_id": cid.group(1),
                                    "redirect_uri": urllib.parse.unquote(red.group(1)),
                                    "response_type": "code", "scope": "identify email"})
        st, _, body = self.raw(f"https://discord.com/api/v9/oauth2/authorize?{q}", "POST",
                               {"permissions": "0", "authorize": True, "integration_type": 0,
                                "guild_id": None, "channel_id": None, "captcha_key": None},
                               {"Authorization": TOKEN, "Content-Type": "application/json",
                                "Origin": "https://discord.com"})
        if st != 200:
            raise RuntimeError(f"Discord authorize {st}: {body[:200]}")
        cb = json.loads(body)["location"]
        self.raw(cb + ("&" if "?" in cb else "?") + "state=" + state.group(1))
        if not any(c.name == "__Host-aurix_sid" for c in self.cj):
            raise RuntimeError("登录没拿到 __Host-aurix_sid")
        st, me = self.api("/api/me", need_csrf=False)
        if st != 200:
            raise RuntimeError(f"/api/me {st}: {str(me)[:200]}")
        self.csrf = me.get("csrf") or ""
        log(f"✅ 登录成功 · {me['user']['username']} · {me['user']['coins']} coins · gate={'已过' if me.get('gateUntil') else '未过'}")
        return me


def pass_gate(ax, tokens):
    st, d = ax.api("/api/servers", need_csrf=False)
    if st != 428:
        return True
    log("→ 入场 gate：30 秒计时 + turnstile")
    st_a, _ = ax.api("/api/adcheck", "POST", {"blocked": False})
    log(f"  adcheck → {st_a}")
    st, gs = ax.api("/api/gate/start", "POST", {})
    if st != 200 or "nonce" not in gs:
        log(f"  gate/start 失败 {st} {str(gs)[:160]}")
        return False
    nonce, secs = gs["nonce"], gs.get("seconds", 30)
    t0 = time.time()
    while time.time() - t0 < secs + 2:
        time.sleep(3)
        ax.api("/api/gate/ping", "POST", {"nonce": nonce, "visible": True})
    st, d = ax.api("/api/gate/complete", "POST", {"nonce": nonce, "turnstile": tokens.get("gate", "")})
    if st != 200:
        log(f"  gate/complete 失败 {st} {str(d)[:160]}")
        return False
    log(f"  ✅ gate 通过，有效至 {time.strftime('%H:%M', time.localtime(d.get('gateUntil', 0) / 1000))}")
    return True


def afk_token():
    """每次 afk/start 都要一个新的 Turnstile token（约 5 分钟有效、一次性）。"""
    try:
        return make_tokens(None, ("afk",))["afk"]
    except Exception as e:  # noqa: BLE001
        log(f"  Turnstile(afk) 失败：{e}")
        return ""


def run_afk(ax, minutes):
    """跑一轮 AFK（服务端一轮 20 分钟，最多 +8 coins）。返回 (earned, balance, ended_reason)。"""
    ax.api("/api/adcheck", "POST", {"blocked": False})
    tok = afk_token()
    if not tok:
        return 0, None, "no-turnstile"
    st, ses = ax.api("/api/afk/start", "POST", {"turnstile": tok})
    if st != 200:
        log(f"AFK start 失败 {st} {str(ses)[:200]}")
        return 0, None, f"start-{st}"
    sid = ses["id"]
    log(f"AFK 开始 · maxTicks={ses.get('maxTicks')} · 每 tick {ses.get('coinsPerTick')} coins"
        f" · 睡 {minutes:g} 分钟")
    t0 = time.time()
    last = ses
    got451 = 0
    while time.time() - t0 < minutes * 60:
        time.sleep(30)
        st, d = ax.api("/api/afk/beat", "POST", {"id": sid})
        if st == 451 and (d.get("adblock") or "ad blocker" in str(d.get("error", ""))):
            got451 += 1
            ax.api("/api/adcheck", "POST", {"blocked": False})
            log(f"  beat 451（adblock 复核，第 {got451} 次）→ 重报 adcheck 后重试")
            time.sleep(3)
            st, d = ax.api("/api/afk/beat", "POST", {"id": sid})
        if st != 200:
            log(f"  beat {st} {str(d)[:160]}")
            return last.get("earned", 0), last.get("balance"), f"beat-{st}"
        last = d
        log(f"  beat t+{time.time()-t0:.0f}s · ticks {d.get('ticksPaid')}/{d.get('maxTicks')}"
            f" · +{d.get('earned')} · 余额 {d.get('balance')}")
        if d.get("ended"):
            log(f"  会话结束：{d.get('endReason')}")
            break
    return last.get("earned", 0), last.get("balance"), last.get("endReason")


def handle_renewals(ax, balance):
    st, d = ax.api("/api/servers", need_csrf=False)
    if st != 200:
        log(f"读服务器失败 {st} {str(d)[:160]}")
        return balance
    for s in d.get("servers", []):
        rn = s.get("renewal") or {}
        left = rn.get("secondsLeft") or 0
        cost = s.get("renewalCost") or 0
        uid = s["uuid"]
        log(f"[{s['name']}] 剩余 {left/3600:.1f}h · 续期 {cost} coins / {s.get('renewalDays')}d · 余额 {balance}")
        if rn.get("exempt") or rn.get("staffSuspended"):
            log("  豁免/被封，跳过")
            continue
        if left > RENEW_AT_H * 3600:
            log(f"  还没到窗口（>{RENEW_AT_H:g}h），不续")
            continue
        if balance < cost:
            log(f"  币不够（{balance} < {cost}），本轮不续")
            continue
        if MODE == "probe":
            log("  probe 模式，不发续期")
            continue
        before = rn.get("expiresAt")
        st, res = ax.api(f"/api/servers/{uid}/renew", "POST", {})
        if st != 200:
            log(f"  续期失败 {st} {str(res)[:200]}")
            continue
        balance = res.get("balance", balance)
        st2, d2 = ax.api("/api/servers", need_csrf=False)
        after = None
        for s2 in d2.get("servers", []):
            if s2["uuid"] == uid:
                after = (s2.get("renewal") or {}).get("expiresAt")
        if after and after != before:
            log(f"  ✅ 续期成功 · {before} → {after} · 余额 {balance}")
        else:
            log(f"  ⚠️ HTTP 200 但到期时间没变（{before}），不算成功")
    return balance


def main():
    if "--tokens" in sys.argv:
        make_tokens(TOKENS_FILE)
        return 0
    try:
        tokens = json.load(open(TOKENS_FILE))
    except Exception:  # noqa: BLE001
        tokens = {}
    ax = Aurix()
    try:
        me = ax.login()
    except Exception as e:  # noqa: BLE001
        log(f"❌ 登录失败：{e}")
        return 3
    balance = me["user"].get("coins", 0)
    try:
        if not pass_gate(ax, tokens):
            log("❌ gate 没过，本轮结束")
            return 2
    except Exception as e:  # noqa: BLE001
        log(f"❌ gate 异常：{e}")
        return 2

    if MODE == "probe":
        handle_renewals(ax, balance)
        log("probe 完成")
        return 0

    end_at = time.time() + RUN_MIN * 60
    session_no = 0
    total = 0
    while True:
        session_no += 1
        left_min = (end_at - time.time()) / 60
        if left_min < 21:
            log(f"剩余 {left_min:.0f} 分钟，不够一个完整会话（20 分钟），收工")
            break
        log(f"===== 第 {session_no} 个 AFK 会话（本轮还剩 {left_min:.0f} 分钟）=====")
        try:
            earned, b2, reason = run_afk(ax, min(21, left_min))
            total += earned or 0
            if b2 is not None:
                balance = b2
            log(f"会话 {session_no} 结束 · +{earned} · 余额 {balance} · 原因={reason} · 本轮累计 +{total}")
        except Exception as e:  # noqa: BLE001
            log(f"会话 {session_no} 异常：{type(e).__name__}: {e}")

        # 每个会话后都检查一次续期（只在最后 2 天才会真的发）
        try:
            balance = handle_renewals(ax, balance)
        except Exception as e:  # noqa: BLE001
            log(f"续期检查异常：{e}")

        if time.time() + REST_MIN * 60 > end_at - 21 * 60:
            log("休息后剩余时间不够再挂一轮，收工")
            break
        log(f"休息 {REST_MIN:g} 分钟…")
        time.sleep(REST_MIN * 60)

    log(f"本轮工作流结束 · {session_no} 个会话 · 累计 +{total} · 余额 {balance}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
