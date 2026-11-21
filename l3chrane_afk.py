#!/usr/bin/env python3
"""L3chrane X Bots —— AFK 挂机赚币（l3chranexbots.oriaxen.com）
                       「挂满 360 分钟 → 冷却 44 分钟 → 继续」永续循环版

站点机制（2026-11-13 / 11-16 实测）
────────────────────────────────────────────────────────────────
· 托管平台：Discord bot 托管（Node.js / Python），免费档 512MB / 50% CPU / 600MB
· 货币：coin。部署一台服务器需 5 coins
· 官方 AFK：打开 /afk 页面，每 60 秒自动 +1 coin（前端计时器纯内存）
· 接口：POST /afk/claim  → {success, coins, newBalance, dailyEarned}
  ⚠️ 实测：**服务端不做任何间隔校验、无 cooldown、maxDaily=0（无日上限）**
     （1 秒内连刷 7~10 次全部 200）
· 登录：Discord OAuth（scope=identify）→ Express session cookie `connect.sid`

本脚本（工作循环）
────────────────────────────────────────────────────────────────
  WORK_MIN=360 分钟持续挂（AFK 不中断，按官方 60s 节奏 claim）
  → COOL_MIN=44 分钟冷却（完全停手，不请求 /afk/claim）
  → 继续下一个 360 分钟 …… 永续

  一个 GH Actions job 只跑得了 ≤6 小时，所以真正的「360+44」节拍由
  workflow 的接力链承担：job 跑满 360 分钟 → 状态写状态文件 → 自派发
  下一次 run，下一次 run 开头先 sleep 44 分钟再做 360 分钟，如此交替。

环境变量
  L3CH_DISCORD_TOKEN  Discord 用户 token（必填）
  L3CH_GOAL           目标余额（0 = 不设上限，默认 0）
  L3CH_DELAY          每次 claim 间隔秒（默认 60 = 官网「1 分钟 1 积分」；最小 1）
  L3CH_BASE           默认 https://l3chranexbots.oriaxen.com

用法
  python3 l3chrane_afk.py --loop 360     # 持续挂 360 分钟（work 段）
  python3 l3chrane_afk.py --cool 44      # 冷却 44 分钟（期间不请求站点）
  python3 l3chrane_afk.py --cycle        # 按 state 里的相位决定 work 还是 cool
  python3 l3chrane_afk.py --status       # 只查余额
  python3 l3chrane_afk.py --once         # 单次 claim（连通性自检）
"""

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("L3CH_BASE", "https://l3chranexbots.oriaxen.com").rstrip("/")
DISCORD_TOKEN = os.environ.get("L3CH_DISCORD_TOKEN", "").strip()
GOAL = int(os.environ.get("L3CH_GOAL", "0"))          # 0 = 无上限，一直挂
DELAY = max(1.0, float(os.environ.get("L3CH_DELAY", "60")))
WORK_MIN = int(os.environ.get("L3CH_WORK_MIN", "360"))
COOL_MIN = int(os.environ.get("L3CH_COOL_MIN", "44"))

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l3chrane_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

# 站点 OAuth client（从 /auth/discord 的 302 里解出来的公开值）
CLIENT_ID = "1519415022168571914"
REDIRECT = f"{BASE}/auth/discord/callback"


def log(m):
    print(m, flush=True)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    st["updated_at"] = _now()
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


class Site:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # ── 基础请求 ──────────────────────────────────────────
    def req(self, path, method="GET", body=None, form=None, headers=None, retries=4):
        url = path if path.startswith("http") else BASE + path
        last = None
        for attempt in range(retries):
            data = None
            h = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
            if headers:
                h.update(headers)
            if form is not None:
                data = urllib.parse.urlencode(form).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            elif body is not None:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            r = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with self.op.open(r, timeout=45) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return resp.status, raw, dict(resp.headers)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                return e.code, raw, dict(e.headers)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                if attempt < retries - 1:
                    time.sleep(4 * (attempt + 1))
        return 0, json.dumps({"error": last or "unreachable"}), {}

    # ── 登录：Discord OAuth ───────────────────────────────
    def login(self):
        log("🔑 走 Discord OAuth 登录…")
        auth = {"Authorization": DISCORD_TOKEN}
        q = urllib.parse.urlencode({
            "client_id": CLIENT_ID, "scope": "identify",
            "response_type": "code", "redirect_uri": REDIRECT,
        })
        st, raw, _ = self.req(f"https://discord.com/api/oauth2/authorize?{q}", headers=auth)
        if st == 401:
            log("❌ Discord token 失效")
            return False
        st, raw, _ = self.req(
            f"https://discord.com/api/v9/oauth2/authorize?{q}",
            method="POST", headers={**auth, "Content-Type": "application/json"},
            body={"permissions": "0", "authorize": True, "captcha_key": None,
                  "integration_type": 0, "guild_id": None},
        )
        try:
            loc = json.loads(raw).get("location", "")
        except Exception:  # noqa: BLE001
            log(f"❌ 授权失败 HTTP {st}: {raw[:200]}")
            return False
        m = re.search(r"[?&]code=([A-Za-z0-9]+)", loc)
        if not m:
            log(f"❌ 未拿到 OAuth code（返回：{raw[:200]}）")
            return False
        st, raw, _ = self.req(f"/auth/discord/callback?code={m.group(1)}&iss=https%3A%2F%2Fdiscord.com")
        if not self.sid():
            log(f"❌ 回调未拿到 session（HTTP {st}）")
            return False
        save_state(sid=self.sid())
        log("✅ 登录成功（connect.sid 已取）")
        return True

    def seed(self, sid):
        host = urllib.parse.urlparse(BASE).hostname
        self.jar.set_cookie(http.cookiejar.Cookie(
            0, "connect.sid", sid, None, False, host, True, False,
            "/", True, True, None, False, None, None, {}, False))

    def sid(self):
        for c in self.jar:
            if c.name == "connect.sid":
                return c.value
        return None

    # ── 业务 ──────────────────────────────────────────────
    def claim(self):
        st, raw, _ = self.req("/afk/claim", method="POST", body={})
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            d = {"raw": raw[:120]}
        return st, (d if isinstance(d, dict) else {})

    def balance(self):
        st, raw, _ = self.req("/afk")
        if st != 200:
            return None
        m = re.search(r'id="totalCoins"[^>]*>\s*(\d+)', raw)
        return int(m.group(1)) if m else None


def ensure_login(site):
    st = load_state()
    if st.get("sid"):
        site.seed(st["sid"])
        b = site.balance()
        if b is not None:
            return True
        log("⚠️ 缓存会话失效，重新登录")
    return site.login()


# ─────────────────────────────────────────────────────────
#  work 段：持续挂 minutes 分钟
# ─────────────────────────────────────────────────────────
def work(site, minutes, once=False, status_only=False):
    if not ensure_login(site):
        return False

    bal = site.balance()
    log(f"💰 当前余额：{bal if bal is not None else '未知'} coins"
        f"｜目标 {GOAL if GOAL > 0 else '无上限'}")
    if status_only:
        return True
    if GOAL > 0 and bal is not None and bal >= GOAL:
        log(f"✅ 已达目标 {GOAL}，本轮不挂")
        return True

    if once:
        st, d = site.claim()
        log(f"  claim → HTTP {st} {json.dumps(d, ensure_ascii=False)[:160]}")
        return st == 200 and d.get("success", False)

    deadline = time.time() + minutes * 60
    gained = 0
    fail = 0
    last_log = 0.0
    log(f"🔥 开始持续挂机 {minutes} 分钟（claim 间隔 {DELAY:.0f}s = 官网规则 60s，"
        f"预计 +{int(minutes*60//DELAY)} coins）")
    while time.time() < deadline:
        st, d = site.claim()
        if st == 200 and d.get("success"):
            gained += 1
            fail = 0
            nb = d.get("newBalance")
            now = time.time()
            if now - last_log > 300 or gained <= 3:      # 每 5 分钟汇报一次
                left = (deadline - now) / 60
                log(f"  +1 → 余额 {nb}｜本段 {gained} 次｜剩 {left:.0f} 分钟")
                last_log = now
            save_state(sid=site.sid(), balance=nb, gained_session=gained, phase="work",
                       last_claim=_now())
            if GOAL > 0 and isinstance(nb, int) and nb >= GOAL:
                log(f"🎯 达到目标 {GOAL} coins，停止")
                break
        elif st in (401, 403):
            log(f"⚠️ 会话失效（HTTP {st}），重新登录")
            if not site.login():
                return False
            continue
        else:
            fail += 1
            log(f"⚠️ claim 失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:160]}")
            if fail >= 5:
                log("❌ 连续 5 次失败，结束本段")
                return False
            time.sleep(min(60, 10 * fail))
            continue
        rest = deadline - time.time()
        if rest <= 0:
            break
        time.sleep(min(DELAY, rest))
    log(f"— work 段结束：本段 +{gained} coins —")
    save_state(sid=site.sid(), phase="work_done", gained_session=gained,
               work_ended=_now(), last_run=_now())
    return True


# ─────────────────────────────────────────────────────────
#  cool 段：完全停手 minutes 分钟（不碰站点接口）
# ─────────────────────────────────────────────────────────
def cool(minutes):
    if minutes <= 0:
        log("⏭️ 冷却时长 0，跳过")
        return True
    log(f"🧊 冷却 {minutes} 分钟（期间不请求 /afk/claim）")
    save_state(phase="cool")
    end = time.time() + minutes * 60
    while time.time() < end:
        left = end - time.time()
        log(f"  ⏳ 冷却中，剩 {left/60:.1f} 分钟")
        time.sleep(min(600, max(1, left)))
    save_state(phase="cool_done", cool_ended=_now(), last_run=_now())
    log("— cool 段结束 —")
    return True


# ─────────────────────────────────────────────────────────
#  cycle：按 state 相位交替 work / cool
# ─────────────────────────────────────────────────────────
def cycle(work_min, cool_min):
    st = load_state()
    phase = st.get("phase", "cool_done")
    site = Site()
    if phase in ("cool", "cool_done"):
        # 上一段是冷却（或首次）→ 现在干 360 分钟
        log(f"▶️ 相位={phase} → 进入 work 段 {work_min} 分钟")
        ok = work(site, work_min)
        save_state(phase="work_done", next_phase="cool")
        return 0 if ok else 2
    # 上一段是 work → 现在冷却 44 分钟
    log(f"▶️ 相位={phase} → 进入 cool 段 {cool_min} 分钟")
    cool(cool_min)
    save_state(phase="cool_done", next_phase="work")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, default=None, metavar="MINUTES", help="持续挂 minutes 分钟")
    p.add_argument("--cool", type=int, default=None, metavar="MINUTES", help="冷却 minutes 分钟（0 = 跳过）")
    p.add_argument("--cycle", action="store_true", help="按 state 相位自动决定 work/cool")
    p.add_argument("--work-min", type=int, default=WORK_MIN)
    p.add_argument("--cool-min", type=int, default=COOL_MIN)
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()

    log(f"🎯 L3chrane AFK · {BASE}")

    # cool 段不需要登录态，先处理，避免缺 token 直接失败
    if a.cool is not None and not a.loop and not a.cycle:
        if a.cool <= 0:
            log("⏭️ cool 段时长 0，跳过")
            return 0
        return 0 if cool(a.cool) else 2

    if not DISCORD_TOKEN:
        log("❌ 缺 L3CH_DISCORD_TOKEN")
        return 3

    if a.cycle:
        return cycle(a.work_min, a.cool_min)
    if a.cool is not None:
        if a.cool <= 0:
            log("⏭️ cool 段时长 0，跳过")
            return 0
        return 0 if cool(a.cool) else 2
    if a.loop is not None:
        if a.loop <= 0:
            log("⏭️ work 段时长 0，跳过")
            return 0
        return 0 if work(Site(), a.loop) else 2
    ok = work(Site(), 0, once=a.once, status_only=a.status)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
