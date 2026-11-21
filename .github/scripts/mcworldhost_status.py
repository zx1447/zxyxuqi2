"""读 mcworldhost 当前状态：余额 / 运行时间到期 / 是否停用。"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in
BASE="https://mcworldhost.de"
def log(m): print(m, flush=True)
def main():
    pw,browser,page=open_session(BASE,wait_seconds=150)
    try:
        sign_in(page,BASE,os.environ["MCWH_EMAIL"],os.environ["MCWH_PASSWORD"],timeout=120000)
        out={}
        dash=page.evaluate("async()=>(await(await fetch('/dashboard')).text())")
        for kw in ["runtimeExpiresAt","isSuspended","serverName","serverId"]:
            m=re.search(r'\\?"'+kw+r'\\?":\s*\\?"?([^"\\,}]+)',dash)
            out[kw]=m.group(1) if m else None
        coins=page.evaluate("async()=>(await(await fetch('/dashboard/coins')).text())")
        m=re.search(r'\\?"balance\\?":\s*(\d+)',coins); out["balance"]=int(m.group(1)) if m else None
        m=re.search(r'\\?"claimedToday\\?":\s*(true|false)',coins); out["claimed"]=m.group(1) if m else None
        afk=page.evaluate("async()=>(await(await fetch('/dashboard/afk')).text())")
        for kw in ["earnedToday","maxEarnPerDay","dayLimitReached","earnPerHour","maxEarnPerHour"]:
            m=re.search(r'\\?"'+kw+r'\\?":\s*\\?"?([^"\\,}]+)',afk)
            out[kw]=m.group(1) if m else None
        log(json.dumps(out,ensure_ascii=False,indent=2))
        import datetime as dt
        exp=out.get("runtimeExpiresAt")
        if exp:
            t=dt.datetime.fromisoformat(exp.replace("Z","+00:00"))
            hl=(t-dt.datetime.now(dt.timezone.utc)).total_seconds()/3600
            log(f"⏳ 运行时间剩余 {hl:.2f} 小时" + ("（已过期）" if hl<0 else ""))
        return 0
    finally:
        try: browser.close(); pw.stop()
        except Exception: pass
if __name__=="__main__": sys.exit(main())
