# SkyCastle AFK 挂机

**状态：✅ 已在 GitHub Actions 真实跑通并赚到 credit。**

```
💓 work → 200 · {"credits_awarded":1,"total_credits":1,"total_afk_time":10,"session_active":true}
```

## 一句话方案

**纯 HTTP，直连，不用住宅代理。**

GitHub Actions runner 出口（境外机房 IP）能直接访问本站；
只有本机/本地沙箱出口会被 TLS 层拒绝。所以完全不需要住宅代理。

## 实测数据

### 收益模型

`work` 每 **60 秒**（服务端硬限）调用一次，每次推进 `total_afk_time` +1 分钟。
**累积到第 10 分钟产出 1 credit。**

```
13:07:31  total_afk_time: 6   credits 0
13:08:37  total_afk_time: 7   credits 0
13:09:42  total_afk_time: 8   credits 0
13:10:47  total_afk_time: 9   credits 0
13:11:52  total_afk_time: 10  credits 1   ← 产出
```

→ **约 10 分钟 = 1 credit。**

### 频率硬限

`work` 少于 60 秒调用会被拒：

```
HTTP 429 {"error_code":"RATE_LIMIT_EXCEEDED",
          "message":"Please wait at least 60 seconds (1 minute) between work calls."}
```

脚本默认间隔 **65 秒**（留 5 秒余量）。

### 续期信息

```
GET /api/user/billingresourcesnewservers/renewals
{
  "queue": [],
  "renewals": [{
    "id": 531, "server_id": 531, "user_id": 452,
    "renewal_cost": 1,          ← 续一次花 1 credit
    "period_days": 3,           ← 续一次管 3 天
    "renews_at": "2026-11-25 11:53:12",
    "last_renewed_at": null,
    "auto_renew": 0,
    "renewal_count": 0,
    "status": "active"
  }]
}
```

**关键换算**：续期要 1 credit / 3 天 → 每天约 0.33 credit → 每天挂约 3.5 分钟就够。
换句话说，单轮 14 分钟的 AFK 就能攒够好几天。

## 接口全集

```
GET  /api/selftest                                   连通自检（免登录）
     → {"success":true,"data":{"status":"ready","checks":{redis,mysql,permissions}}}

GET  /api/user/billingresourcesnewservers/renewals   续期信息

POST /api/user/billingafk/start   {"action":"start"}
     → {"message":"AFK session already active","data":{"session_active":true,...}}

POST /api/user/billingafk/work    {"action":"work"}     ← 17 字节，与抓包 content-length 吻合
     → {"message":"AFK stats updated",
        "data":{"credits_awarded":N,"total_credits":N,"total_afk_time":N,"session_active":true}}
```

`start` 的请求体是按 `work` 的规律推定的（抓包里 start 的 content-length 未给出）。
实测 `{"action":"start"}` 正常工作。

## 登录：Discord OAuth（无需密码）

```
GET  /api/user/auth/discord/login
     → 302 discord.com/oauth2/authorize
       client_id = 1317246734530908221, scope = identify email
POST https://discord.com/api/v9/oauth2/authorize   （带 Discord token）
     body {"permissions":"0","authorize":true,"integration_type":0,
           "guild_id":null,"channel_id":null,"captcha_key":null}
     → {"location": ".../api/user/auth/discord/callback?code=..."}
GET  <该 location>  → 302 /auth/login?discord_token=<handoff>
PUT  /api/user/auth/login  {"discord_token":"<handoff>"}   → 200
```

成功后下发 `remember_token`（30 天），存进 `skycastle_state.json` 复用，免重复走 OAuth。

账号：`laihate` / id 452 / uuid `b2b58507-c67d-4ee7-9a7d-c6775554accf`

## 用法

```bash
python3 skycastle_afk.py --dry     # 只打印计划，不发请求
python3 skycastle_afk.py --probe   # 只验连通 + 鉴权（selftest + renewals）
python3 skycastle_afk.py --once    # start + 一次 work
python3 skycastle_afk.py           # 正常挂机（14 分钟上限，65 秒一次）
```

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SC_DISCORD_TOKEN` | ✅ | — | Discord 用户 token |
| `SC_BASE` | — | `https://panel.skycastle.us` | 面板地址 |
| `SC_PROXY` | — | 空（直连） | 仅在 runner 出口被封时才填 |
| `SC_WORK_INTERVAL` | — | `65` | work 间隔（服务端限 60s） |
| `SC_MAX_MINUTES` | — | `14` | 单轮上限 |

## 退出码

`0` 正常 · `2` 有失败 · `3` 登录/连通失败 · `4` 接口异常

## 排队

workflow 在 `main`（`.github/workflows/skycastle_afk.yml`），cron `41 */6 * * *`，单轮 ≤14 分钟。
代码在本 `skycastle` 分支。

## 待确认

1. 日上限是多少（跑了 10 分钟拿 1 credit，还没见到天花板）
2. `total_afk_time` 是否按 session 累计、session 会不会过期
3. 是否有自动续期接口（`auto_renew: 0` 这个字段暗示可能存在）
4. 需不需要偶尔调 `renewals` 之外的续期接口把 credit 花掉
