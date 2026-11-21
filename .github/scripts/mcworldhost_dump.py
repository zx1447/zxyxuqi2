"""Dump shop RSC payload + item ids, and try buy with full response."""
import json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in
BASE="https://mcworldhost.de"
def log(m): print(m, flush=True)
def main():
    pw,browser,page=open_session(BASE,wait_seconds=150)
    try:
        sign_in(page,BASE,os.environ["MCWH_EMAIL"],os.environ["MCWH_PASSWORD"],timeout=120000)
        log("✅ 登录")
        shop=page.evaluate("async()=>(await(await fetch('/dashboard/shop')).text())")
        log(f"shop payload len={len(shop)}")
        # 找所有形如 "id":"xxx" 与 price
        ids=re.findall(r'\\?"id\\?":\s*\\?"([a-z0-9_]+)\\?"', shop)
        log("item ids: "+str(sorted(set(ids))))
        for m in re.finditer(r'\\?"([a-z0-9_]+)\\?"[^}]{0,200}?\\?"price\\?":\s*(\d+)', shop):
            log(f"  {m.group(1)} = {m.group(2)}")
        # 关键字段
        for kw in ["runtime_7d","Runtime","7 Days","7-day","7 day"]:
            for m in re.finditer(re.escape(kw), shop):
                log(f"  [{kw}] …{shop[max(0,m.start()-120):m.start()+160]}…".replace("\n"," ")[:320]); break
        # 抓 Buy 按钮的 DOM 结构
        page.goto(BASE+"/dashboard/shop", wait_until="domcontentloaded"); page.wait_for_timeout(6000)
        html=page.content()
        log(f"rendered html len={len(html)}")
        # 打印每个 Buy 按钮周围的卡片文本
        cards=page.eval_on_selector_all("button", """els=>els.filter(e=>e.innerText.trim()==='Buy').map(e=>{
            let p=e.closest('[class*=card],[class*=item],li,article,div');
            for(let i=0;i<6&&p;i++){ if(p.innerText && p.innerText.length>20 && p.innerText.length<400) return p.innerText.replace(/\\n+/g,' | '); p=p.parentElement; }
            return ''; })""")
        for i,c in enumerate(cards): log(f"  CARD{i}: {c[:200]}")
        # 点击第一个 Buy 并 dump 完整响应
        resp={}
        def on_resp(r):
            if r.request.method=="POST" and r.url.endswith("/dashboard/shop"):
                try: resp['body']=r.text()[:2500]; resp['status']=r.status
                except Exception: pass
                resp['req']=r.request.post_data
                resp['act']=r.request.headers.get("next-action")
        page.on("response", on_resp)
        page.get_by_role("button", name=re.compile("^Buy$", re.I)).first.click()
        page.wait_for_timeout(8000)
        log(f"CLICK req={resp.get('req')} act={resp.get('act')} status={resp.get('status')}")
        log(f"CLICK resp={ (resp.get('body') or '')[:1500] }")
        coins=page.evaluate("async()=>(await(await fetch('/dashboard/coins')).text())")
        m=re.search(r'\\?"balance\\?":\s*(\d+)',coins)
        log(f"balance now: {m.group(1) if m else '?'}")
    finally:
        try: browser.close(); pw.stop()
        except Exception: pass
if __name__=="__main__": main()
