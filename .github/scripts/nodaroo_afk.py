"""Nodaroo Free AFK 挂机 —— 纯 Python 直连版（无浏览器）。

站点规则（free.nodaroo.com/afk 页面原文）：
    Earn 1 credit every 60 seconds while this page is connected.
    One earning tab per account. Reconnect if your browser sleeps.

计分机制：
    页面把 data-afk-token 作为 WebSocket subprotocol 传给
        wss://free.nodaroo.com/afkwspath
    服务端每 60 秒推 {"balance": N}，余额涨即到账。客户端只收不发。

为什么不用浏览器（2026-11-16 实测）：
    本沙箱用 requests + websockets(16) 直连完整跑通（登录 -> 取 token ->
    WS 计分，余额 1->4 逐分钟到账）。之前 CI 里的失败是两层问题：
      1) websocket-client 库的 WS 指纹被服务端秒断 -> 换 websockets 库解决
      2) Playwright + 静态 cf_clearance 在 CI 被挑战循环卡死
         （clearance 与出口 IP 强绑定，VLESS 节点一换就失效）-> 纯 HTTP
         请求根本不触发 CF 托管挑战，问题不存在了
    若 CI 直连仍被 CF 拦（脚本会明确报出来），设 NODAROO_VLESS_URL 走
    家宽出口兜底（脚本内自动拉起 xray，见 fallback_proxy()）。

节奏（用户指定）：
    挂机 6 小时 -> 冷却 30 分钟 -> 再挂 6 小时 -> ...
    GitHub 单 job 上限 360 分钟装不下 6.5 小时，由 workflow 自派发接力。

登录（三级回退，与 mws 分支一致）：
    加密存档 cookie -> NODAROO_COOKIE -> Discord token OAuth。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

import requests

BASE = "https://free.nodaroo.com"
AFK_PAGE = f"{BASE}/afk"
STATE_FILE = "nodaroo_state.json"

DISCORD_TOKEN = os.environ.get("NODAROO_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("NODAROO_SESSION_KEY", "").strip()
STATIC_COOKIE = os.environ.get("NODAROO_COOKIE", "").strip()
PROXY = os.environ.get("NODAROO_PROXY", "").strip()          # socks5://127.0.0.1:40000
VLESS_URL = os.environ.get("NODAROO_VLESS_URL", "").strip()  # 家宽订阅兜底
XRAY_BIN = os.environ.get("XRAY_BIN", os.path.expanduser("~/.xray/xray"))
COOKIE_NAME = "nodaroo_free"

# 站点的 OAuth client（/login 的 302 里是公开信息）
CLIENT_ID = "1569058628902719678"
REDIRECT_URI = f"{BASE}/callback"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

FARM_MINUTES = float(os.environ.get("NODAROO_FARM_MINUTES", "350"))
NO_EARN_MINUTES = float(os.environ.get("NODAROO_NO_EARN_MINUTES", "10"))
WS_RECV_TIMEOUT = float(os.environ.get("NODAROO_WS_RECV_TIMEOUT", "90"))

_output_written = {}


def log(msg):
    print(msg, flush=True)


def emit(key, value):
    """GITHUB_OUTPUT，只写一次，避免多值。"""
    if key in _output_written:
        return
    _output_written[key] = True
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def mask(s, head=10):
    return (s[:head] + "...") if s else "(空)"


# ------------------------------------------------------------------ 状态存档
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(cookie):
    if not SESSION_KEY:
        log("⚠️ 未配置 NODAROO_SESSION_KEY，登录态不持久化（每轮走 OAuth）")
        return
    try:
        from cryptography.fernet import Fernet
        blob = Fernet(SESSION_KEY.encode()).encrypt(
            json.dumps({"cookie": cookie}).encode()).decode()
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"encrypted_cookie": blob}, fh)
        log(f"💾 登录态已加密保存（cookie {mask(cookie, 12)}）")
    except Exception as exc:
        log(f"⚠️ 保存登录态失败: {type(exc).__name__}")


def decrypt_cookie(state):
    if not SESSION_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        blob = state.get("encrypted_cookie")
        if not blob:
            return None
        raw = Fernet(SESSION_KEY.encode()).decrypt(blob.encode()).decode()
        data = json.loads(raw)
        return data.get("cookie") if isinstance(data, dict) else None
    except Exception:
        return None


# ------------------------------------------------------------------ HTTP 会话
def site_proxies():
    return {"http": PROXY, "https": PROXY} if PROXY else None


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if PROXY:
        s.proxies.update(site_proxies())
    return s


def is_cf_blocked(resp):
    """识别 Cloudflare 托管挑战/拦截。"""
    if resp.status_code in (403, 503, 429):
        if resp.headers.get("cf-mitigated") == "challenge":
            return True
        body = (resp.text or "")[:4000].lower()
        return "just a moment" in body or "cf-challenge" in body
    return False


# ------------------------------------------------------------------ 登录
def cookie_valid(s, cookie):
    """GET /dashboard：200 有效；302 去 /login 无效。"""
    try:
        r = s.get(f"{BASE}/dashboard", cookies={COOKIE_NAME: cookie},
                  allow_redirects=False, timeout=30)
        if r.status_code == 200:
            return True
        if is_cf_blocked(r):
            log(f"  ⚠️ 校验 cookie 时被 CF 拦（{r.status_code}），按无效处理")
        return False
    except requests.RequestException as exc:
        log(f"  ⚠️ cookie 校验网络异常: {type(exc).__name__}")
        return False


def oauth_login():
    """Discord token -> OAuth 全链 -> 站点 session cookie。

    参数全放 query，body 只放 {"authorize": true}；偶尔 400，重试能过。
    返回 (cookie, session)——session 里带着登录态，调用方要接着用它。
    """
    if not DISCORD_TOKEN:
        log("⚠️ 没有 NODAROO_DISCORD_TOKEN，跳过 OAuth")
        return None, None
    for attempt in range(1, 7):
        s = new_session()
        try:
            r = s.get(f"{BASE}/login", allow_redirects=False, timeout=30)
            if r.status_code != 302:
                log(f"  [{attempt}] /login -> {r.status_code}"
                    + ("（CF 拦截）" if is_cf_blocked(r) else "") + "，重试")
                time.sleep(5)
                continue
            m = re.search(r"[?&]state=([^&]+)", r.headers.get("location", ""))
            if not m:
                time.sleep(5)
                continue
            state = urllib.parse.unquote(m.group(1))

            url = (f"https://discord.com/api/v9/oauth2/authorize?client_id={CLIENT_ID}"
                   f"&response_type=code"
                   f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
                   f"&scope=identify%20email%20guilds.join"
                   f"&state={urllib.parse.quote(state, safe='')}")
            ra = s.post(url, data=json.dumps({"authorize": True, "permissions": "0"}),
                        headers={"Authorization": DISCORD_TOKEN,
                                 "Content-Type": "application/json",
                                 "Origin": "https://discord.com",
                                 "Referer": "https://discord.com/"},
                        timeout=30)
            try:
                cb_url = (ra.json() or {}).get("location", "")
            except ValueError:
                cb_url = ra.headers.get("location", "")
            if not cb_url:
                log(f"  [{attempt}] Discord authorize 失败 {ra.status_code}: "
                    f"{ra.text[:120]}")
                time.sleep(6)
                continue
            if cb_url.startswith("/"):
                cb_url = "https://discord.com" + cb_url

            rc = s.get(cb_url, allow_redirects=False, timeout=30)
            cookie = s.cookies.get(COOKIE_NAME)
            if rc.status_code in (301, 302) and cookie:
                log(f"✅ OAuth 登录成功（第 {attempt} 次尝试，cookie {mask(cookie, 12)}）")
                return cookie, s
            log(f"  [{attempt}] callback {rc.status_code}: {rc.text[:80]}")
            time.sleep(6)
        except requests.RequestException as exc:
            log(f"  [{attempt}] OAuth 异常 {type(exc).__name__}: {exc}")
            time.sleep(6)
    return None, None


def get_session_cookie():
    """三级回退拿可用 cookie。返回 (cookie, 来源, session)。"""
    s = new_session()
    state = load_state()
    cookie = decrypt_cookie(state)
    if cookie:
        log(f"♻️ 尝试存档 cookie（{mask(cookie, 12)}）")
        if cookie_valid(s, cookie):
            log("✅ 存档 cookie 有效")
            s.cookies.set(COOKIE_NAME, cookie, domain="free.nodaroo.com")
            return cookie, "state", s
        log("  存档 cookie 已失效")
    if STATIC_COOKIE:
        log(f"🍪 尝试 NODAROO_COOKIE（{mask(STATIC_COOKIE, 12)}）")
        if cookie_valid(s, STATIC_COOKIE):
            log("✅ 静态 cookie 有效")
            s.cookies.set(COOKIE_NAME, STATIC_COOKIE, domain="free.nodaroo.com")
            return STATIC_COOKIE, "static", s
        log("  静态 cookie 已失效")
    log("🔑 走 Discord OAuth 登录")
    cookie, s = oauth_login()
    if cookie:
        return cookie, "oauth", s
    return None, None, s


# ------------------------------------------------------------------ VLESS 兜底
def fallback_proxy():
    """直连被 CF 拦且未给代理时，用 VLESS_URL 拉起 xray。

    返回 True 表示 NODAROO_PROXY 已就绪（进程组常驻，交给 job 结束回收）。
    """
    global PROXY
    if not VLESS_URL:
        log("::error::直连被 CF 拦且未配置 NODAROO_VLESS_URL，无法兜底")
        return False
    if not os.path.exists(XRAY_BIN):
        log(f"::error::本机没有 xray（{XRAY_BIN}），无法兜底；"
            "workflow 需保留 Install xray 步骤")
        return False
    log("🛡️ 启动 VLESS 家宽出口兜底...")
    try:
        import urllib.parse as up
        u = up.urlparse(VLESS_URL)
        q = up.parse_qs(u.query)
        outbound = {
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": u.hostname, "port": u.port or 443,
                "users": [{"id": u.username, "encryption": "none"}],
            }]},
            "streamSettings": {
                "network": q.get("type", ["ws"])[0],
                "security": q.get("security", ["tls"])[0],
                "tlsSettings": {"serverName": q.get("sni", [u.hostname])[0],
                                "fingerprint": q.get("fp", ["chrome"])[0]},
                "wsSettings": {"path": q.get("path", ["/"])[0]},
            },
        }
        conf = {"log": {"loglevel": "warning"},
                "inbounds": [{"port": 40000, "listen": "127.0.0.1",
                              "protocol": "socks", "settings": {"udp": False}}],
                "outbounds": [outbound]}
        conf_path = "/tmp/nodaroo_xray.json"
        with open(conf_path, "w", encoding="utf-8") as fh:
            json.dump(conf, fh)
        proc = subprocess.Popen([XRAY_BIN, "run", "-c", conf_path],
                                stdout=subprocess.DEVNULL,
                                stderr=open("/tmp/nodaroo_xray.log", "wb"),
                                start_new_session=True)
        time.sleep(3)
        if proc.poll() is not None:
            log(f"::error::xray 启动即退（exit {proc.returncode}），详见日志")
            return False
        for _ in range(15):
            try:
                tr = requests.get("https://www.cloudflare.com/cdn-cgi/trace",
                                  proxies={"http": "socks5h://127.0.0.1:40000",
                                           "https": "socks5h://127.0.0.1:40000"},
                                  timeout=8)
                ip = re.search(r"^ip=(.+)$", tr.text, re.M)
                if tr.status_code == 200 and ip:
                    log(f"✅ VLESS 出口就绪: {ip.group(1)}")
                    PROXY = "socks5://127.0.0.1:40000"
                    return True
            except requests.RequestException:
                pass
            time.sleep(2)
        log("::error::VLESS 出口未就绪")
        return False
    except Exception as exc:
        log(f"::error::VLESS 兜底异常 {type(exc).__name__}: {exc}")
        return False


# ------------------------------------------------------------------ AFK 页面
def fetch_afk_token(s):
    """GET /afk -> (afk_token, every, reward)；CF 拦截返回 None。"""
    r = s.get(AFK_PAGE, timeout=30)
    if r.status_code != 200:
        log(f"  /afk -> {r.status_code}" + ("（CF 拦截）" if is_cf_blocked(r) else ""))
        return None
    body = r.text
    if "Just a moment" in body[:2000]:
        log("  /afk 返回 CF 挑战页")
        return None
    m = re.search(r'data-afk-token="([^"]+)"', body)
    if not m:
        # 未登录会被 302 到 /login（requests 默认跟随），此时页面是登录页
        if "Continue with Discord" in body:
            log("  /afk 内容是登录页 -> cookie 失效")
        else:
            log("  /afk 页面结构变了，读不到 data-afk-token")
        return None
    every = re.search(r'data-afk-every="([^"]+)"', body)
    reward = re.search(r'data-afk-reward="([^"]+)"', body)
    return m.group(1), int(every.group(1)) if every else 60, \
        int(reward.group(1)) if reward else 1


# ------------------------------------------------------------------ WS 挂机
def farm_cycle(s, cookie, afk_token, deadline, stats):
    """开一条 WS 收分，直到断线/无收益/到 deadline。返回是否该整体续期重连。"""
    import asyncio
    import websockets

    async def run():
        url = "wss://free.nodaroo.com/afkwspath"
        headers = {"Origin": BASE, "User-Agent": UA, "Cookie": f"{COOKIE_NAME}={cookie}"}
        kw = dict(subprotocols=[afk_token], additional_headers=headers,
                  ping_interval=20, ping_timeout=20, close_timeout=5)
        if PROXY:
            kw["proxy"] = PROXY
        else:
            kw["proxy"] = None
        async with websockets.connect(url, **kw) as ws:
            log(f"🔌 WS 已连上（token {mask(afk_token)}，"
                f"{'经代理' if PROXY else '直连'}），开始收分")
            stats["connects"] += 1
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    return "deadline"
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=WS_RECV_TIMEOUT)
                except (asyncio.TimeoutError, TimeoutError):
                    return "stale"
                try:
                    d = json.loads(msg)
                except ValueError:
                    log(f"  非JSON消息: {str(msg)[:80]}")
                    continue
                if "error" in d:
                    log(f"  服务端报错: {d['error']}")
                    return "error"
                bal = d.get("balance")
                if bal is None:
                    continue
                now = time.time()
                if stats["balance"] is not None and bal > stats["balance"]:
                    gain = bal - stats["balance"]
                    stats["earned"] += gain
                    stats["last_gain"] = now
                    log(f"  [{int((now - stats['t0']) // 60):3d}m] +{gain} -> "
                        f"balance {bal}（本轮累计 +{stats['earned']}）")
                    emit("earned", str(stats["earned"]))
                    emit("balance", str(bal))
                elif stats["balance"] is not None and bal < stats["balance"]:
                    log(f"  [{int((now - stats['t0']) // 60):3d}m] 余额下降 "
                        f"{stats['balance']} -> {bal}（花了积分）")
                stats["balance"] = bal
                stats["last_msg"] = now

    reason = asyncio.run(run())
    log(f"  本条 WS 结束: {reason}")
    return reason


def main():
    log("#" * 44)
    log(f"  Nodaroo AFK 纯Python挂机 {FARM_MINUTES:.0f} 分钟（60s/credit）")
    log("#" * 44)
    if PROXY:
        log(f"出口代理（workflow 指定）: {PROXY}")

    cookie, source, s = get_session_cookie()
    if not cookie:
        # 直连被 CF 拦会把有效 cookie 误判失效 —— 先试一次代理兜底再重来
        if PROXY:
            log("::error::三种登录方式全部失败")
            return 1
        if VLESS_URL or os.path.exists(XRAY_BIN):
            log("登录失败 -> 尝试 VLESS 代理兜底后重试")
            if fallback_proxy():
                s = new_session()
                cookie, source, s2 = get_session_cookie()
                if cookie:
                    s = s2
        if not cookie:
            log("::error::拿不到登录 cookie（Discord token 失效？）")
            return 1
    log(f"登录方式: {source}，cookie {mask(cookie, 12)}")
    save_state(cookie)

    deadline = time.time() + FARM_MINUTES * 60
    stats = {"balance": None, "earned": 0, "connects": 0,
             "last_gain": time.time(), "last_msg": time.time(), "t0": time.time()}
    fail_streak = 0
    exit_code = 0

    while time.time() < deadline:
        got = fetch_afk_token(s)
        if got is None:
            fail_streak += 1
            if fail_streak >= 3 and not PROXY and (VLESS_URL or os.path.exists(XRAY_BIN)):
                log("连续拉不到 AFK token -> 判定直连被 CF 拦，走 VLESS 兜底")
                if fallback_proxy():
                    s = new_session()
                    fail_streak = 0
                    continue
            if fail_streak >= 5:
                log("::error::连续 5 次拿不到 AFK token，放弃本轮")
                exit_code = 2
                break
            time.sleep(15)
            continue
        token, every, reward = got
        log(f"AFK 规则: every={every}s reward={reward}")
        try:
            reason = farm_cycle(s, cookie, token, deadline, stats)
            fail_streak = 0
        except Exception as exc:
            log(f"  WS 异常: {type(exc).__name__}: {str(exc)[:200]}")
            reason = "error"
            fail_streak += 1
            if fail_streak >= 8:
                log("::error::WS 连续 8 次异常，放弃本轮")
                exit_code = 3
                break
        if reason == "deadline":
            break
        # stale/error -> 换新 token 重连（站点允许 Reconnect if your browser sleeps）
        silent = (time.time() - stats["last_gain"]) / 60
        if silent >= NO_EARN_MINUTES and stats["balance"] is not None:
            log(f"⚠️ 已 {silent:.0f} 分钟没涨分（可能另一标签在挂），本轮提前收工")
            exit_code = 0
            break
        time.sleep(5)

    mins = (time.time() - stats["t0"]) / 60
    log("=" * 44)
    log(f"本轮结束: {mins:.1f} 分钟，重连 {stats['connects']} 次，"
        f"balance={stats['balance']}，本轮 +{stats['earned']} credits")
    log("=" * 44)
    emit("earned", str(stats["earned"]))
    emit("balance", str(stats["balance"] if stats["balance"] is not None else ""))
    emit("connects", str(stats["connects"]))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a",
              encoding="utf-8") as fh:
        fh.write(f"**Nodaroo AFK**: +{stats['earned']} credits，"
                 f"余额 {stats['balance']}，{stats['connects']} 次连接，"
                 f"{mins:.0f} 分钟\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
