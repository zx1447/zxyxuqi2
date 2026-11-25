# OpenGameNode 守护（分支 opengamenode）

面板：https://panel.opengamenode.com （Calagopus）。**纯 HTTP，无浏览器、无 Turnstile、无住宅代理。**

## 登录：Discord OAuth（不用密码）

```
GET  /api/auth/oauth/redirect/609712d4-1d05-45ad-8ad9-eea05c78941a
     → 302 discord.com/oauth2/authorize?client_id=1510780042509291622&scope=email+identify&state=<s>&redirect_uri=...
POST discord.com/api/v9/oauth2/authorize?<上面那串 query 原样带过来>
     body {"permissions":"0","authorize":true,"integration_type":0,
           "guild_id":null,"channel_id":null,"captcha_key":null}
     → 200 {"location":"https://panel.opengamenode.com/api/auth/oauth/<uuid>?code=..."}
GET  <location>&state=<s>          ← Discord 不回传 state，必须自己补，否则 400 missing field `state`
     → 307 到面板首页，下发 calagopus_session cookie
```

关键坑：
- `client_id` / `scope` / `redirect_uri` 必须放在 **query** 上，放 body 会返回 `BASE_TYPE_REQUIRED` 或 `CONTENT_TYPE_INVALID`。
- 面板 `registration_enabled=false`：**只有绑定过该 Discord 号的账号能进**，没绑会跳 `/auth/login?error=registration_disabled`。

密码登录则必须过 Turnstile（`password_login_enabled=true`，
`{"user","password","captcha"}`，无 captcha → 400 `captcha: required`），
所以 OAuth 是唯一免浏览器的路径。

## 免费套餐参数（GET /api/client/servers/{uuid}/ogn-session）

```
plan: FreeTier, paid=false, queue_priority=0
session_limit_seconds 14400 (4h)     extension_seconds 14400 (4h)
max_extensions 2                     idle_action "stop" / idle_seconds 300
clock: status / elapsed_ms / extensions_used / action / session_deadline
```

会话到点后**不会自己重启**：`clock.action="session_limit"`，`resources.state=offline`。

## 动作规则

| 情况 | 动作 |
|---|---|
| `resources.state` 不是 running/starting | `POST /power {"action":"start"}` |
| running 且剩余 ≤ 10 分钟、`extensions_used < 2` | `POST /ogn-session/extend` |
| 剩余 ≤ 10 分钟但续期额度用尽 | `stop` → 等 8s → `start`（换新会话） |

`extend` 只在**最后 10 分钟内**被接受，提前调用返回
`409 {"errors":["Extend during the final 10 minutes, before the session expires"]}`。

## 脚本

`ogn_keepalive.py`

```
OGN_DISCORD_TOKEN   必填
OGN_SERVER_UUIDS    可选，逗号分隔；留空=全部
OGN_DWELL_MINUTES   可选，默认 0（检查一次就退出）
OGN_CHECK_SECONDS   驻留间隔，默认 300
```

workflow 在 main：`.github/workflows/ogn_keepalive.yml`（手动触发）。
