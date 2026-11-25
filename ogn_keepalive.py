#!/usr/bin/env python3
"""OpenGameNode（panel.opengamenode.com / Calagopus）守护 —— 纯 HTTP。

策略（按用户要求）：
  1. 前期能续就续：会话到期前最后 10 分钟才能 extend，脚本精确等到 deadline-60s 再发，
     最多 max_extensions 次（FreeTier = 2 次，每次 +4h）→ 单会话最长 12 小时连续在线。
  2. 额度用完：等到 deadline 之后服务器自动 offline，立刻 power start 换新会话。

时间全部取自服务端 clock（毫秒 epoch），不靠本地估算：
  clock.session_deadline     本次会话的确切到期时刻
  clock.extensions_used      已用续期次数
  plan.max_extensions        允许的续期次数
  plan.extension_seconds     每次续期时长

环境变量：
  OGN_DISCORD_TOKEN    必填
  OGN_SERVER_UUIDS     可选，逗号分隔；留空=账户下全部
  OGN_DWELL_MINUTES    本轮驻留分钟数，默认 350（配合 6 小时 cron，几乎不断档）
  OGN_EXTEND_AT_SEC    在到期前多少秒发 extend，默认 60（窗口是最后 600 秒）
  OGN_MODE             probe = 只读打印计划，不执行任何写操作
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

BASE = "https://panel.opengamenode.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROVIDER = os.environ.get("OGN_OAUTH_PROVIDER", "609712d4-1d05-45ad-8ad9-eea05c78941a")
TOKEN = os.environ.get("OGN_DISCORD_TOKEN", "").strip()
WANT = [u.strip() for u in os.environ.get("OGN_SERVER_UUIDS", "").split(",") if u.strip()]
DWELL_MIN = float(os.environ.get("OGN_DWELL_MINUTES", "350") or 0)
EXTEND_AT = int(os.environ.get("OGN_EXTEND_AT_SEC", "60") or 60)
MODE = os.environ.get("OGN_MODE", "act").strip().lower()
STATE_OK = {"running", "starting"}
MAX_SLEEP = 300


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


# ---------------- 调度（纯函数，可单测） ----------------
def plan(state, snap, now):
    """返回 (action, wake_in_seconds)。
    action: ok | start | extend | wait_expire | none
    """
    clock = (snap or {}).get("clock") or {}
    pl = (snap or {}).get("plan") or {}
    used = clock.get("extensions_used") or 0
    max_ext = pl.get("max_extensions") or 0
    deadline_ms = clock.get("session_deadline")

    if state not in STATE_OK:
        return "start", 30

    if not deadline_ms:
        # 没拿到 deadline（会话未登记），用 limit-elapsed 兜底
        limit = pl.get("session_limit_seconds") or 0
        elapsed = (clock.get("elapsed_ms") or 0) / 1000.0
        if limit and elapsed:
            deadline = now + max(limit - elapsed, 0)
        else:
            return "ok", 60
    else:
        deadline = deadline_ms / 1000.0

    remaining = deadline - now

    if remaining <= 0:
        # 到点了但还没变 offline，稍等再看
        return "wait_expire", 20

    if used < max_ext:
        if remaining <= EXTEND_AT:
            return "extend", 15
        return "ok", min(max(remaining - EXTEND_AT, 5), MAX_SLEEP)

    # 续期额度用完：等会话自然到期，然后 start
    return "ok", min(max(remaining + 5, 5), MAX_SLEEP)


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
    cid, state, red = (re.search(r"client_id=(\d+)", loc),
                       re.search(r"state=([^&]+)", loc),
                       re.search(r"redirect_uri=([^&]+)", loc))
    if not (cid and state and red):
        raise RuntimeError(f"OAuth 起始失败 st={st} loc={loc[:120]}")
    q = urllib.parse.urlencode({"client_id": cid.group(1), "scope": "email identify",
                                "response_type": "code",
                                "redirect_uri": urllib.parse.unquote(red.group(1))})
    st, _, body = do(op, f"https://discord.com/api/v9/oauth2/authorize?{q}", "POST",
                     {"permissions": "0", "authorize": True, "integration_type": 0,
                      "guild_id": None, "channel_id": None, "captcha_key": None},
                     {"Authorization": TOKEN, "Content-Type": "application/json",
                      "Origin": "https://discord.com"})
    if st != 200:
        raise RuntimeError(f"Discord authorize {st}: {body[:200]}")
    cb = json.loads(body)["location"]
    do(op, cb + ("&" if "?" in cb else "?") + "state=" + state.group(1))
    if not any(c.name == "calagopus_session" for c in cj):
        do(op, BASE + "/")
    if not any(c.name == "calagopus_session" for c in cj):
        raise RuntimeError("登录没拿到 calagopus_session（该 Discord 号是否绑过面板？）")
    log("✅ 登录成功（Discord OAuth）")
    return op


def api(op, path, method="GET", data=None):
    st, _, body = do(op, BASE + path, method, data,
                     {"Origin": BASE, "Content-Type": "application/json", "Referer": BASE + "/"})
    try:
        return st, (json.loads(body) if body else {})
    except Exception:  # noqa: BLE001
        return st, {"_raw": body[:300]}


def get_servers(op):
    st, d = api(op, "/api/client/servers")
    if st != 200:
        raise RuntimeError(f"servers {st}: {str(d)[:200]}")
    return d.get("servers", {}).get("data", [])


def get_session(op, uuid):
    st, d = api(op, f"/api/client/servers/{uuid}/ogn-session")
    return d if st == 200 else {}


def get_state(op, uuid):
    st, d = api(op, f"/api/client/servers/{uuid}/resources")
    return (d.get("resources") or {}).get("state") if st == 200 else None


def main():
    try:
        op = login()
    except Exception as e:  # noqa: BLE001
        log(f"❌ {e}")
        return 3
    try:
        servers = get_servers(op)
    except Exception as e:  # noqa: BLE001
        log(f"❌ {e}")
        return 3
    if WANT:
        servers = [s for s in servers if s["uuid"] in WANT]
    log(f"守护 {len(servers)} 台服务器 · 驻留 {DWELL_MIN:g} 分钟 · 模式 {MODE}")

    end_at = time.time() + max(DWELL_MIN, 0) * 60
    fails = 0
    while True:
        wake_in = MAX_SLEEP
        for s in servers:
            uuid, name = s["uuid"], s.get("name")
            try:
                snap = get_session(op, uuid)
                state = get_state(op, uuid)
                action, wake = plan(state, snap, time.time())
                wake_in = min(wake_in, wake)
                clock = snap.get("clock") or {}
                pl = snap.get("plan") or {}
                dl = clock.get("session_deadline")
                left = f"{(dl/1000-time.time())/60:.1f}min" if dl else "?"
                log(f"[{name}] 状态={state} 剩余={left} 续期={clock.get('extensions_used')}/{pl.get('max_extensions')} → {action}")

                if MODE == "probe" or action in ("ok", "none", "wait_expire"):
                    continue
                if action == "start":
                    st, _ = api(op, f"/api/client/servers/{uuid}/power", "POST", {"action": "start"})
                    log(f"    🚀 start → {st}")
                    if st not in (200, 202, 204):
                        fails += 1
                elif action == "extend":
                    st, d = api(op, f"/api/client/servers/{uuid}/ogn-session/extend", "POST", {})
                    if st == 200:
                        log(f"    ➕ extend 成功 → {str(d)[:120]}")
                    else:
                        log(f"    ⚠️ extend {st} {str(d)[:160]}")
                        if st == 409:
                            # 会话已经到期了，直接转启动
                            st2, _ = api(op, f"/api/client/servers/{uuid}/power", "POST", {"action": "start"})
                            log(f"    🚀 start（续期太晚，改换新会话）→ {st2}")
                        else:
                            fails += 1
            except Exception as e:  # noqa: BLE001
                log(f"[{name}] ❌ {e}")
                fails += 1
        now = time.time()
        if now >= end_at:
            log("本轮驻留结束（下一轮由 cron 接续）")
            break
        slp = min(wake_in, max(end_at - now, 0), MAX_SLEEP)
        if slp <= 0:
            time.sleep(5)
        else:
            log(f"— 休眠 {slp:.0f}s —")
            time.sleep(slp)
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
