# OpenGameNode 守护（分支 opengamenode）

面板 https://panel.opengamenode.com （Calagopus）。**纯 HTTP：无浏览器、无 Turnstile、无代理。**

## 策略：能续就精准续，不能续就点启动

```
1. 离线/到点停机            → POST /power {"action":"start"}
2. running 且剩余 ≤60s 且 extensions_used < max_extensions
                            → POST /ogn-session/extend（窗口是最后 600s，脚本精确等在 deadline-60s）
3. 续期额度用尽（2/2）      → 等会话自然到期 → 再 start（换新会话，续期次数清零）
```

时间全部取服务端 `clock.session_deadline`（毫秒），不靠本地估算。

## 免费套餐（FreeTier）实测

```
session_limit_seconds 14400 (4h)   extension_seconds 14400 (4h)   max_extensions 2
idle_action stop / idle_seconds 300
```

单会话 4h，可续 2 次 → 最长 12h 连续在线；到点 `clock.action="session_limit"`、`state=offline`，不会自愈。
`extend` 只在最后 600 秒内接受，提前调用：
`409 {"errors":["Extend during the final 10 minutes, before the session expires"]}`

## 登录：Discord OAuth（不用密码、不用验证码）

```
GET  /api/auth/oauth/redirect/609712d4-1d05-45ad-8ad9-eea05c78941a
POST discord.com/api/v9/oauth2/authorize?<query 原样带过来>   → {"location": "...?code=..."}
GET  <location>&state=<s>      ← Discord 不回传 state，必须自己补
```
`client_id`/`scope`/`redirect_uri` 必须在 query 上；放 body 会 `BASE_TYPE_REQUIRED`。
面板 `registration_enabled=false`：只有绑过该 Discord 号的账号能进。

## 环境变量

```
OGN_DISCORD_TOKEN   必填
OGN_SERVER_UUIDS    可选，逗号分隔；留空=全部
OGN_DWELL_MINUTES   本轮驻留分钟数，默认 350
OGN_EXTEND_AT_SEC   到期前多少秒发 extend，默认 60
OGN_MODE            probe = 只读打印计划
```

## 驻留时长与 cron 的关系

单会话 12h，但 GitHub 单个 job 最多 6h，所以一轮驻留 350 分钟 + cron 每 6 小时一次：
两轮之间只有 10 分钟空档，靠下一轮接管。这样续期能精确落在窗口内。
