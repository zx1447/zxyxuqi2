#!/usr/bin/env python3
"""GemHost.pl 免费服守护：停了就启动（只做 start，不做 restart）。

已实测的接口：
  POST {BASE}/api/auth/login  {"email","password"}  → Cookie gemhost_access / gemhost_csrf
  GET  {BASE}/api/servers/list                      → servers[{id,name}]
  GET  {BASE}/api/servers/<id>/status               → status, is_suspended, free_session{ends_at,remaining_ms}
  POST {BASE}/api/servers/<id>/power {"action":"start"}   （变更请求需带 X-CSRF-Token）

规则：
  · status 为 running / starting → 不动
  · stopping / installing / restoring → 本轮放过
  · is_suspended → 只报告，不动
  · 其余（offline 等）→ 发 start，并复查
  · 不使用 restart（实测会让服务器离线且不自动恢复）

环境变量：GEMHOST_EMAIL、GEMHOST_PASSWORD（必填，只从 secrets 读取，不写文件）
退出码：0 正常；2 有服务器启动失败；3 缺凭据或登录失败
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("GEMHOST_BASE", "https://panel.gemhost.pl").rstrip("/")
EMAIL = os.environ.get("GEMHOST_EMAIL", "").strip()
PASSWORD = os.environ.get("GEMHOST_PASSWORD", "").strip()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

OK_STATES = {"running", "starting"}
SKIP_STATES = {"stopping", "installing", "restoring"}


def log(msg):
    print(msg, flush=True)


class Session:
    def __init__(self):
        self.cookies = {}

    def _absorb(self, resp):
        hdrs = getattr(resp, "headers", None)
        if not hdrs:
            return
        for line in (hdrs.get_all("Set-Cookie") or []):
            name, _, rest = line.partition("=")
            value = rest.split(";")[0].strip()
            if name.strip() and value:
                self.cookies[name.strip()] = value

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/",
        })
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        if method != "GET" and self.cookies.get("gemhost_csrf"):
            req.add_header("X-CSRF-Token", self.cookies["gemhost_csrf"])
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._absorb(resp)
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            self._absorb(e)
            raw = e.read().decode("utf-8", "replace")
            try:
                d = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                d = {}
            d.setdefault("http_error", e.code)
            return d


def login(sess):
    d = sess.request("/api/auth/login", method="POST",
                     body={"email": EMAIL, "password": PASSWORD})
    if "gemhost_access" not in sess.cookies:
        log(f"❌ 登录失败: {str(d)[:200]}")
        return False
    log("✅ 登录成功")
    return True


def check_once(sess):
    """返回 (有失败, 最小剩余分钟)。"""
    lst = sess.request("/api/servers/list")
    servers = lst.get("servers") or []
    if not servers:
        if not login(sess):
            return None, None
        lst = sess.request("/api/servers/list")
        servers = lst.get("servers") or []
    failed = False
    min_remain = None
    for s in servers:
        sid, name = s["id"], s.get("name", "?")
        stt = sess.request(f"/api/servers/{sid}/status")
        if stt.get("http_error") == 401 or "status" not in stt:
            log("会话失效，重新登录")
            if not login(sess):
                return None, None
            stt = sess.request(f"/api/servers/{sid}/status")
        status = (stt.get("status") or "?").lower()
        fs = stt.get("free_session") or {}
        rem = fs.get("remaining_ms")
        rem_min = round(rem / 60000, 1) if isinstance(rem, (int, float)) else None
        if rem_min is not None and (min_remain is None or rem_min < min_remain):
            min_remain = rem_min
        log(f"-- [{sid}] {name} | status={status} | suspended={stt.get('is_suspended')} "
            f"| 会话至 {fs.get('ends_at', '-')} | 剩余 {rem_min}min")
        if status in OK_STATES:
            continue
        if status in SKIP_STATES:
            log("   过渡状态，本轮放过")
            continue
        if stt.get("is_suspended"):
            log("   ⚠️ 已挂起，只报告，不处理")
            failed = True
            continue
        log("   ▶ 已停止，发送 start")
        r = sess.request(f"/api/servers/{sid}/power", method="POST", body={"action": "start"})
        log(f"   start 返回: {str(r)[:160]}")
        time.sleep(20)
        stt2 = sess.request(f"/api/servers/{sid}/status")
        st2 = (stt2.get("status") or "?").lower()
        log(f"   复查: status={st2}")
        if st2 not in OK_STATES:
            failed = True
    return failed, min_remain


def main():
    if not EMAIL or not PASSWORD:
        log("❌ 缺 GEMHOST_EMAIL / GEMHOST_PASSWORD")
        return 3
    sess = Session()
    if not login(sess):
        return 3
    failed, _ = check_once(sess)
    if failed is None:
        return 3
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
