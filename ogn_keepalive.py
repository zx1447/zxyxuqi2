#!/usr/bin/env python3
"""OpenGameNode（Calagopus 面板）守护 —— 纯 HTTP，无浏览器、无 Turnstile。

登录走 Discord OAuth（不需要验证码）：
  GET  /api/auth/oauth/redirect/<provider>        → 302 discord.com/oauth2/authorize
  POST discord.com/api/v9/oauth2/authorize?...    → {"location": ".../api/auth/oauth/<uuid>?code=..."}
  GET  <location>&state=<state>                   → 307 到面板首页，下发 calagopus_session
  ↑ Discord 不回传 state，必须自己补，否则 400 missing field `state`

免费套餐（FreeTier）实测参数：
  session_limit_seconds = 14400 (4h)      extension_seconds = 14400 (4h)
  max_extensions = 2                      idle_action = stop / idle_seconds = 300
  会话到点后不会自动重启（clock.action = "session_limit"，state 变 offline）

动作规则：
  offline / session_limit → power start
  running 且剩余 ≤ 10 分钟 → ogn-session/extend（只在最后 10 分钟内被接受）
  续期额度用尽 → stop → start 换新会话

环境变量：
  OGN_DISCORD_TOKEN   必填，Discord token（登录用）
  OGN_SERVER_UUIDS    可选，逗号分隔；留空 = 账户下全部服务器
  OGN_DWELL_MINUTES   可选，默认 0（单次检查后退出）；>0 则驻留巡检
  OGN_CHECK_SECONDS   驻留间隔，默认 300
"""
import json
import http.cookiejar  # noqa: E402  (确保不被同名函数遮蔽)
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "https://panel.opengamenode.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROVIDER = os.environ.get("OGN_OAUTH_PROVIDER", "609712d4-1d05-45ad-8ad9-eea05c78941a")
TOKEN = os.environ.get("OGN_DISCORD_TOKEN", "").strip()
WANT = [u.strip() for u in os.environ.get("OGN_SERVER_UUIDS", "").split(",") if u.strip()]
DWELL_MIN = float(os.environ.get("OGN_DWELL_MINUTES", "0") or 0)
CHECK_SEC = int(os.environ.get("OGN_CHECK_SECONDS", "300") or 300)
EXTEND_WINDOW_SEC = 600
STATE_OK = {"running", "starting"}


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def do(op, url, method="GET", data=None, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        r = op.open(req, timeout=30)
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, {}, f"{type(e).__name__}: {e}"


def login():
    if not TOKEN:
        raise RuntimeError("缺 OGN_DISCORD_TOKEN")
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NR)
    st, hd, _ = do(op, f"{BASE}/api/auth/oauth/redirect/{PROVIDER}")
    loc = hd.get("location", "")
    cid = re.search(r"client_id=(\d+)", loc)
    state = re.search(r"state=([^&]+)", loc)
    red = re.search(r"redirect_uri=([^&]+)", loc)
    if not (cid and state and red):
        raise RuntimeError(f"OAuth 起始失败 st={st} loc={loc[:120]}")
    q = urllib.parse.urlencode({
        "client_id": cid.group(1), "scope": "email identify",
        "response_type": "code", "redirect_uri": urllib.parse.unquote(red.group(1)),
    })
    st, _, body = do(op, f"https://discord.com/api/v9/oauth2/authorize?{q}", "POST",
                       {"permissions": "0", "authorize": True, "integration_type": 0,
                        "guild_id": None, "channel_id": None, "captcha_key": None},
                       {"Authorization": TOKEN, "Content-Type": "application/json",
                        "Origin": "https://discord.com"})
    if st != 200:
        raise RuntimeError(f"Discord authorize {st}: {body[:200]}")
    cb = json.loads(body)["location"]
    cb += ("&" if "?" in cb else "?") + "state=" + state.group(1)
    do(op, cb)
    if not any(c.name == "calagopus_session" for c in cj):
        # 307 到首页后再取一次，确保 cookie 落地
        do(op, BASE + "/")
    if not any(c.name == "calagopus_session" for c in cj):
        raise RuntimeError("登录没拿到 calagopus_session（Discord 号是否已绑定该面板？）")
    log("✅ 登录成功（Discord OAuth，无浏览器）")
    return op


def api(op, path, method="GET", data=None):
    st, _, body = do(op, BASE + path, method, data,
                       {"Origin": BASE, "Content-Type": "application/json",
                        "Referer": BASE + "/"})
    try:
        return st, json.loads(body) if body else {}
    except Exception:  # noqa: BLE001
        return st, {"_raw": body[:400]}


def servers(op):
    st, d = api(op, "/api/client/servers")
    if st != 200:
        raise RuntimeError(f"servers {st}: {str(d)[:200]}")
    return d.get("servers", {}).get("data", [])


def session(op, uuid):
    st, d = api(op, f"/api/client/servers/{uuid}/ogn-session")
    return d if st == 200 else None


def state_of(op, uuid):
    st, d = api(op, f"/api/client/servers/{uuid}/resources")
    return (d.get("resources", {}) or {}).get("state") if st == 200 else None


def power(op, uuid, action):
    st, d = api(op, f"/api/client/servers/{uuid}/power", "POST", {"action": action})
    log(f"    power {action} → {st} {str(d)[:120]}")
    return st in (200, 202, 204)


def ensure(op, srv):
    uuid, name = srv["uuid"], srv.get("name")
    if WANT and uuid not in WANT:
        return "skip"
    snap = session(op, uuid) or {}
    plan = snap.get("plan") or {}
    clock = snap.get("clock") or {}
    limit = plan.get("session_limit_seconds") or 0
    ext_max = plan.get("max_extensions") or 0
    used = clock.get("extensions_used") or 0
    elapsed = (clock.get("elapsed_ms") or 0) / 1000.0
    remaining = limit - elapsed if limit else None
    st = state_of(op, uuid)
    log(f"[{name}] 状态={st} 会话={clock.get('status')} 已用={elapsed/60:.1f}min"
        + (f" 剩余={remaining/60:.1f}min" if remaining is not None else "")
        + f" 续期={used}/{ext_max}")

    if st not in STATE_OK:
        if power(op, uuid, "start"):
            log("    → 已发送开机")
            return "started"
        return "fail"

    if remaining is not None and remaining <= EXTEND_WINDOW_SEC:
        if used < ext_max:
            e_st, e_d = api(op, f"/api/client/servers/{uuid}/ogn-session/extend", "POST", {})
            log(f"    extend → {e_st} {str(e_d)[:140]}")
            if e_st == 200:
                return "extended"
        log("    续期额度已用尽 → 换新会话（stop → start）")
        power(op, uuid, "stop")
        time.sleep(8)
        power(op, uuid, "start")
        return "resession"
    return "ok"


def main():
    try:
        op = login()
    except Exception as e:  # noqa: BLE001
        log(f"❌ {e}")
        return 3
    try:
        lst = servers(op)
    except Exception as e:  # noqa: BLE001
        log(f"❌ {e}")
        return 3
    log(f"账户下 {len(lst)} 台服务器")

    deadline = time.time() + max(DWELL_MIN, 0) * 60
    fails = 0
    while True:
        for s in lst:
            try:
                r = ensure(op, s)
                if r == "fail":
                    fails += 1
            except Exception as e:  # noqa: BLE001
                log(f"[{s.get('name')}] ❌ {e}")
                fails += 1
        if time.time() + CHECK_SEC > deadline:
            break
        time.sleep(CHECK_SEC)
        try:
            lst = servers(op)
        except Exception as e:  # noqa: BLE001
            log(f"❌ 刷新服务器列表失败：{e}")
            break
    log("本轮结束")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
