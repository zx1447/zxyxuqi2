#!/usr/bin/env python3
"""Fetch a VLESS+WebSocket+TLS subscription without logging credentials."""
import base64, json, os, sys, urllib.parse, urllib.request

def fail(message):
    print('proxy setup error: ' + message, file=sys.stderr)
    raise SystemExit(1)
url=os.environ.get('AFK_SUBSCRIPTION_URL','').strip()
if not url or urllib.parse.urlsplit(url).scheme not in ('http','https'): fail('missing or invalid subscription URL')
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=20) as r:
        data=r.read(1024*1024).decode('utf-8').strip()
except Exception as e: fail('subscription download failed (' + type(e).__name__ + ')')
if '://' not in data:
    try: data=base64.urlsafe_b64decode(data + '=' * (-len(data)%4)).decode('utf-8')
    except Exception: fail('unsupported subscription encoding')
entries=[x.strip() for x in data.splitlines() if x.strip().startswith('vless://')]
if not entries: fail('no VLESS entry')
u=urllib.parse.urlsplit(entries[0]); q=urllib.parse.parse_qs(u.query)
def param(key, default=''): return q.get(key,[default])[0]
if not u.username or not u.hostname or not u.port or param('type','tcp')!='ws' or param('security','none')!='tls': fail('only VLESS WebSocket TLS is supported')
stream={'network':'ws','security':'tls','tlsSettings':{'serverName':param('sni',u.hostname),'fingerprint':param('fp','chrome')},'wsSettings':{'path':param('path','/')}}
if param('host'):stream['wsSettings']['headers']={'Host':param('host')}
config={'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':17890,'protocol':'http','settings':{}},{'listen':'127.0.0.1','port':17891,'protocol':'socks','settings':{'auth':'noauth','udp':False}}], 'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':u.hostname,'port':u.port,'users':[{'id':urllib.parse.unquote(u.username),'encryption':'none'}]}]},'streamSettings':stream}]}
path=os.environ.get('AFK_PROXY_CONFIG','/tmp/afk-proxy.json')
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:json.dump(config,f)
print('subscription: VLESS + WebSocket + TLS; config created')
