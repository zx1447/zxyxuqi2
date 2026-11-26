#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SkyCastle (panel.skycastle.us) AFK 挂机。

方案：纯 HTTP，**直连**（不走住宅代理）。
  GitHub Actions runner 出口可直连站点；本地沙箱出口 TLS 被拒，属环境差异。

实测配置（GET /api/user/billingafk/status）：
  credits_per_minute           0.1     → 10 分钟 = 1 credit
  reward_interval_seconds      60      → work 最小 60 秒
  max_credits_per_day          "50"
  max_sessions_per_day         "3"     ← 每次 start/stop 计 1 个 session
  max_time_per_day_seconds     "30000" → 8.33 小时
  require_claim                True
  require_active_tab           True    ← 纯 HTTP 无法满足，属服务端校验
  require_captcha              False
  daily_usage                  {credits_earned_today, sessions_today, time_seconds_today}

接口（用户浏览器抓包确认，均 200）：
  GET  /api/selftest                                  连通性自检
  GET  /api/user/billingresourcesnewservers/renewals  续期信息
  POST /api/user/billingafk/start                     开始 AFK
  POST /api/user/billingafk/work                      心跳，**服务端限 60 秒最小间隔**
      429 RATE_LIMIT_EXCEEDED: "Please wait at least 60 seconds between work calls"

请求体：work 的 content-length 是 17 字节
  → {"action":"work"} 正好 17 字节，据此推定（start 同理用 {"action":"start"}）

登录：Discord OAuth → PUT /api/user/auth/login {"discord_token":...}
      成功后 remember_token cookie 有效期 30 天，存 state 复用。

环境变量：
  SC_DISCORD_TOKEN   Discord 用户 token（必填）
  SC_BASE            默认 https://panel.skycastle.us
  SC_PROXY           可选 "socks5h://127.0.0.1:10809"
                     ⚠️ 默认**不使用**。只有确认 runner 出口被封时才配。
  SC_WORK_INTERVAL   work 间隔秒，默认 65（服务端限 60s，留余量）
  SC_MAX_MINUTES     单轮上限分钟，默认 14

用法：
  python3 skycastle_afk.py --dry      只打印计划，不发请求
  python3 skycastle_afk.py --probe    只做 selftest + renewals，验证连通与鉴权
  python3 skycastle_afk.py --once     start + 一次 work
  python3 skycastle_afk.py            正常挂机

退出码：0 正常；2 有失败；3 登录/连通失败；4 接口异常
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import re
import os
import sys
import time
import urllib.parse

import requests

BASE = (os.environ.get("SC_BASE") or "https://panel.skycastle.us").rstrip("/")
DISCORD_TOKEN = (os.environ.get("SC_DISCORD_TOKEN") or "").strip()
PROXY = (os.environ.get("SC_PROXY") or "").strip()
WORK_INTERVAL = int(os.environ.get("SC_WORK_INTERVAL") or 65)  # 服务端硬限 60s，留 5s 余量
MAX_MINUTES = int(os.environ.get("SC_MAX_MINUTES") or 14)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skycastle_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

DRY = "--dry" in sys.argv
ONCE = "--once" in sys.argv
PROBE = "--probe" in sys.argv


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def make_session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": BASE + "/dashboard/earn/afk",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
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
    tok = st.get("remember_token")
    if not tok:
        return False
    s.cookies.set("remember_token", tok, domain=urllib.parse.urlparse(BASE).hostname, path="/")
    try:
        r = s.get(BASE + "/api/user/session", timeout=30)
        return r.status_code == 200 and (r.json() or {}).get("success") is not False
    except Exception:
        return False


def login(s, st):
    if not DISCORD_TOKEN:
        log("❌ 缺少 SC_DISCORD_TOKEN")
        return False

    log("🔐 Discord OAuth 登录…")
    try:
        r = s.get(BASE + "/api/user/auth/discord/login", allow_redirects=True, timeout=40)
    except Exception as exc:
        log(f"❌ 无法连接 {BASE}：{type(exc).__name__}")
        log("   若本机出口被封，属环境问题；GitHub Actions runner 通常可直连。")
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


def call(s, method, path, payload=None, retries=4):
    """返回 (status, parsed_or_text)。status=0 表示网络异常。"""
    for i in range(retries):
        try:
            r = s.request(method, BASE + path,
                          json=payload if payload is not None else None,
                          timeout=40, allow_redirects=False)
            body = r.text
            try:
                return r.status_code, json.loads(body)
            except ValueError:
                return r.status_code, body[:400]
        except Exception as exc:
            if i == retries - 1:
                return 0, f"{type(exc).__name__}: {exc}"
            time.sleep(2 + i * 2)
    return 0, "unreachable"


def show(tag, st, body):
    if isinstance(body, (dict, list)):
        txt = json.dumps(body, ensure_ascii=False)
    else:
        txt = str(body)
    log(f"{tag} → HTTP {st} · {txt[:320]}")
    return st


def selftest(s):
    st, body = call(s, "GET", "/api/selftest")
    return show("🩺 selftest", st, body)


def renewals(s):
    st, body = call(s, "GET", "/api/user/billingresourcesnewservers/renewals")
    show("📋 renewals", st, body)
    if PROBE and isinstance(body, dict):
        data = body.get("data") or {}
        for item in data.get("renewals") or []:
            print("RENEWAL_DETAIL", json.dumps(item, ensure_ascii=False), flush=True)
            uid = item.get("uuidShort")
            if uid:
                rc, resources = call(s, "GET", "/api/user/servers/" + str(uid) + "/resources", retries=1)
                print("SERVER_RESOURCES", rc, json.dumps(resources, ensure_ascii=False), flush=True)
        print("RENEWAL_QUEUE", json.dumps(data.get("queue"), ensure_ascii=False), flush=True)
    return st


def server_records(s):
    code, body = call(s, "GET", "/api/user/billingresourcesnewservers/renewals")
    if code != 200 or not isinstance(body, dict) or body.get("success") is not True:
        log(f"❌ 读取续期列表失败: HTTP {code} · {str(body)[:300]}")
        return None
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("renewals"), list):
        log("❌ 续期列表格式异常，停止续期操作")
        return None
    return data["renewals"]


def expiry_utc(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def keep_servers(s):
    """每台最后48小时内且站点开放时续期；无论是否续期都检查并尝试启动。"""
    records = server_records(s)
    if records is None:
        return False
    all_ok = True
    for item in records:
        if not isinstance(item, dict):
            log("❌ 续期记录格式异常")
            all_ok = False
            continue
        rid, uid = item.get("id"), item.get("uuidShort")
        if not str(rid).isdigit() or not re.fullmatch(r"[a-zA-Z0-9-]{1,40}", str(uid or "")):
            log("❌ 续期记录缺少有效 id / uuidShort")
            all_ok = False
            continue
        name = item.get("server_name") or str(rid)
        before = expiry_utc(item.get("renews_at"))
        if before is None:
            log(f"❌ [{name}] 到期时间无法解析，不自动续期")
            all_ok = False
        elif item.get("is_exempt") in (1, True):
            log(f"ℹ️ [{name}] 免续期")
        elif item.get("can_renew") is True and (before - datetime.now(timezone.utc)).total_seconds() <= 48 * 3600:
            log(f"➕ [{name}] 在续期窗口，准备续期 · 原到期 {item.get('renews_at')}")
            # 禁止对可能已扣币的 POST 自动重试；无论 HTTP 是否成功都复查账期。
            code, body = call(s, "POST", f"/api/user/billingresourcesnewservers/renewals/{rid}", retries=1)
            log(f"   续期接口 HTTP {code} · {str(body)[:400]}")
            current = server_records(s)
            updated = next((x for x in (current or []) if isinstance(x, dict) and x.get("id") == rid), None)
            after = expiry_utc(updated.get("renews_at")) if updated else None
            if after and before and after > before:
                log(f"✅ [{name}] 到期时间已推进：{item.get('renews_at')} → {updated.get('renews_at')}")
            else:
                log(f"❌ [{name}] 未能验证到期时间推进，请查看面板；不自动重试扣费")
                all_ok = False
        else:
            log(f"⏳ [{name}] 不在续期窗口或站点未开放 · 到期 {item.get('renews_at')} · can_renew={item.get('can_renew')}")
            if item.get("can_renew") is None or (before and before <= datetime.now(timezone.utc)):
                log(f"❌ [{name}] 续期状态未知或已经到期，需人工检查")
                all_ok = False

        # 续期失败也不跳过启动；不把 HTTP 200 当成运行成功。
        path = f"/api/user/servers/{uid}/resources"
        code, body = call(s, "GET", path, retries=1)
        state = ((body.get("data") or {}).get("state") if code == 200 and isinstance(body, dict)
                 and body.get("success") is True else None)
        log(f"🖥 [{name}] 运行状态={state or '未知'}")
        if state in ("running", "starting"):
            continue
        code, body = call(s, "POST", f"/api/user/servers/{uid}/power/start", retries=1)
        log(f"🚀 [{name}] start → HTTP {code} · {str(body)[:300]}")
        verified = False
        for attempt in range(3):
            time.sleep(3)
            rc, result = call(s, "GET", path, retries=1)
            state = ((result.get("data") or {}).get("state") if rc == 200 and isinstance(result, dict)
                     and result.get("success") is True else None)
            log(f"   复查状态={state or '未知'}")
            if state in ("running", "starting"):
                verified = True
                break
        if not verified:
            log(f"❌ [{name}] 未确认启动成功")
            all_ok = False
    return all_ok


def afk_status(s):
    """读配置 + 日限额。返回 data dict 或 {}。"""
    st, body = call(s, "GET", "/api/user/billingafk/status")
    d = (body or {}).get("data") if isinstance(body, dict) else None
    return d or {}


def afk_stop(s):
    st, body = call(s, "POST", "/api/user/billingafk/stop", {"action": "stop"})
    return show("⏹  stop ", st, body)


def afk_start(s):
    if DRY:
        log('🔍 [dry] POST /api/user/billingafk/start  {"action":"start"}')
        return 0
    st, body = call(s, "POST", "/api/user/billingafk/start", {"action": "start"})
    show("▶️  start", st, body)
    return st if isinstance(body, dict) and body.get("success") is True else -1


def afk_work(s):
    if DRY:
        log('🔍 [dry] POST /api/user/billingafk/work  {"action":"work"}')
        return 0, {}
    st, body = call(s, "POST", "/api/user/billingafk/work", {"action": "work"})
    show("💓 work ", st, body)
    return st, body


def main():
    log(f"🎯 SkyCastle AFK · {'DRY ' if DRY else ''}{'PROBE ' if PROBE else ''}"
        f"{'ONCE ' if ONCE else ''}· proxy={'ON' if PROXY else 'OFF(直连)'} · "
        f"interval={WORK_INTERVAL}s · max={MAX_MINUTES}min")

    s = make_session()
    st = load_state()

    # 0) 连通性自检（不需要登录）
    code = selftest(s)
    if code == 0:
        log("❌ 本站不可达，终止")
        return 3

    # 1) 登录
    if not restore_cookie(s, st):
        if not login(s, st):
            return 3
    else:
        log("♻️  复用 remember_token")

    # 2) 续期信息（同时验证鉴权是否真的生效）
    renewals(s)
    if PROBE:
        log("✅ probe 结束")
        return 0

    # 3) 每台服务器按窗口续期，并检查电源；失败时仍继续 AFK。
    servers_ok = keep_servers(s)

    # 4) 读配置 + 日限额
    info = afk_status(s)
    if info:
        du = info.get("daily_usage") or {}
        dl = info.get("daily_limits") or {}
        log(f"📊 日限额：credits {du.get('credits_earned_today', '?')}/"
            f"{dl.get('max_credits_per_day', '?')} · "
            f"sessions {du.get('sessions_today', '?')}/{dl.get('max_sessions_per_day', '?')} · "
            f"时长 {du.get('time_seconds_today', '?')}s/"
            f"{dl.get('max_time_per_day_seconds', '?')}s · "
            f"余额 {info.get('user_credits')} credits")
        log(f"⚙️  收益 {info.get('credits_per_minute')} credit/分钟 · "
            f"work 间隔 {info.get('reward_interval_seconds')}s · "
            f"require_claim={info.get('require_claim')} · "
            f"require_active_tab={info.get('require_active_tab')}")
        if info.get("is_enabled") is False:
            log("🚫 AFK 已停用（is_enabled=false），终止")
            return 4
        if str(du.get("sessions_today", 0)) >= str(dl.get("max_sessions_per_day", 99)):
            log("🚫 今日 session 已用尽，终止")
            return 4
        st["daily_usage"] = du
        st["user_credits"] = info.get("user_credits")
        save_state(st)
    else:
        log("⚠️ 读不到 status（可能鉴权失败），继续但不做限额保护")

    # 4) 开始 AFK
    if afk_start(s) not in (0, 200) and not DRY:
        log("❌ start 失败，终止")
        return 4

    # 5) 心跳循环
    n = 0
    afk_ok = True
    deadline = time.time() + (0 if ONCE else MAX_MINUTES * 60)
    while True:
        n += 1
        code, body = afk_work(s)
        if not DRY:
            if code != 200 or not isinstance(body, dict) or body.get("success") is not True:
                afk_ok = False
                log("❌ work 未确认成功，本轮标记失败")
            st["last_work_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            st["last_work_http"] = code
            st["work_count"] = st.get("work_count", 0) + 1
            save_state(st)
            if code == 429:
                log("⏱  429 频率限制，下轮拉长间隔")
            if code in (401, 403):
                log("🔐 会话失效，重登")
                st.pop("remember_token", None)
                save_state(st)
                if not login(s, st):
                    return 3
                afk_start(s)
        if ONCE or time.time() + WORK_INTERVAL >= deadline:
            break
        time.sleep(WORK_INTERVAL)

    log(f"✅ 本轮挂机完成，work × {n}")
    if not servers_ok or not afk_ok:
        log("❌ 服务器续期/启动或挂机心跳存在未确认项，请查看日志")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
