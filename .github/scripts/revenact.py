#!/usr/bin/env python3
"""Revenact 控制台（console.revenact.io）—— 每日签到 + AFK 挂机赚 bolts

全部协议均为 2026-11-16 实测确认（不是推测）
════════════════════════════════════════════════════════════════
【认证】
  POST /api/auth/login            {"identifier","password"}
        → 新设备/新 IP 返回 {"twoFactorRequired":true,"challengeId":...,"method":"email",
                             "emailHint":"z•••x@gmail.com","reason":"new_location"}
        → 注意：每调一次就重发一封邮件，旧 challengeId 立即作废！不要重试。
  POST /api/auth/login/2fa        {"challengeId","code"}      ← 登录验证码（不是 step-up！）
        → 200 {"token":"<uuid>.<hex>","userId":"<uuid>"}
  ⚠️ 会话认证用 **Bearer token**，不是 cookie：
       Authorization: Bearer <token>       ✅ 200
       Cookie: revenact_session=<token>    ❌ 401
     前端会把它塞进 URL 里的 revenact_session cookie，但服务端只认 Authorization 头。
  ⚠️ token 有效期约 7 天（cookie 层 2026-11-23 到期的同一次会话）。

【TOTP 免验证码】（2026-11-16 实测通过）
  · 站点支持验证器 App（TOTP）。密钥 32 位 base32，存 REVENACT_TOTP_SECRET。
  · 启用 TOTP 后，登录常常**直接返回 token**，完全不需要 challenge；
    若仍要求 challenge，则脚本自己算出 6 位码填 login/2fa，同样无需人工。
  · 算法：HMAC-SHA1(base32decode(secret), floor(now/30)) → 动态截断 → 6 位
  · 已实测：设置 TOTP 后裸登录 200 直接给 token，验证码环节消失。

【租户 / team】
  GET /api/tenants                → {"items":[{id,name,creditsCents,packageId,...}]}
  tenant id 是奖励接口的路径参数。

【奖励】
  GET  /api/tenants/{tid}/rewards                     状态
        daily: {canClaim,lastClaimISO,currentStreak,nextStreak,baseRewardCents,
                streakBonusCents,totalRewardCents}
        afk:   {activeCount,ratePerMinuteCents,estimatedPerMinuteCents,
                dailyCapCents,earnedTodayCents,remainingTodayCents}
  POST /api/tenants/{tid}/rewards/claim               → {ok,balanceCents,baseRewardCents,
                                                          streakBonusCents,totalRewardCents,newStreak}
  POST /api/tenants/{tid}/rewards/afk/start           → {ok:true}
  POST /api/tenants/{tid}/rewards/afk/heartbeat       → {ok:true}
        ⚠️ **必须每 30 秒一次**（前端 setInterval 30000）。超过约 60 秒不发就
           "No active AFK connection"，会话作废。
  POST /api/tenants/{tid}/rewards/afk/stop

【收益实测（2026-11-16 两次长测确认）】
  · 单位：1 bolt = 100 「cents」。站点 UI「About 2.4 bolts an hour」= 240 cents/h = 4 cents/min ✔
  · 签到：冷却 **14 小时**（不是 24h）。base 1333 + streak 67/天 → 约 14.00/次
  · AFK：**4 cents/分钟 = 2.4 bolts/小时**，脉冲式结算（每 2 分钟跳 +8）
  · AFK 启动有约 1.5~2 分钟预热期不计费
  · AFK 日上限 dailyCapCents=3000 = **30 bolts**，4/min → **需 750 分钟 = 12.5 小时**
  · party 加成：同时在线 ≥3 人开始 +0.1×，8 人封顶 1.5×
  · activeCount 是全局同时挂机人数（含别人的），party 满 3 人开始加乘
  ⚠️ AFK 在部分情况下需要先过 challenge（cap token），统一从 rewards 状态判断

环境变量
  REVENACT_TOKEN       Bearer token（推荐，最稳）
  REVENACT_EMAIL       邮箱（无 token 时需要）
  REVENACT_PASSWORD    密码（无 token 时需要）
  REVENACT_TENANT      （可选）tenant id；留空自动取第一个
  REVENACT_BASE        默认 https://console.revenact.io

用法
  python revenact.py --status          只读状态
  python revenact.py --claim           每日签到
  python revenact.py --afk 50          挂机 50 分钟（跑满 AFK 日上限）
  python revenact.py --all 50          签到 + 挂机
  python revenact.py --login-only      只登录并打印 challengeId（等人工喂码）
  python revenact.py --verify CODE     用 CODE + state 里的 challengeId 换 token
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("REVENACT_BASE", "https://console.revenact.io").rstrip("/")
TOKEN = os.environ.get("REVENACT_TOKEN", "").strip()
EMAIL = os.environ.get("REVENACT_EMAIL", "").strip()
PASSWORD = os.environ.get("REVENACT_PASSWORD", "").strip()
TENANT = os.environ.get("REVENACT_TENANT", "").strip()
TOTP_SECRET = os.environ.get("REVENACT_TOTP_SECRET", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "revenact_state.json")
HEARTBEAT_SEC = 30          # 前端就是 30s，别改
CLAIM_COOLDOWN_H = 14       # 签到冷却（实测 14 小时，不是 24 小时）


def log(m):
    print(m, flush=True)


def totp(secret, at=None, step=30, digits=6):
    """RFC 6238 TOTP（SHA1）。纯标准库，无外部依赖。"""
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hm
    import struct as _st
    key = _b64.b32decode(secret.upper().replace(" ", "") +
                         "=" * ((8 - len(secret.replace(" ", "")) % 8) % 8))
    counter = int((at if at is not None else time.time()) // step)
    h = _hm.new(key, _st.pack(">Q", counter), _hl.sha1).digest()
    off = h[-1] & 0x0F
    val = (_st.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(val).zfill(digits)


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


class Api:
    def __init__(self, token=""):
        self.token = token or TOKEN or load_state().get("token", "")

    def req(self, path, method="GET", body=None, retries=3):
        url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
        last = None
        for attempt in range(retries):
            h = {"User-Agent": UA, "Accept": "application/json",
                 "Origin": BASE, "Referer": BASE + "/"}
            if self.token:
                h["Authorization"] = f"Bearer {self.token}"
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            r = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(r, timeout=45) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
        return 0, json.dumps({"error": last or "unreachable"})

    # ---------- 认证 ----------
    def login(self):
        """请求登录验证码。⚠️ 每调用一次都会发新邮件并使旧 challengeId 失效。"""
        if not EMAIL or not PASSWORD:
            log("❌ 缺 REVENACT_EMAIL / REVENACT_PASSWORD")
            return False
        st, raw = self.req("/api/auth/login", "POST",
                           {"identifier": EMAIL, "password": PASSWORD})
        log(f"  登录 HTTP {st}")
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            log(f"❌ 登录返回非 JSON: {raw[:200]}")
            return False
        if d.get("twoFactorRequired"):
            cid = d.get("challengeId")
            save_state(challenge_id=cid, challenge_at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            # 有 TOTP 密钥就自己算码，无需人工
            if TOTP_SECRET or load_state().get("totp_secret"):
                sec = TOTP_SECRET or load_state().get("totp_secret")
                now = time.time()
                for at in (now, now - 30, now + 30):
                    code = totp(sec, at)
                    st2, raw2 = self.req("/api/auth/login/2fa", "POST",
                                         {"challengeId": cid, "code": code})
                    log(f"  🔑 自动 TOTP {code} → HTTP {st2} {raw2[:120]}")
                    if st2 == 200:
                        try:
                            dd = json.loads(raw2)
                        except Exception:  # noqa: BLE001
                            continue
                        if dd.get("token"):
                            self.token = dd["token"]
                            save_state(token=dd["token"], user_id=dd.get("userId"),
                                       challenge_id=None)
                            log(f"✅ TOTP 登录成功（无需人工），token 约 7 天")
                            return True
                        break
                    if st2 == 400 and "6-digit" in raw2:
                        break   # 不能重试，避免触发风控
                log("⚠️ TOTP 自动验证未成功，回落到人工验证码")
            log("🔐 需要邮箱验证码（new_location 风控）")
            log(f"   challengeId = {cid}")
            log(f"   验证码已发到 {d.get('emailHint')}")
            log("   → 用最新一封邮件里的 6 位码：python revenact.py --verify <CODE>")
            return False
        # 理论上也可能直接给 token（同设备/IP）
        if d.get("token"):
            self.token = d["token"]
            save_state(token=d["token"], user_id=d.get("userId"))
            log("✅ 登录直接成功（无需验证码），token 已保存")
            return True
        log(f"❓ 未预期的登录响应: {raw[:200]}")
        return False

    def verify(self, code):
        cid = load_state().get("challenge_id")
        if not cid:
            log("❌ state 里没有 challengeId，先跑 --login-only")
            return False
        st, raw = self.req("/api/auth/login/2fa", "POST",
                           {"challengeId": cid, "code": code.strip()})
        log(f"  验证码校验 HTTP {st}: {raw[:200]}")
        if st != 200:
            return False
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            return False
        if not d.get("token"):
            log("❌ 响应里没有 token")
            return False
        self.token = d["token"]
        save_state(token=d["token"], user_id=d.get("userId"), challenge_id=None)
        log(f"✅ 已拿到 token（约 7 天有效）: {d['token'][:13]}…")
        return True

    # ---------- 租户 ----------
    def tenant_id(self):
        if TENANT:
            return TENANT
        st, raw = self.req("/api/tenants")
        if st != 200:
            log(f"❌ /api/tenants HTTP {st}: {raw[:200]}")
            return None
        items = json.loads(raw).get("items", [])
        if not items:
            log("❌ 账号下没有 tenant")
            return None
        t = items[0]
        save_state(tenant=t["id"])
        log(f"  租户: {t.get('name')} / {t['id']} ｜余额 {t.get('creditsCents', 0) / 100:.2f}")
        return t["id"]

    # ---------- 奖励 ----------
    def rewards(self, tid):
        st, raw = self.req(f"/api/tenants/{tid}/rewards")
        try:
            return st, json.loads(raw)
        except Exception:  # noqa: BLE001
            return st, {"raw": raw[:300]}

    def claim(self, tid):
        return self.req(f"/api/tenants/{tid}/rewards/claim", "POST", {})

    def afk(self, tid, action):
        return self.req(f"/api/tenants/{tid}/rewards/afk/{action}", "POST", {})


def c(v):
    try:
        return f"{int(v) / 100:.2f}"
    except Exception:  # noqa: BLE001
        return str(v)


def next_claim_at(d):
    """由 lastClaimISO + 14h 算出下次可领时间（UTC 字符串）。"""
    iso = (d.get("daily") or {}).get("lastClaimISO")
    if not iso:
        return None
    try:
        import datetime as _dt
        t = _dt.datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except Exception:  # noqa: BLE001
        try:
            t = _dt.datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        except Exception:  # noqa: BLE001
            return None
    nxt = t + _dt.timedelta(hours=CLAIM_COOLDOWN_H)
    return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_delta(iso):
    """距离 iso 还有多久，人类可读。"""
    if not iso:
        return "未知"
    try:
        import datetime as _dt
        t = _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        sec = (t - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    except Exception:  # noqa: BLE001
        return "未知"
    if sec <= 0:
        return "已到时间"
    h, m = divmod(int(sec) // 60, 60)
    return f"{h}h {m}m"


def print_status(d):
    day, afk = d.get("daily", {}), d.get("afk", {})
    nxt = next_claim_at(d)
    log(f"📊 签到：连击 {day.get('currentStreak')} 天｜现在可领={day.get('canClaim')}"
        f"｜下次奖励 {c(day.get('totalRewardCents'))}（基础 {c(day.get('baseRewardCents'))}"
        f" + 连击 {c(day.get('streakBonusCents'))}）")
    rem = afk.get("remainingTodayCents") or 0
    rate = afk.get("estimatedPerMinuteCents") or 1
    need = int(rem / max(rate, 1))
    log(f"📊 挂机：今日已赚 {c(afk.get('earnedTodayCents'))} / 上限 {c(afk.get('dailyCapCents'))} bolts"
        f"｜{c(rate)}/分钟"
        f"｜剩 {c(rem)}（约需 {need // 60}h{need % 60:02d}m）"
        f"｜party×{afk.get('partyMultiplier')} 在线 {afk.get('activeCount')}")
    return nxt


MAX_SESSION_MIN = 300      # 单次 AFK 会话最长 5 小时，避开服务端隐式断连
RECONNECT_MAX = 5          # 断连后最多重连几次


def _afk_start(api, tid):
    """启动 AFK。409 = 已有会话，直接接管。返回 True 表示可以开始心跳。"""
    st, raw = api.afk(tid, "start")
    if st == 200:
        log(f"▶️ AFK start HTTP 200")
        return True
    if st == 409:
        log("ℹ️ 已有 AFK 会话在跑（409），直接接管心跳")
        return True
    log(f"❌ AFK start HTTP {st}: {raw[:200]}")
    return False


def do_afk(api, tid, minutes, remaining_cents=None):
    """挂机到指定分钟数。

    容错规则：
      · 心跳返回 400 "No active AFK connection" → 服务端把会话断了，
        不是我们的错，自动重新 start 接着挂（最多 RECONNECT_MAX 次）
      · 单次会话最多跑 MAX_SESSION_MIN 分钟，到点主动 stop + start，避免被服务端强杀
      · 额度跑满就提前收工
    """
    if remaining_cents is not None and remaining_cents <= 0:
        log("✅ 今日 AFK 额度已满，无需挂机")
        return True

    deadline = time.time() + minutes * 60
    reconnects = 0
    total_beats = 0

    while time.time() < deadline:
        if not _afk_start(api, tid):
            return False

        # 单个会话的结束时间：取 min(总 deadline, 会话上限)
        session_end = min(deadline, time.time() + MAX_SESSION_MIN * 60)
        beats = 0
        broke = False

        try:
            while time.time() < session_end:
                time.sleep(HEARTBEAT_SEC)
                beats += 1
                total_beats += 1
                st, raw = api.afk(tid, "heartbeat")
                if st == 400 and "No active AFK" in raw:
                    log(f"⚠️ 第 {beats} 次心跳被服务端断开（{raw[:60]}）——自动重连")
                    broke = True
                    break
                if st != 200:
                    log(f"❌ 第 {beats} 次心跳失败 HTTP {st}: {raw[:150]}")
                    return False
                if beats % 20 == 0:      # 每 10 分钟报一次
                    st2, d2 = api.rewards(tid)
                    if st2 == 200:
                        a = d2.get("afk", {})
                        log(f"  💓 {beats} 次心跳｜今日 {c(a.get('earnedTodayCents'))}"
                            f" / {c(a.get('dailyCapCents'))} bolts"
                            f"｜剩 {(deadline - time.time()) / 60:.0f} 分钟")
                        if (a.get("remainingTodayCents") or 0) <= 0:
                            log("  ✅ 今日 AFK 额度已满，提前收工")
                            return True
        finally:
            api.afk(tid, "stop")

        if broke:
            reconnects += 1
            if reconnects > RECONNECT_MAX:
                log(f"❌ 断连重连已达上限 {RECONNECT_MAX} 次，放弃本轮挂机")
                return False
            time.sleep(10)
            continue
        # 正常到达单会话上限或总 deadline
        if time.time() >= deadline:
            break
        log(f"🔄 单会话满 {MAX_SESSION_MIN} 分钟，主动轮换会话")

    st, d = api.rewards(tid)
    if st == 200:
        print_status(d)
    log(f"⏹️ 本轮挂机结束：共 {total_beats} 次心跳，重连 {reconnects} 次")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--claim", action="store_true")
    ap.add_argument("--claim-if-due", action="store_true")
    ap.add_argument("--claim-only", action="store_true",
                    help="只签到不挂机（高频 cron 用，10 秒内结束）")
    ap.add_argument("--afk", type=int, default=0, metavar="MINUTES")
    ap.add_argument("--all", type=int, default=0, metavar="MINUTES")
    ap.add_argument("--login-only", action="store_true")
    ap.add_argument("--verify", default="")
    a = ap.parse_args()

    api = Api()

    code = a.verify
    if code == "totp":
        sec = TOTP_SECRET or load_state().get("totp_secret")
        if not sec:
            log("❌ 没有 REVENACT_TOTP_SECRET")
            return 5
        code = totp(sec)
        log(f"🔑 用 TOTP 密钥算出当前码 {code}")

    if code or a.verify:
        if not api.token and not api.verify(code):
            return 5
        # 验证成功后继续走下面的流程
    elif a.login_only:
        return 0 if api.login() else 3
    elif not api.token:
        log("ℹ️ 没有可用 token，尝试登录")
        if not api.login():
            return 3

    tid = api.tenant_id()
    if not tid:
        return 4

    st, d = api.rewards(tid)
    if st != 200:
        log(f"❌ rewards HTTP {st}: {str(d)[:200]}")
        if st == 401:
            log("🔑 token 失效，需要重新登录 + 验证码")
            save_state(token=None)
            return 7
        return 4
    nxt = print_status(d)
    save_state(rewards=d, next_claim_at=nxt)

    if a.claim_only:
        day = d.get("daily", {})
        if not day.get("canClaim"):
            log(f"⏳ 还没到 14 小时：上次 {day.get('lastClaimISO')}"
                f"，下次 {nxt}（还差 {fmt_delta(nxt)}）—— 静默跳过")
            return 0
        st, raw = api.claim(tid)
        log(f"🎁 签到 HTTP {st}: {raw[:300]}")
        if st != 200:
            log("❌ 签到失败")
            return 6
        try:
            j = json.loads(raw)
            log(f"   ✅ +{c(j.get('totalRewardCents'))} bolts"
                f"（基础 {c(j.get('baseRewardCents'))} + 连击 {c(j.get('streakBonusCents'))}）"
                f"｜连击 {j.get('newStreak')} 天｜余额 {c(j.get('balanceCents'))}")
            save_state(last_claim_result=j)
        except Exception:  # noqa: BLE001
            pass
        st, d2 = api.rewards(tid)
        if st == 200:
            print_status(d2)
            save_state(rewards=d2, next_claim_at=next_claim_at(d2))
        return 0

    if a.status and not (a.claim or a.all or a.afk or a.claim_if_due):
        return 0

    if a.claim_if_due:
        day = d.get("daily", {})
        if not day.get("canClaim"):
            log(f"⏳ 未到签到时间：上次 {day.get('lastClaimISO')}"
                f"，下次 {nxt}（还差 {fmt_delta(nxt)}）—— 本次跳过")
            # 仍然要把挂机额度跑掉（这条 cron 也负责挂机）
            minutes = a.afk
            if minutes > 0:
                if not do_afk(api, tid, minutes, d.get("afk", {}).get("remainingTodayCents")):
                    return 6
            return 0

    if a.claim or a.all or a.claim_if_due:
        day = d.get("daily", {})
        if not day.get("canClaim"):
            log(f"🎁 今日已签到（{day.get('lastClaimISO')}），下次 {nxt}（还差 {fmt_delta(nxt)}），跳过")
        else:
            st, raw = api.claim(tid)
            log(f"🎁 签到 HTTP {st}: {raw[:300]}")
            if st != 200:
                log("❌ 签到失败 —— 明确失败，不吞")
                return 6
            try:
                j = json.loads(raw)
                log(f"   ✅ +{c(j.get('totalRewardCents'))} bolts（基础 {c(j.get('baseRewardCents'))}"
                    f" + 连击 {c(j.get('streakBonusCents'))}）｜连击 {j.get('newStreak')} 天"
                    f"｜余额 {c(j.get('balanceCents'))}")
            except Exception:  # noqa: BLE001
                pass

    minutes = a.afk or a.all
    if (a.claim or a.all or a.claim_if_due) and minutes <= 0:
        st, d2 = api.rewards(tid)
        if st == 200:
            print_status(d2)
            save_state(rewards=d2, next_claim_at=next_claim_at(d2))

    if minutes > 0:
        if not do_afk(api, tid, minutes, d.get("afk", {}).get("remainingTodayCents")):
            return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
