#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SkyCastle (panel.skycastle.us) AFK 挂机。

⚠️ 状态：接口来自用户浏览器实测抓包，沙箱侧站点当时不可达，逻辑未经真实跑通。
   首次运行请用 --dry 先看请求，再用 --once 单轮验证。

已知接口（用户抓包确认，均为 POST，返回 200）：
  POST /api/user/billingafk/start    开始 AFK 计费
  POST /api/user/billingafk/work     工作/心跳（需按站点要求周期调用）

登录：Discord OAuth（无需密码）
  GET  /api/user/auth/discord/login                → 302 discord authorize
  POST https://discord.com/api/v9/oauth2/authorize （带 Discord token）
  GET  <callback>                                  → /auth/login?discord_token=<handoff>
  PUT  /api/user/auth/login {"discord_token":...}  → 200 + remember_token cookie(30天)

环境变量：
  SC_DISCORD_TOKEN   Discord 用户 token（必填）
  SC_BASE            默认 https://panel.skycastle.us
  SC_PROXY           socks5h://127.0.0.1:10809（可选；沙箱/机房 IP 直连 TLS 会被拒）
  SC_WORK_INTERVAL   work 调用间隔秒数，默认 60
  SC_MAX_MINUTES     单轮上限分钟，默认 14

退出码：0 正常；2 有失败；3 登录失败；4 接口不符合预期
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse

import requests

BASE = (os.environ.get("SC_BASE") or "https://panel.skycastle.us").rstrip("/")
DISCORD_TOKEN = (os.environ.get("SC_DISCORD_TOKEN") or "").strip()
PROXY = (os.environ.get("SC_PROXY") or "").strip()
WORK_INTERVAL = int(os.environ.get("SC_WORK_INTERVAL") or 60)
MAX_MINUTES = int(os.environ.get("SC_MAX_MINUTES") or 14)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skycastle_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

DRY = "--dry" in sys.argv
ONCE = "--once" in sys.argv


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def make_session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": BASE + "/dashboard/earn/afk",
    })
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
    return s


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def restore_cookie(s, st):
    """复用 remember_token（有效期 30 天），免每次走 OAuth。"""
    tok = st.get("remember_token")
    if not tok:
        return False
    s.cookies.set("remember_token", tok, domain=urllib.parse.urlparse(BASE).hostname, path="/")
    try:
        r = s.get(BASE + "/api/user/session", timeout=30)
        return r.status_code == 200 and r.json().get("success") is not False
    except Exception:
        return False


def login(s, st):
    """Discord OAuth → PUT /api/user/auth/login。成功返回 True。"""
    if not DISCORD_TOKEN:
        log("❌ 缺少 SC_DISCORD_TOKEN")
        return False

    log("🔐 Discord OAuth 登录…")
    try:
        r = s.get(BASE + "/api/user/auth/discord/login", allow_redirects=True, timeout=40)
    except Exception as exc:
        log(f"❌ 无法连接 {BASE}：{type(exc).__name__}: {exc}")
        log("   站点侧 TLS 握手被拒（直连与住宅代理同样失败），请确认站点可用或更换 SC_PROXY")
        return False
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.url).query)
    if "client_id" not in q:
        log(f"❌ 未拿到 authorize URL：HTTP {r.status_code}")
        return False

    auth_url = "https://discord.com/api/v9/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": q["client_id"][0],
        "response_type": "code",
        "scope": q["scope"][0],
        "state": q["state"][0],
        "redirect_uri": q["redirect_uri"][0],
        "prompt": "consent",
    })
    # Discord 侧不经过 SC_PROXY（本就在境外，且走代理更慢）
    dr = requests.post(
        auth_url,
        json={"permissions": "0", "authorize": True, "integration_type": 0,
              "guild_id": None, "channel_id": None, "captcha_key": None},
        headers={"Authorization": DISCORD_TOKEN, "User-Agent": UA},
        timeout=30,
    )
    if dr.status_code != 200:
        log(f"❌ Discord 授权失败：HTTP {dr.status_code} {dr.text[:150]}")
        return False
    loc = (dr.json() or {}).get("location")
    if not loc:
        log(f"❌ Discord 未返回 location：{dr.text[:200]}")
        return False

    r2 = s.get(loc, timeout=40, allow_redirects=True)
    handoff = urllib.parse.parse_qs(urllib.parse.urlparse(r2.url).query).get("discord_token", [""])[0]
    if not handoff:
        log(f"❌ 未拿到 discord_token（落地：{r2.url[:120]}）")
        return False

    r3 = s.put(BASE + "/api/user/auth/login", json={"discord_token": handoff}, timeout=40)
    if r3.status_code != 200:
        log(f"❌ 登录失败：HTTP {r3.status_code} {r3.text[:200]}")
        return False

    user = ((r3.json() or {}).get("data") or {}).get("user") or {}
    log(f"✅ 登录成功 · {user.get('username')} · id {user.get('id')}")
    tok = s.cookies.get("remember_token")
    if tok:
        st["remember_token"] = tok
        save_state(st)
    return True


def post(s, path, payload=None):
    """带重试的 POST，返回 (status, json_or_text)。"""
    for i in range(4):
        try:
            r = s.post(BASE + path, json=payload if payload is not None else {},
                       timeout=40, allow_redirects=False)
            body = r.text
            try:
                return r.status_code, json.loads(body)
            except ValueError:
                return r.status_code, body[:300]
        except Exception as exc:
            if i == 3:
                return 0, f"{type(exc).__name__}: {exc}"
            time.sleep(2 + i * 2)


def afk_start(s):
    if DRY:
        log("🔍 [dry] POST /api/user/billingafk/start {}")
        return 0, {}
    st, body = post(s, "/api/user/billingafk/start")
    log(f"▶️  start → HTTP {st} · {json.dumps(body, ensure_ascii=False)[:200] if isinstance(body, (dict, list)) else body}")
    return st, body


def afk_work(s):
    if DRY:
        log("🔍 [dry] POST /api/user/billingafk/work {}")
        return 0, {}
    st, body = post(s, "/api/user/billingafk/work")
    ok = st == 200
    log(f"{'💓' if ok else '⚠️'} work  → HTTP {st} · "
        f"{json.dumps(body, ensure_ascii=False)[:200] if isinstance(body, (dict, list)) else body}")
    return st, body


def main():
    log(f"🎯 SkyCastle AFK 挂机 · proxy={'on' if PROXY else 'off'} · "
        f"interval={WORK_INTERVAL}s · max={MAX_MINUTES}min")
    s = make_session()
    st = load_state()

    if not restore_cookie(s, st):
        if not login(s, st):
            log("❌ 登录不可用，退出")
            return 3
    else:
        log("♻️  复用 remember_token")

    # 1) 开始 AFK
    st_code, start_body = afk_start(s)
    if not DRY and st_code != 200:
        log(f"❌ start 失败，终止。HTTP {st_code}")
        return 4

    # 2) 周期性 work
    n = 0
    deadline = time.time() + (0 if ONCE else (MAX_MINUTES * 60))
    while True:
        n += 1
        st_code, body = afk_work(s)
        if not DRY:
            st["last_work_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            st["last_work_http"] = st_code
            st["work_count"] = st.get("work_count", 0) + 1
            save_state(st)
            if st_code in (401, 403):
                log("🔐 会话失效，重新登录")
                st.pop("remember_token", None)
                save_state(st)
                if not login(s, st):
                    return 3
                afk_start(s)
            elif st_code == 0:
                log("⚠️ 网络异常，下轮重试")
        if ONCE or time.time() + WORK_INTERVAL >= deadline:
            break
        time.sleep(WORK_INTERVAL)

    log(f"✅ 本轮完成，work 调用 {n} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
