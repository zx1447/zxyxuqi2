#!/usr/bin/env python3
"""BotHoster（bothoster.top）保活服务 —— 纯 HTTP，无浏览器。

三个能力：
  1. status()      读 /dashboard/servers 的 x-data（expiresAtTimestamp / remainingSeconds /
                   status / isSuspended）+ navbar-balance，判断「要不要续 / 要不要启动」
  2. afk(min)      在 /dashboard/afk 上按 60s 心跳领币（+0.25/次，dailyMax=10 → 10 coins/天）
  3. keepalive()   余额够就按小时预付续期（1.0000 Coins/h），停机的就拉起来；
                   一切以「到期时间 / 余额 / 运行状态」三个真实读数为准，不看 HTTP 200

计费真相（2026-11-25 实盘核对 /dashboard/transactions）：
  注册 +5.00 → PROVISION 首小时预付 -1.00 → 之后**只按整点扣费**（09:01 这个整点都没扣，
  说明扣费滞后或按满小时结算）；余额只增不减的那几个小时 = 站点侧扣费未跑满。
  所以本服务不假设扣费准点，只要 remainingSeconds < 阈值 就续 1 小时，续完再读一次核对。

环境变量：
  BOTHOSTER_DISCORD_TOKEN  必填（Discord user token）
  BH_MODE                  service(默认) / afk / status / renew / probe
  BH_SERVER_ID             默认 13
  BH_RUN_MINUTES           afk / service 单轮挂机分钟，默认 50
  BH_RENEW_BELOW_HOURS     remaining 低于这个小时数才续，默认 6
  BH_RENEW_HOURS           每次续几小时（连续 renewServer 调用次数），默认 2
  BH_RESERVE_COINS         余额保留线，低于它就少续，默认 1
  BH_HEARTBEAT_SECONDS     心跳间隔，默认 60（不得小于服务端 intervalSeconds）
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

B = "https://bothoster.top"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TOKEN = os.environ.get("BOTHOSTER_DISCORD_TOKEN", "").strip()
MODE = (os.environ.get("BH_MODE", "service") or "service").strip().lower()
SERVER_ID = int(os.environ.get("BH_SERVER_ID", "13") or 13)
RUN_MIN = min(10.0, max(0.0, float(os.environ.get("BH_RUN_MINUTES", "10") or 10)))
HB_SEC = int(os.environ.get("BH_HEARTBEAT_SECONDS", "60") or 60)
RENEW_BELOW_H = float(os.environ.get("BH_RENEW_BELOW_HOURS", "6") or 6)
RENEW_HOURS = int(os.environ.get("BH_RENEW_HOURS", "2") or 2)
RESERVE = float(os.environ.get("BH_RESERVE_COINS", "1") or 1)


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


class NR(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向（OAuth 需要手动补 state）。"""
    def redirect_request(self, *a, **k):
        return None


class BH:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        proxy = os.environ.get("BH_PROXY", "").strip()
        handlers = [urllib.request.HTTPCookieProcessor(self.cj), NR]
        if proxy:
            handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            log("使用订阅代理")
        self.op = urllib.request.build_opener(*handlers)
        self.csrf = None
        self.lw_url = None
        self.snapshot = None
        self.info = {}
        self.balance = None
        self.srv_snapshot = None

    # ---------------------------------------------------------------- core
    def req(self, url, method="GET", data=None, headers=None):
        h = {"User-Agent": UA, "Accept": "text/html,application/json"}
        if headers:
            h.update(headers)
        if isinstance(data, str):
            body = data.encode()
        elif data is not None:
            body = json.dumps(data).encode()
        else:
            body = None
        try:
            r = self.op.open(urllib.request.Request(url, data=body, method=method, headers=h), timeout=40)
            return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, {}, f"{type(e).__name__}: {e}"

    def login(self):
        if not TOKEN:
            raise RuntimeError("缺 BOTHOSTER_DISCORD_TOKEN")
        st, hd, _ = self.req(B + "/auth/discord/redirect", headers={"Referer": B + "/auth/login"})
        loc = hd.get("Location") or ""
        m_cid = re.search(r"client_id=(\d+)", loc)
        m_state = re.search(r"state=([^&]+)", loc)
        m_red = re.search(r"redirect_uri=([^&]+)", loc)
        if not (m_cid and m_state and m_red):
            raise RuntimeError(f"OAuth 起始失败 st={st} loc={loc[:100]}")
        q = urllib.parse.urlencode({"client_id": m_cid.group(1),
                                    "redirect_uri": urllib.parse.unquote(m_red.group(1)),
                                    "response_type": "code", "scope": "identify email guilds.join"})
        st, _, b = self.req(f"https://discord.com/api/v9/oauth2/authorize?{q}", "POST",
                            {"permissions": "0", "authorize": True, "integration_type": 0,
                             "guild_id": None, "channel_id": None, "captcha_key": None},
                            {"Authorization": TOKEN, "Content-Type": "application/json",
                             "Origin": "https://discord.com"})
        if st != 200:
            raise RuntimeError(f"Discord authorize {st}: {b[:200]}")
        cb = json.loads(b)["location"]
        self.req(cb + ("&" if "?" in cb else "?") + "state=" + m_state.group(1))
        if not any(c.name == "bothoster-session" for c in self.cj):
            raise RuntimeError("登录没拿到 bothoster-session")
        log("✅ 登录成功（Discord OAuth）")

    def _snapshots(self, html):
        """返回 {component_name: (raw_snapshot, data)}"""
        out = {}
        for mm in re.finditer(r'wire:snapshot="([^"]+)"', html):
            raw = mm.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'")
            try:
                d = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            out[d.get("memo", {}).get("name")] = (raw, d.get("data", {}))
        return out

    def _lw(self, page, snapshot, method, params, extra_head=None):
        hd = {"Content-Type": "application/json", "X-Livewire": "true",
              "X-CSRF-TOKEN": self.csrf, "Referer": B + page, "Origin": B}
        if extra_head:
            hd.update(extra_head)
        payload = {"_token": self.csrf,
                   "components": [{"snapshot": snapshot, "updates": {},
                                   "calls": [{"path": "", "method": method, "params": params}]}]}
        st, _, body = self.req(self.lw_url, "POST", payload, hd)
        if st != 200:
            return st, None, None
        try:
            comp = json.loads(body)["components"][0]
            new_snap = comp["snapshot"]
            return st, new_snap, json.loads(new_snap)["data"]
        except Exception:  # noqa: BLE001
            return st, None, None

    # ---------------------------------------------------------------- 服务器
    def status(self):
        st, _, h = self.req(B + "/dashboard/servers")
        if st != 200:
            raise RuntimeError(f"服务器页 {st}: {h[:120]}")
        snaps = self._snapshots(h)
        m = re.search(r'name="csrf-token" content="([^"]+)"', h)
        if m:
            self.csrf = m.group(1)
        m = re.search(r'data-update-uri="([^"]+)"', h)
        if m:
            self.lw_url = m.group(1)
        if "server-list" in snaps:
            self.srv_snapshot = snaps["server-list"][0]
        if "navbar-balance" in snaps:
            self.balance = snaps["navbar-balance"][1].get("credits")
        g = lambda p, d=None: (re.search(p, h).group(1) if re.search(p, h) else d)  # noqa: E731
        sid = g(r"serverTelemetry\(\{ serverId: (\d+)")
        # 只取我们关心的那台服务器卡片的读数
        exp = g(r"expiresAtTimestamp: (\d+)")
        rem = g(r"remainingSeconds: (\d+)")
        srv = {
            "serverId": int(sid) if sid else SERVER_ID,
            "status": g(r"status: '([a-z_]+)'"),
            "suspended": g(r"isSuspended: (true|false)") == "true",
            "expiresAt": int(exp) if exp else None,
            "remaining_s": int(rem) if rem else None,
            "isLowRunway": g(r"isLowRunway: (true|false)") == "true",
            "isCriticalRunway": g(r"isCriticalRunway: (true|false)") == "true",
            "balance": self.balance,
        }
        if srv["expiresAt"]:
            srv["expiresAtHuman"] = time.strftime(
                "%Y-%m-%d %H:%M UTC", time.gmtime(srv["expiresAt"] / 1000))
        return srv

    def power(self, action="start", sid=None):
        """action: start / restart / stop"""
        sid = sid or SERVER_ID
        if not self.srv_snapshot:
            self.status()
        st, snap, data = self._lw("/dashboard/servers", self.srv_snapshot,
                                  "powerAction", [sid, action])
        msg = (data or {}).get("actionMessage") or (data or {}).get("errorMessage")
        log(f"  powerAction({sid},'{action}') → {st} {msg}")
        return st, msg

    def renew(self, sid=None, hours=None):
        """按小时预付续期。返回 (成功续的小时数, 消息)。以余额变化 + 到期时间核对。"""
        sid = sid or SERVER_ID
        hours = hours or RENEW_HOURS
        done, msgs = 0, []
        for i in range(hours):
            before = self.status()
            if before["balance"] is not None and float(before["balance"]) < 1 + RESERVE:
                msgs.append(f"余额 {before['balance']} 不足（保留线 {RESERVE}），停手")
                break
            if not self.srv_snapshot:
                self.status()
            st, snap, data = self._lw("/dashboard/servers", self.srv_snapshot, "renewServer", [sid])
            msg = (data or {}).get("actionMessage") or (data or {}).get("errorMessage") or ""
            after = self.status()
            gained = 0
            if before["expiresAt"] and after["expiresAt"]:
                gained = (after["expiresAt"] - before["expiresAt"]) / 1000 / 3600
            msgs.append(f"#{i+1} {st} {msg} | expires +{gained:.2f}h | 余额 {before['balance']}→{after['balance']}")
            log("  " + msgs[-1])
            if gained > 0.5:
                done += 1
            elif "suspended" in msg.lower():
                msgs.append("服务端只允许对『已挂起』的机器续期；正常运行的机器按小时自动结算，"
                            "不需要也不能手动续 → 只需保证余额 ≥1")
                break
            else:
                break
        return done, "; ".join(msgs)

    # ---------------------------------------------------------------- AFK
    def load_afk(self):
        st, _, h = self.req(B + "/dashboard/afk")
        if st != 200:
            raise RuntimeError(f"AFK 页面 {st}")
        m = re.search(r'name="csrf-token" content="([^"]+)"', h)
        self.csrf = m.group(1) if m else self.csrf
        m = re.search(r'data-update-uri="([^"]+)"', h)
        self.lw_url = m.group(1) if m else self.lw_url
        snaps = self._snapshots(h)
        if "afk-earner" not in snaps:
            raise RuntimeError("没找到 afk-earner 组件")
        self.snapshot, self.info = snaps["afk-earner"]
        if "navbar-balance" in snaps:
            self.balance = snaps["navbar-balance"][1].get("credits")
        log(f"AFK 就绪 · 余额 {self.balance} · 今日 {self.info.get('earnedToday')}"
            f"/{self.info.get('dailyMax')} · {self.info.get('coinsPerInterval')}"
            f" coins/{self.info.get('intervalSeconds')}s")
        return self.info

    def heartbeat(self):
        tok = self.info.get("sessionToken")
        st, snap, data = self._lw("/dashboard/afk", self.snapshot, "heartbeat", [tok])
        if snap:
            self.snapshot, self.info = snap, data
        return st, self.info

    def afk(self, minutes):
        self.load_afk()
        if (self.info.get("earnedToday") or 0) >= (self.info.get("dailyMax") or 10):
            log("今日 AFK 已满，跳过")
            return 0.0
        t0, got = time.time(), 0.0
        while time.time() - t0 < minutes * 60:
            time.sleep(HB_SEC)
            st, info = self.heartbeat()
            if st != 200:
                log(f"  heartbeat {st}")
                if st in (0, 502, 503, 504):
                    continue
                break
            if not info:
                continue
            msg = info.get("lastClaimMessage") or ""
            if msg.startswith("+"):
                got += info.get("coinsPerInterval") or 0
            log(f"  t+{time.time()-t0:.0f}s · 今日 {info.get('earnedToday')}/{info.get('dailyMax')} · {msg}")
            if "limit" in msg.lower() or "maximum" in msg.lower():
                log("  到日上限，收工")
                break
        return got


# -------------------------------------------------------------------- 保活决策
def keepalive(bh):
    """服务端仅允许对挂起服务器续期；尽力续期后，无论成功与否都尝试启动。"""
    s = bh.status()
    log(f"服务器 #{s['serverId']} · {s['status']} · suspended={s['suspended']} · 余额 {s['balance']}")
    if s["suspended"]:
        bal = float(s["balance"] or 0)
        if bal >= 1 + RESERVE:
            log("服务器已挂起，尝试续期")
            bh.renew(s["serverId"])
            s = bh.status()
        else:
            log("⚠️ 余额不足以续期，仍尝试启动")
    if s["status"] not in ("running", "starting", "installing") or s["suspended"]:
        bh.power("start", s["serverId"])
        time.sleep(8)
        s = bh.status()
        log(f"启动后：{s['status']} · suspended={s['suspended']}")
    return s


def main():
    bh = BH()
    try:
        bh.login()
        if MODE == "probe":
            info = bh.load_afk()
            safe = {k: info.get(k) for k in ("earnedToday", "dailyMax", "coinsPerInterval", "intervalSeconds")}
            print(json.dumps({"server": bh.status(), "afk": safe}, indent=1, ensure_ascii=False))
            return 0
        if MODE == "status":
            print(json.dumps(bh.status(), indent=1, ensure_ascii=False))
            return 0
        if MODE == "renew":
            print(bh.renew())
            print(json.dumps(bh.status(), indent=1, ensure_ascii=False))
            return 0
        if MODE == "afk":
            got = bh.afk(RUN_MIN)
            log(f"AFK 结束 · 本轮 +{got:.2f} · 今日 {bh.info.get('earnedToday')}/{bh.info.get('dailyMax')}")
            if got <= 0 and (bh.info.get("earnedToday") or 0) < (bh.info.get("dailyMax") or 10):
                log("❌ 未确认 AFK 入账")
                return 2
            return 0
        # 先续期/启动；只要未达到日限额，就按用户要求挂机，不按余额跳过。
        s = keepalive(bh)
        got = bh.afk(RUN_MIN)
        log(f"AFK 补币 +{got:.2f} · 今日 {bh.info.get('earnedToday')}/{bh.info.get('dailyMax')}")
        if got <= 0 and (bh.info.get("earnedToday") or 0) < (bh.info.get("dailyMax") or 10):
            log("❌ 未确认 AFK 入账")
            return 2
        if s["status"] not in ("running", "starting", "installing") or s["suspended"]:
            s = keepalive(bh)
        if s["status"] not in ("running", "starting", "installing") or s["suspended"]:
            log("❌ 无法确认服务器已启动")
            return 2
        return 0
    except Exception as e:  # noqa: BLE001
        log(f"❌ {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
