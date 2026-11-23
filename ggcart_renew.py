#!/usr/bin/env python3
"""GGCart 免费服精准续期（Community Free，2 GB，7 天周期）。纯 HTTP，不用浏览器。

规则（站点公告）：续期只在当前周期最后 48 小时内开放。
接口（2026-11-23 实测抓包）：
  · 登录   Supabase  POST {SB}/auth/v1/token?grant_type=password   → access_token
  · 读状态 GET  /_serverFn/<overview>   (Authorization: Bearer)     → claims（seroval 格式，本脚本解码）
  · 续期   POST /_serverFn/<renew>      body = seroval {claimId}     → 窗口内才会真正续期

流程：登录 → 读 community 且 active 的 claim 及到期时间（UTC）
      剩余 > 48h：只打印，退出 0（不发写请求）
      剩余 ≤ 48h：POST 续期 → 重新读取，到期时间变晚才算成功。
环境变量：GG_EMAIL、GG_PASS（不写进代码）。
退出码：0 正常（含窗口外）；2 续期失败；3 缺凭据。
"""
import datetime as dt
import json
import os
import sys

import requests

SB = "https://fmiyossrwgihzcpeiafe.supabase.co"
SB_KEY = "sb_publishable_jRmm-1XvgWEZOXleZDjbjw_jiFitz8A"  # 站点前端公开的 publishable key
SITE = "https://ggcart.in"
OVERVIEW_ID = "f95795c7d74a7bb099d37beea648bf37f8f2115b15dacf0af466f7bb01e461f8"
RENEW_ID = "bd5fdbcf4546f797645ac0d14c46ca3fb57957e3786c184f1be0a31dc479f3cf"
WINDOW = dt.timedelta(hours=48)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


def login(email, pwd):
    r = requests.post(f"{SB}/auth/v1/token?grant_type=password",
                      headers={"apikey": SB_KEY, "Content-Type": "application/json", "User-Agent": UA},
                      json={"email": email, "password": pwd}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"登录失败 HTTP {r.status_code}")
    return r.json()["access_token"]


def hdrs(token):
    return {"Authorization": f"Bearer {token}", "x-tsr-serverfn": "true", "User-Agent": UA,
            "Accept": "application/x-tss-framed, application/x-ndjson, application/json",
            "Origin": SITE, "Referer": f"{SITE}/dashboard/discord"}


def dec(n):
    """解码 seroval JSON 节点（只覆盖本站用到的类型）。"""
    t = n.get("t")
    if t == 10:
        p = n["p"]
        return {k: dec(v) for k, v in zip(p["k"], p["v"])}
    if t == 9:
        return [dec(x) for x in n["a"]]
    if t in (0, 1):
        return n["s"]
    if t == 2:
        return {2: True, 3: False}.get(n["s"])
    return None


def root_of(body):
    d = json.loads(body)
    if isinstance(d, dict) and isinstance(d.get("t"), dict):
        d = d["t"]
    return dec(d)


def read_community(token):
    """返回 (claim_id, expires_at) 或 (None, None)。"""
    r = requests.get(f"{SITE}/_serverFn/{OVERVIEW_ID}", headers=hdrs(token), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"读取状态失败 HTTP {r.status_code}")
    data = root_of(r.text)
    claims = (data or {}).get("result", {}).get("claims") or []
    for c in claims:
        if c.get("kind") == "community" and c.get("status") == "active":
            exp = dt.datetime.fromisoformat(c["expires_at"].replace("Z", "+00:00"))
            return c["id"], exp
    return None, None


def renew_body(claim_id):
    return json.dumps({"t": {"t": 10, "i": 0, "p": {"k": ["data"], "v": [
        {"t": 10, "i": 1, "p": {"k": ["claimId"], "v": [{"t": 1, "s": claim_id}]}, "o": 0}]}, "o": 0},
        "f": 127, "m": []})


def post_renew(token, claim_id):
    h = hdrs(token)
    h["Content-Type"] = "application/json"
    r = requests.post(f"{SITE}/_serverFn/{RENEW_ID}", headers=h, data=renew_body(claim_id), timeout=60)
    return r.status_code, r.text


def main():
    email = os.environ.get("GG_EMAIL", "").strip()
    pwd = os.environ.get("GG_PASS", "")
    if not email or not pwd:
        log("❌ 缺 GG_EMAIL / GG_PASS")
        return 3
    token = login(email, pwd)
    claim_id, exp = read_community(token)
    if not claim_id:
        log("❌ 没有 active 的 Community Free claim")
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    remain = exp - now
    log(f"Community Free 到期 {exp:%Y-%m-%d %H:%M:%S} UTC，剩余 {remain}")
    if remain > WINDOW:
        log(f"⏳ 未进入续期窗口（窗口开放于 {exp - WINDOW:%Y-%m-%d %H:%M} UTC），不发续期请求")
        return 0
    if remain <= dt.timedelta(0):
        log("❌ 已过期：本脚本不处理重新激活")
        return 2

    log("🔁 进入续期窗口，发送续期请求")
    code, text = post_renew(token, claim_id)
    log(f"续期请求 HTTP {code}: {text[:200]}")
    new_id, new_exp = read_community(token)
    if new_exp and new_exp > exp:
        log(f"✅ 续期成功：{exp:%Y-%m-%d %H:%M} → {new_exp:%Y-%m-%d %H:%M} UTC")
        return 0
    log("❌ 续期未生效（到期时间未变）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
