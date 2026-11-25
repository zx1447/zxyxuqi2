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

环境变量：
  AURIX_DISCORD_TOKEN   必填
  AURIX_TOKENS_FILE     --tokens 产出的 json 路径，默认 /tmp/aurix_tokens.json
  AURIX_RENEW_AT_HOURS  剩余多少小时以内才续期，默认 48
  AURIX_AFK_MINUTES     AFK 最多挂多久，默认 21（跑满一轮）
  AURIX_MODE            probe = 只读（登录 + 读状态，不写）
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
AFK_MIN = float(os.environ.get("AURIX_AFK_MINUTES", "21") or 21)
MODE = os.environ.get("AURIX_MODE", "act").strip().lower()
SITEKEY = "0x4AAAAAAFzE2Y-C6wXTHOxu"


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


# ------------------------------------------------------------------ Turnstile
def make_tokens(path):
    """真浏览器里渲染 Turnstile，点一下复选框，取 gate / afk 两个 token。"""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False,
                              args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        pg = ctx.new_page()
        pg.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(2000)
        for n, action in enumerate(("gate", "afk")):
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
    json.dump(out, open(path, "w"))
    log(f"token 已写入 {path}")


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


def run_afk(ax, tokens):
    ax.api("/api/adcheck", "POST", {"blocked": False})
    st, ses = ax.api("/api/afk/start", "POST", {"turnstile": tokens.get("afk", "")})
    if st != 200:
        log(f"AFK start 失败 {st} {str(ses)[:200]}")
        return 0, None
    sid = ses["id"]
    log(f"AFK 开始 · maxTicks={ses.get('maxTicks')} · 每 tick {ses.get('coinsPerTick')} coins")
    t0 = time.time()
    last = ses
    while time.time() - t0 < AFK_MIN * 60:
        time.sleep(30)
        st, d = ax.api("/api/afk/beat", "POST", {"id": sid})
        if st == 451 and (d.get("adblock") or "ad blocker" in str(d.get("error", ""))):
            # 站点会周期性复核广告拦截状态，重打一次 adcheck 再继续
            st_a, _ = ax.api("/api/adcheck", "POST", {"blocked": False})
            log(f"  beat 451（adblock 复核）→ 重报 adcheck {st_a} 后重试")
            time.sleep(3)
            st, d = ax.api("/api/afk/beat", "POST", {"id": sid})
        if st != 200:
            log(f"  beat {st} {str(d)[:160]}")
            break
        last = d
        if d.get("ticksPaid") != last.get("ticksPaid") or d.get("ended"):
            pass
        log(f"  beat t+{time.time()-t0:.0f}s · ticks {d.get('ticksPaid')}/{d.get('maxTicks')}"
            f" · +{d.get('earned')} · 余额 {d.get('balance')}")
        if d.get("ended"):
            log(f"  会话结束：{d.get('endReason')}")
            break
    return last.get("earned", 0), last.get("balance")


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
        if MODE != "probe":
            earned, b2 = run_afk(ax, tokens)
            if b2 is not None:
                balance = b2
            log(f"AFK 结束 · 本轮 +{earned} · 余额 {balance}")
        balance = handle_renewals(ax, balance)
    except Exception as e:  # noqa: BLE001
        log(f"❌ {type(e).__name__}: {e}")
        return 2
    log("本轮完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
