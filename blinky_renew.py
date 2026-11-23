#!/usr/bin/env python3
"""Baltic Host 免费服续期（blinky）。

站点规则（2026-11-23 实测面板）：
  · GET  /manage/server/api/{短ID}/renewal   → {"data": {can_renew, days_until_expiration,
                                                     expiration_date, server_status, ...}}
  · POST /manage/server/api/{短ID}/renewal/renew  （表单 csrf_token；过期后同一接口=Reactivate）
  · 续期窗口：剩余 ≤3 天才 can_renew=true（页面文案「续期在 N 天后可用」）
  · 启动   POST /manage/server/api/{完整UUID}/power  body {"action":"start"}（X-CSRF-Token）

流程：登录 → 读 renewal → 仅 can_renew=true 才续期 → 以「到期日是否变晚」核对（不以 HTTP 200 为准）
      → 续期成功后检查运行状态，未运行则 start → 核对状态。
不在窗口：只打印剩余天数，直接退出（不发任何写请求）。
出口：直连被 403/1012 拦时，用 BLINKY_VLESS_URL 家宽出口兜底（与 blinky_probe.py 相同逻辑）。
"""
import os
import re
import sys
import time

import requests

import blinky_probe as bp

SHORT = os.environ.get("BLINKY_SHORT_ID", "f766593e")
FULL = os.environ.get("BLINKY_SERVER_UUID", "f766593e-30c4-4f34-9f53-5337f0e3db33")


def xhdr(s, referer):
    token = ""
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"',
                  s.get(f"{bp.BASE}{referer}", timeout=30).text)
    if m:
        token = m.group(1)
    return {"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json", "Referer": f"{bp.BASE}{referer}"}


def read_renewal(s):
    h = xhdr(s, f"/manage/server/{SHORT}/")
    r = s.get(f"{bp.BASE}/manage/server/api/{SHORT}/renewal", headers=h, timeout=30)
    d = r.json().get("data", {}) if r.status_code == 200 else {}
    return d


def state_of(s):
    r = s.get(f"{bp.BASE}/api/dashboard-data?all=true", timeout=30)
    for srv in (r.json().get("all_servers") or []) if r.status_code == 200 else []:
        if (srv.get("uuid_short") or "").startswith(SHORT):
            return srv.get("status") or "?", bool(srv.get("is_suspended"))
    return "?", False


def main():
    s = bp.session()
    ok, msg = bp.login(s)
    if not ok and bp.blocked(msg):
        proxies = bp.start_xray()
        if proxies:
            s = bp.session(proxies)
            ok, msg = bp.login(s)
    print(f"登录: {'✅' if ok else '❌'} {msg[:120]}", flush=True)
    if not ok:
        return 2

    before = read_renewal(s)
    print(f"续期状态: can_renew={before.get('can_renew')} 剩余 {before.get('days_until_expiration')} 天"
          f" 到期 {before.get('expiration_date')} 服务器状态 {before.get('server_status')}", flush=True)
    if not before.get("can_renew"):
        print("⏳ 未到续期窗口（站点 can_renew=false），本轮不发写请求", flush=True)
        return 0

    old_exp = before.get("expiration_date")
    h = xhdr(s, f"/manage/server/{SHORT}/renewal")
    r = s.post(f"{bp.BASE}/manage/server/api/{SHORT}/renewal/renew",
               data={"csrf_token": h["X-CSRF-Token"]}, headers=h, timeout=60,
               allow_redirects=False)
    print(f"续期请求: HTTP {r.status_code} {r.text[:160]!r}", flush=True)
    time.sleep(3)
    after = read_renewal(s)
    print(f"续期后到期: {old_exp} → {after.get('expiration_date')} "
          f"(剩余 {after.get('days_until_expiration')} 天)", flush=True)
    if after.get("expiration_date") == old_exp:
        print("❌ 到期日未变化，续期未生效", flush=True)
        return 2
    print("🚀 续期已生效", flush=True)

    status, suspended = state_of(s)
    print(f"运行状态: {status} suspended={suspended}", flush=True)
    if status in ("running", "starting"):
        print("✅ 已在运行，无需启动", flush=True)
        return 0
    hp = xhdr(s, f"/manage/server/{SHORT}/")
    r = s.post(f"{bp.BASE}/manage/server/api/{FULL}/power", json={"action": "start"},
               headers=hp, timeout=60)
    print(f"启动请求: HTTP {r.status_code} {r.text[:120]!r}", flush=True)
    for _ in range(10):
        time.sleep(10)
        status, _ = state_of(s)
        if status in ("running", "starting"):
            print(f"✅ 启动成功: {status}", flush=True)
            return 0
    print(f"❌ 启动后仍为 {status}", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
