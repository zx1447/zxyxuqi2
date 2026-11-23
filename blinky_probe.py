#!/usr/bin/env python3
"""Baltic Host（blinky.baltichost.online）探针：登录 → 抓面板页面，找续期/启动接口。

登录流程（从站点前端 JS 还原）：
  1. GET /auth/login/            取页面里的 RSA 公钥
  2. POST /auth/validator        multipart: username + password(RSA PKCS1v1.5, base64)
     成功的判据：HTTP 200 且 body 含 "success"
出口：先直连；若被 403/1012（VPN/代理/数据中心 IP 拦截），
      用 BLINKY_VLESS_URL（家宽）拉 xray 兜底后重试。
输出：out/ 下保存各页面 HTML 与站点 JS，供分析（artifact）。不打印密码/cookie。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://blinky.baltichost.online"
USER = os.environ.get("BLINKY_USER", "").strip()
PASS = os.environ.get("BLINKY_PASS", "")
VLESS = os.environ.get("BLINKY_VLESS_URL", "").strip()
XRAY = os.path.expanduser("~/.xray/xray")
OUT = "out"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


def save(name, text):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def start_xray():
    """拉起 xray（SOCKS 127.0.0.1:40000），复用 nodaroo 的 VLESS 解析方式。"""
    if not VLESS or not os.path.exists(XRAY):
        log("⚠️ 无家宽兜底（缺 BLINKY_VLESS_URL 或 xray）")
        return None
    u = urllib.parse.urlparse(VLESS)
    q = urllib.parse.parse_qs(u.query)
    outbound = {
        "protocol": "vless",
        "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443,
                                "users": [{"id": u.username, "encryption": "none"}]}]},
        "streamSettings": {
            "network": q.get("type", ["ws"])[0],
            "security": q.get("security", ["tls"])[0],
            "tlsSettings": {"serverName": q.get("sni", [u.hostname])[0],
                            "fingerprint": q.get("fp", ["chrome"])[0]},
            "wsSettings": {"path": q.get("path", ["/"])[0]},
        },
    }
    conf = {"log": {"loglevel": "warning"},
            "inbounds": [{"port": 40000, "listen": "127.0.0.1", "protocol": "socks",
                          "settings": {"udp": False}}],
            "outbounds": [outbound]}
    with open("/tmp/blinky_xray.json", "w", encoding="utf-8") as fh:
        json.dump(conf, fh)
    subprocess.Popen([XRAY, "run", "-c", "/tmp/blinky_xray.json"],
                     stdout=subprocess.DEVNULL,
                     stderr=open("/tmp/blinky_xray.log", "wb"), start_new_session=True)
    time.sleep(3)
    proxy = "socks5h://127.0.0.1:40000"
    log("🛡️ 家宽 VLESS 出口已拉起")
    return {"http": proxy, "https": proxy}


def session(proxies=None):
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if proxies:
        s.proxies.update(proxies)
    return s


def login(s):
    """返回 (ok, 说明)。"""
    r = s.get(f"{BASE}/auth/login/", timeout=30)
    if r.status_code != 200:
        return False, f"登录页 HTTP {r.status_code}"
    m = re.search(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", r.text, re.S)
    if not m:
        return False, "登录页里没找到 RSA 公钥"
    key = serialization.load_pem_public_key(m.group(0).encode())
    enc = base64.b64encode(key.encrypt(PASS.encode(), padding.PKCS1v15())).decode()
    r2 = s.post(f"{BASE}/auth/validator",
                files={"username": (None, USER), "password": (None, enc)},
                headers={"Referer": f"{BASE}/auth/login/", "X-Requested-With": "XMLHttpRequest"},
                timeout=30, allow_redirects=False)
    body = r2.text or ""
    if r2.status_code == 200 and "success" in body:
        return True, "登录成功"
    flat = re.sub(r"<[^>]+>|\s+", " ", re.sub(r"<style.*?</style>|<script.*?</script>", "", body, flags=re.S))
    return False, f"HTTP {r2.status_code}: {flat.strip()[:160]}"


def blocked(msg):
    return "403" in msg or "1012" in msg or "VPN" in msg


def dump_panel(s):
    pages = {"dashboard.html": f"{BASE}/"}
    r = s.get(f"{BASE}/", timeout=30, allow_redirects=True)
    save("dashboard.html", r.text)
    log(f"dashboard HTTP {r.status_code} len={len(r.text)} final={r.url}")
    # 站点同源 JS：列出来并保存，方便找 renew/start 接口
    js = re.findall(r'src="(/[^"]+\.js[^"]*)"', r.text)
    for path in sorted(set(js)):
        jr = s.get(BASE + path, timeout=30)
        fname = "js_" + re.sub(r"[^A-Za-z0-9_.-]", "_", path.strip("/"))[:120]
        save(fname, jr.text)
    # 服务器相关页面：从 dashboard 的链接里挑
    links = sorted(set(re.findall(r'href="(/[^"#]*)"', r.text)))
    log("面板内链接: " + ", ".join(links[:60]))
    save("links.txt", "\n".join(links))
    for path in links:
        if any(k in path for k in ("server", "renew", "free", "billing", "dashboard")):
            pr = s.get(BASE + path, timeout=30)
            save("page_" + re.sub(r"[^A-Za-z0-9_.-]", "_", path.strip("/"))[:120] + ".html", pr.text)
            log(f"  {path} HTTP {pr.status_code} len={len(pr.text)}")
            time.sleep(1)


def main():
    if not USER or not PASS:
        log("❌ 缺 BLINKY_USER / BLINKY_PASS")
        return 3
    log(f"🎯 Baltic Host 探针 · {BASE}")
    s = session()
    ok, msg = login(s)
    log(f"直连登录: {'✅' if ok else '❌'} {msg}")
    if not ok and blocked(msg):
        proxies = start_xray()
        if proxies:
            s = session(proxies)
            ok, msg = login(s)
            log(f"家宽登录: {'✅' if ok else '❌'} {msg}")
    if not ok:
        return 2
    dump_panel(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
