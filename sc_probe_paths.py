#!/usr/bin/env python3
"""一次性探针：枚举 billingafk 相关路径 + 读日限额。用完可删。"""
import json, os, sys, time, urllib.parse
import skycastle_afk as K

s = K.make_session()
st = K.load_state()
K.selftest(s)
if not K.restore_cookie(s, st):
    if not K.login(s, st):
        sys.exit(3)

paths = [
    ("GET",  "/api/user/billingafk"),
    ("GET",  "/api/user/billingafk/status"),
    ("GET",  "/api/user/billingafk/stats"),
    ("GET",  "/api/user/billingafk/limits"),
    ("GET",  "/api/user/billingafk/session"),
    ("GET",  "/api/user/billingafk/daily"),
    ("GET",  "/api/user/billingresourcesnewservers"),
    ("GET",  "/api/user/billing"),
    ("GET",  "/api/user/billing/limits"),
    ("GET",  "/api/user/credits"),
    ("GET",  "/api/user/daily-limits"),
    ("POST", "/api/user/billingafk/stop"),
    ("POST", "/api/user/billingresourcesnewservers/renew"),
    ("POST", "/api/user/billingresourcesnewservers/531/renew"),
]
for m, p in paths:
    code, body = K.call(s, m, p, {} if m == "POST" else None)
    t = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
    print(f"{m:5} {p:52} {code}  {t[:260]}", flush=True)
    time.sleep(1)
