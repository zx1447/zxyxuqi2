# SkyCastle AFK 挂机

**状态：✅ 已在 GitHub Actions 真实跑通，赚到 credit，日限额保护已上线。**

## 一句话方案

**纯 HTTP，直连，不用住宅代理。** GitHub runner 出口能直连本站；只有本机/沙箱出口会被 TLS 拒绝，那是环境差异。

## ⚠️ 日限额（这是最关键的约束）

来自 `GET /api/user/billingafk/status`：

| 字段 | 值 | 含义 |
|---|---|---|
| `max_credits_per_day` | `"50"` | 每天最多 50 credit |
| `max_sessions_per_day` | `"3"` | **每天只能启动 3 次 session** |
| `max_time_per_day_seconds` | `"30000"` | 每天最多 8.33 小时 |
| `credits_per_minute` | `0.1` | **按分钟计价** |
| `reward_interval_seconds` | `60` | work 最小间隔 60 秒 |
| `require_claim` | `True` | 可能需要额外 claim 才入账 |
| `require_active_tab` | `True` | 要求标签页在前台（纯 HTTP 满足不了，属服务端校验） |
| `require_captcha` | `False` | ✅ 不需要验证码 |

### 收益换算

```
0.1 credit/分钟  →  10 分钟 = 1 credit
单轮 14 分钟（受"单轮 ≤15 分钟"约束）≈ 1.4 credit
```

**按次计价是错的，按分钟计价才是对的。** 之前看到"10 分钟产出 1 credit"，
实际是 `total_afk_time` 到 10 分钟时凑够了 1.0 credit。

### 三种上限哪个先到

| 限制 | 撞线条件 |
|---|---|
| 50 credits | 需 8.3 小时/天 |
| **3 sessions** | **每 session 挂 2.8 小时才能赚满 50** |
| 30000 秒 | 8.33 小时，基本不会先到 |

所以**真正卡脖子的是 session 次数（3 次/天）**，不是时长也不是币。

### 实际需要多少

续期 1 credit / 3 天 → **每 3 天挂 10 分钟就够**。

现有配置（cron 每 6 小时，每轮 14 分钟）理论产出：
`3 sessions × 1.4 = 4.2 credit/天`，远超续期需求 0.33/天。

**结论：现有频率已经过剩，不需要为了配额加频。**

## 实测数据

### status（完整）

```
credits_per_minute            0.1
reward_interval_seconds       60
max_credits_per_day           "50"
max_sessions_per_day          "3"
max_time_per_day_seconds      "30000"
require_claim                 True
auto_claim_interval_seconds   None
require_active_tab            True
require_captcha               False
minutes_afk                   10
last_seen_afk                 1795353112
session_active                False
user_credits                  1
daily_usage    {credits_earned_today: 1, sessions_today: 1, time_seconds_today: 60}
daily_limits   {max_credits_per_day: "50", max_sessions_per_day: "3",
                max_time_per_day_seconds: "30000"}
captcha        []
is_enabled     True
```

`javascript_injection` 字段注入的"广告"其实是个空 `div`（`ad.innerHTML = \`\``），
没有真正的广告加载 —— 纯 HTTP 完全够用。

### 一次完整运行

```
📊 日限额：credits 1/50 · sessions 1/3 · 时长 60s/30000s · 余额 1 credits
⚙️  收益 0.1 credit/分钟 · work 间隔 60s · require_claim=True · require_active_tab=True
▶️  start → 200 {"message":"AFK session started successfully",
                 "data":{"session_active":true,"session_started_at":1795353556}}
💓 work  → 200 {"credits_awarded":0,"total_credits":1,"total_afk_time":11}
💓 work  → 200 {"credits_awarded":0,"total_credits":1,"total_afk_time":12}
💓 work  → 200 {"credits_awarded":0,"total_credits":1,"total_afk_time":13}
💓 work  → 200 {"credits_awarded":0,"total_credits":1,"total_afk_time":14}
✅ 本轮完成，work × 4
```

### work 速率硬限

```
HTTP 429 {"error_code":"RATE_LIMIT_EXCEEDED",
          "message":"Please wait at least 60 seconds (1 minute) between work calls."}
```

脚本间隔 **65 秒**（留 5 秒余量）。

### stats

```
{"total_time_seconds":600,"total_time_formatted":"0d 0h 10m",
 "total_credits_earned":1,"sessions_count":0,"last_session_at":null}
```

## 续期

```
GET /api/user/billingresourcesnewservers/renewals
{
  "renewal_cost": 1,        ← 1 credit
  "period_days": 3,         ← 管 3 天
  "renews_at": "2026-11-25 11:53:12",
  "last_renewed_at": null,
  "auto_renew": 0,
  "status": "active"
}
```

⚠️ **续期接口还没找到**。试过这些全 404：
```
POST /api/user/billingresourcesnewservers/renew
POST /api/user/billingresourcesnewservers/531/renew
GET  /api/user/billingresourcesnewservers
```
需要在面板上手动点一次，从 Network 抓出真正的续期 URL。

## 接口全集

```
GET  /api/selftest                                   连通自检（免登录）
GET  /api/user/billingafk/status                     ⭐ 配置 + 日限额 + daily_usage
GET  /api/user/billingafk/stats                      累计统计
GET  /api/user/billingresourcesnewservers/renewals   续期信息
POST /api/user/billingafk/start   {"action":"start"}
POST /api/user/billingafk/work    {"action":"work"}    ← 17 字节，与抓包吻合
POST /api/user/billingafk/stop    {"action":"stop"}   → 200 session_active:false
```

## 登录：Discord OAuth（无需密码）

```
GET  /api/user/auth/discord/login → 302 discord（client_id 1317246734530908221）
POST https://discord.com/api/v9/oauth2/authorize  （带 Discord token）
GET  <callback>  → /auth/login?discord_token=<handoff>
PUT  /api/user/auth/login {"discord_token":"<handoff>"} → 200 + remember_token(30天)
```

账号：`laihate` / id 452 / uuid `b2b58507-c67d-4ee7-9a7d-c6775554accf`

## 用法

```bash
python3 skycastle_afk.py --dry     # 只打印计划
python3 skycastle_afk.py --probe   # 只验连通 + 鉴权
python3 skycastle_afk.py --once    # start + 一次 work
python3 skycastle_afk.py           # 正常挂机（14 分钟，65 秒一次 work）
```

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SC_DISCORD_TOKEN` | ✅ | — | Discord 用户 token |
| `SC_BASE` | — | `https://panel.skycastle.us` | 面板地址 |
| `SC_PROXY` | — | 空（直连） | 仅在 runner 出口被封时才填 |
| `SC_WORK_INTERVAL` | — | `65` | work 间隔（服务端限 60s） |
| `SC_MAX_MINUTES` | — | `14` | 单轮上限 |

## 部署

```
zxyxuqi2 / skycastle → skycastle_afk.py / README.md / workflow
zxyxuqi2 / main      → .github/workflows/skycastle_afk.yml  (cron '41 */6')
secret               → SC_DISCORD_TOKEN
```

脚本内建限额保护：`sessions_today >= 3` 时本轮直接跳过，不浪费一次启动。

## 待确认

1. **续期接口**（最要紧 —— 531 11-25 到期，得能把 credit 换成续期）
2. session 什么时候结束算一个"session"（是 start/stop 才计数，还是超时也算）
3. `require_claim=True` 是否需要额外 claim 调用才真正入账（目前 credits 是会涨的，应该不用）
4. `require_active_tab=True` 是否会在某个时间点开始校验（目前纯 HTTP 没被拦）
