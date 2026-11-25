# Aurix Host AFK 赚币 + 精准续期（分支 aurix）

站点 https://dash.aurix.host/#/earn 。**纯 HTTP 干活，只有 Turnstile 那一下用真浏览器。**

## 流程

```
1) 真浏览器（Xvfb）渲染 Turnstile，点一下复选框 → 拿 gate / afk 两个 token
2) 纯 HTTP：Discord OAuth 登录 → 过入场 gate（30s 计时）→ AFK 挂机 → 到期前续期
```

## 实测到的接口（2026-11-25）

```
GET  /auth/login                     302 → discord oauth2/authorize（client_id 1567447623315488829）
GET  /auth/callback?code=..&state=.. 302 → /，下发 __Host-aurix_sid
GET  /api/me                         {user:{coins}, csrf, gateUntil, usage}
POST /api/adcheck                    {"blocked":false}
POST /api/gate/start                 → {nonce, seconds:30}
POST /api/gate/ping                  {"nonce","visible":true} → {watched,counted}
POST /api/gate/complete              {"nonce","turnstile":"<gate token>"} → {gateUntil}
POST /api/afk/start                  {"turnstile":"<afk token>"} → {id,maxTicks,coinsPerTick,nextTickAt}
POST /api/afk/beat                   {"id"} → ticksPaid / earned / balance / ended
GET  /api/servers                    → renewal:{expiresAt,secondsLeft}, renewalCost, renewalDays
POST /api/servers/{uuid}/renew       {} → {balance, expiresAt}
```

## 必须知道的六个坑

1. **所有写操作要带 `x-csrf-token`**（= `/api/me` 里的 `csrf`），缺了返回 403。
2. **入场 gate 是按会话的**：每次重新登录都要重过一次（30 秒计时 + turnstile），
   否则 `/api/servers`、`/api/afk/*` 全部 `428 {"error":"Watch the entry ad first."}`。
3. **`/api/adcheck` 必须先打一次 `{"blocked":false}`**，否则 `gate/complete` 返回
   `451 {"error":"Turn off your ad blocker to use this.","adblock":true}`。
4. **Turnstile 必须点复选框**：headless 拿不到 token；有界面 Xvfb 不点也是 0；
   点一下 2 秒内出 token（gate ≈730 字节、afk ≈752 字节）。action 要和请求对应。
5. **AFK 一轮 20 分钟**（tickMinutes=5、coinsPerTick=2、maxTicks=4）→ 每次最多 +8 coins。
   `beat` 每 30 秒一次即可，不 beat 会被判 idle 停掉。
6. **续期需要 20 coins / 14 天**，续期后必须比对 `renewal.expiresAt` 变了才算成功（HTTP 200 不算）。

## 用量估算

```
需求：20 coins / 14 天 = 1.43 coins/天
产出：一轮 20 分钟 = 8 coins；每天 2 轮 = 16 coins/天   → 富余约 11 倍
```

## 循环策略（当前配置）

```
本轮工作流 5 小时（cron 每 6 小时接续）
  ├─ AFK 会话 1：20 分钟
  ├─ 休息 44 分钟            ← 你要的间隔
  ├─ AFK 会话 2：20 分钟
  ├─ 休息 44 分钟
  └─ … 直到本轮剩余不足 21 分钟
每个会话后都查一次续期：剩余 ≤48h（最后 2 天）且余额 ≥20 才真的发
```

**每个 `afk/start` 都要新的 Turnstile token**（约 5 分钟有效、一次性），
所以循环里会按需再开一次浏览器点复选框 —— job 里 Chromium 已装好，不需要额外步骤。

一个小发现：站点在会话跑到约 14~15 分钟时会再复核一次广告拦截，
`beat` 先返回 451、随后服务端直接把会话判 `endReason=adblock`。
所以实际单会话约 15 分钟 / +4 coins。脚本会自动重报 adcheck 并继续下一轮，
不会因此中断整轮工作流。

## 用法

```
AURIX_DISCORD_TOKEN=... python3 aurix_afk.py --tokens   # 产出 token（需 Xvfb）
AURIX_DISCORD_TOKEN=... python3 aurix_afk.py            # 主流程
AURIX_MODE=probe ...                                    # 只读
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AURIX_DISCORD_TOKEN` | — | 必填 |
| `AURIX_RUN_MINUTES` | 300 | 本轮循环总时长（分钟） |
| `AURIX_REST_MINUTES` | 44 | 每个会话之间休息（分钟） |
| `AURIX_RENEW_AT_HOURS` | 48 | 剩余多少小时以内才续期（最后 2 天） |
| `AURIX_TOKENS_FILE` | `/tmp/aurix_tokens.json` | token 文件 |

workflow 在 main：`.github/workflows/aurix_afk.yml`，cron `23 */6 * * *`（job 上限 6 小时，一轮 5 小时）。
