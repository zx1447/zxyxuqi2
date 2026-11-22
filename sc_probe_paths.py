#!/usr/bin/env python3
"""一次性探针：打印 AFK 配置全字段 + 日限额尝试。用完删。"""
import json, sys
import skycastle_afk as K

s = K.make_session()
st = K.load_state()
K.selftest(s)
if not K.restore_cookie(s, st):
    if not K.login(s, st):
        sys.exit(3)

code, body = K.call(s, "GET", "/api/user/billingafk/status")
d = (body or {}).get("data") or {}
print("\n=== /billingafk/status 全字段 ===", flush=True)
for k, v in d.items():
    if k == "javascript_injection":
        print(f"\n--- {k} ---", flush=True)
        print(v, flush=True)
        print("--- end ---\n", flush=True)
    else:
        print(f"{k} = {v!r}", flush=True)

code, body = K.call(s, "GET", "/api/user/billingafk/stats")
print("\n=== /stats ===", flush=True)
print(json.dumps((body or {}).get("data"), ensure_ascii=False), flush=True)

for p in ("/api/user/billingafk/limits", "/api/user/billingafk/daily-limits",
          "/api/user/billingafk/usage", "/api/user/billingafk/sessions"):
    code, body = K.call(s, "GET", p)
    print(f"{p} -> {code} {json.dumps(body, ensure_ascii=False)[:200] if isinstance(body,(dict,list)) else body}", flush=True)
