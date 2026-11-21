# Revenact 自动签到 + AFK 挂机（分支 `revenact`）

站点：<https://console.revenact.io> ｜ 每日奖励页：`/billing/rewards/daily`
账号：`zxy1715x@gmail.com`

## 一、站点机制（2026-11-16 从前端 chunk 逆向 + 实测）

| 项 | 内容 |
|---|---|
| 前端 | Vite + React SPA，接口前缀 `/api` |
| 会话 cookie | `revenact_session` |
| 登录 | `POST /api/auth/login` `{"identifier","password"}` |
| 新设备验证 | 返回 `{"twoFactorRequired":true,"challengeId":...,"method":"email","emailHint":"z•••x@gmail.com","reason":"new_location"}`，需邮箱 6 位码 |
| 验证码校验 | `POST /api/auth/login/2fa` `{"challengeId","code"}` → `{"token","userId"}` |
| 会话认证 | **`Authorization: Bearer <token>`**（cookie 方式实测 401，不认） |
| token 有效期 | 约 7 天 |
| **TOTP 免码** | ✅ 已启用。`REVENACT_TOTP_SECRET` 存密钥，脚本自己算码；设置后裸登录常直接返回 token |
| 备用登录 | Discord OAuth，`client_id=1568209280274796695`，`scope=identify email guilds.members.read guilds.join`，回调 `/api/auth/discord/callback` |

### 奖励接口（按 team/tenant 维度）

```
GET  /api/tenants/{tid}/rewards                 状态：baseRewardCents / currentStreak /
                                                dailyCapCents / earnedTodayCents / canClaim
POST /api/tenants/{tid}/rewards/claim           每日签到
POST /api/tenants/{tid}/rewards/afk/start       AFK 开始
POST /api/tenants/{tid}/rewards/afk/heartbeat   AFK 心跳（挂机期间每分钟）
POST /api/tenants/{tid}/rewards/afk/stop        AFK 结束
```

⚠️ **心跳必须每 30 秒一次**（前端 `setInterval(...,30000)`）。超过约 60 秒不发 →
`400 {"error":"No active AFK connection"}`，会话作废。
⚠️ `start` 返回 `409 "You already have an AFK connection active"` = 已有会话在跑，直接接管心跳即可。
⚠️ 奖励以「分」为单位（`*Cents`）。
⚠️ 每调一次 `POST /api/auth/login` 都会重发邮件并作废旧 `challengeId`，脚本保证不重试。

### 实测收益（2026-11-16 两次长测）

**单位：1 bolt = 100 cents。** 站点 UI "About 2.4 bolts an hour" = 240 cents/h = 4 cents/min ✔

- 签到：**冷却 14 小时**（不是 24h）。base 1333 + streak 67/天 → 约 14.00/次
- AFK：**4 cents/min = 2.4 bolts/小时**，脉冲式结算（每 2 分钟跳一次）
- AFK 启动有 **1.5~2 分钟预热期**不计费
- AFK 日上限 `dailyCapCents=3000` = **30 bolts** → 需 **750 分钟 = 12.5 小时**
- party 加成：同时在线 ≥3 人开始 +0.1×，8 人封顶 1.5×

## 二、脚本 `revenact.py`

```bash
python revenact.py --status         # 只看状态（含下次可领时间）
python revenact.py --claim          # 立即尝试签到
python revenact.py --claim-if-due   # 服务端 canClaim 才签到（cron 用）
python revenact.py --afk 360        # 挂机 360 分钟
python revenact.py --all 360        # 签到 + 挂机 360 分钟
python revenact.py --login-only     # 只发登录请求，打印 challengeId
python revenact.py --verify 123456  # 用最新邮件里的 6 位码换 token
```

| 环境变量 | 说明 |
|---|---|
| `REVENACT_EMAIL` | 邮箱 |
| `REVENACT_PASSWORD` | 密码 |
| `REVENACT_TOKEN` | Bearer token（约 7 天） |
| `REVENACT_TOTP_SECRET` | **必需**，TOTP 密钥（32 位 base32），彻底免验证码 |
| `REVENACT_TENANT` | 可选，指定 team/tenant id；留空取第一个 |
| `REVENACT_AFK_MINUTES` | 挂机分钟数 |

状态写 `revenact_state.json`（含 `challenge_id`、上次状态），CI 回写。

## 三、Workflow `revenact.yml`

- `schedule: '13 1 * * *'` 每天 UTC 01:13 自动签到
- 手动 dispatch 参数：`mode`（status/claim/all）、`afk_minutes`、`verify_code`

### 运行节奏（用户指定）

```
每轮：挂满 360 分钟 → 休息 44 分钟 → 再挂满 360 分钟 → 循环
签到：每轮开头检查，canClaim=true 且距上次 ≥14h 才领
cron：'7 0,7,14,21 * * *'（每 7 小时起一轮，两班覆盖全天）
```

⚠️ GitHub Actions 单 job 上限 **6 小时（350 分钟）**，所以 360 分钟会被硬切。
需要跑满 12.5 小时必须靠多个 cron 班次接力，单班做不到。

### token 过期时 —— 全自动，无需人工

脚本流程：
1. 用 `REVENACT_TOKEN` 直接跑；401 就自动登录
2. `POST /api/auth/login` → 若直接给 token（启用 TOTP 后实测如此）→ 完成
3. 若仍返回 `twoFactorRequired` → 用 `REVENACT_TOTP_SECRET` 自己算出 6 位码
   填 `POST /api/auth/login/2fa` → 拿 token
4. 全程不需要邮箱、不需要人工

TOTP 算法为 RFC 6238（HMAC-SHA1 / 30 秒 / 6 位），且会顺带试前后一个窗口容错漂移。

## 四、当前状态（2026-11-16 实测通过）

| 项 | 结果 |
|---|---|
| 登录 | ✅ 邮箱 + 验证码 → token |
| 签到 | ✅ `+13.33`，连击 1 天，余额 113.49 |
| AFK 挂机 | ✅ 30s 心跳稳定累计，4 分/分钟 |
| secret | ✅ `REVENACT_EMAIL` / `REVENACT_PASSWORD` / `REVENACT_TOKEN` 已写入 |
| 定时 | ✅ `7 0,7,14,21 * * *`，每班挂 360 分钟 |
| TOTP | ✅ 密钥已存 secret，登录完全免验证码 |

## 五、待观察

- AFK 的 challenge（cap token）只在部分情况触发；目前未触发，若将来 `start`
  返回 challenge 相关错误，脚本会明确打出并退出码 6。
