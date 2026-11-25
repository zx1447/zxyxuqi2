# BotHoster（bothoster.top）保活

同一 Discord（lycoris）账号。Basic #38 = **1.0000 Coins/h ≈ 24 coins/天**，所以「一直运行」的唯一问题就是**每天凑够 24 coins**。

## 站点机制（2026-11-25 实盘核对）

| 事实 | 证据 |
|---|---|
| 服务器 `serverId = 13`，`Basic #38`，地址 `syd2.bothoster.top:24619` | `/dashboard/servers` 的 `serverTelemetry({ serverId: 13, … })` |
| 计费 **按整点扣费**，账本里叫「hourly compute deductions」 | `/dashboard/transactions`：注册 +5.00、`PROVISION` 首小时预付 -1.00，之后 09:01 整点**没有**扣费记录 |
| 余额只增不减的那几小时 = 站点侧扣费没跑满，不是我们赚到了 | 08:27→09:03 心跳 +0.25×多，余额 5.75 未变 |
| **`renewServer(13)` 只允许对「已挂起」的服务器续期**，正常运行的机器返回 `This server is not suspended.` | Livewire 直调实测 |
| 因此「小时续」不需要我们点：余额 ≥1 就自动续下一小时；**余额 <1 → 挂起停机** | 同上 + 挂起后才可手动续 |
| `powerAction(13,'start'\|'restart'\|'stop')` 随时可用 | 实跑 `restart` → `Đã gửi lệnh thành công: RESTART` |
| AFK：0.25 coins / 60s，`dailyMax = 10` → **最多 10 coins/天 = 10 小时** | `/dashboard/afk` |
| Work.ink：`POST /dashboard/earn/workink/generate` → 短链 + 4.0000，**每天 6 次 = 24 coins** | `/dashboard/earn` |

**结论：AFK 一个人撑不住 24 小时（只够 10 小时），缺口 14 coins/天 必须靠 Work.ink。**

## 脚本

`bothoster.py`（纯 HTTP，无需浏览器；`data-update-uri` 的 Livewire 端点 + `X-Livewire` 头）

```bash
export BOTHOSTER_DISCORD_TOKEN=...
BH_MODE=status    python3 bothoster.py   # 只读：状态/剩余小时/余额
BH_MODE=probe     python3 bothoster.py   # 状态 + AFK 组件快照
BH_MODE=afk       BH_RUN_MINUTES=50 python3 bothoster.py
BH_MODE=renew     python3 bothoster.py   # 显式续期（仅挂起时有效，会自己核对到期时间）
BH_MODE=service   python3 bothoster.py   # 默认：启动 → 按需续期 → 余额低就 AFK 补币
```

判定一律用真实读数：`status`、`remainingSeconds`、`expiresAtTimestamp`、`navbar-balance.credits`、`/dashboard/transactions` 账本。HTTP 200 不算成功。

## 工作流

`main` 分支：`.github/workflows/bothoster_keepalive.yml`，`cron: '7 * * * *'`（每小时第 7 分），单轮最多 70 分钟，无接力、无 job 链。
代码在独立分支 `bothoster`（checkout `ref: bothoster`）。
Secret：`BOTHOSTER_DISCORD_TOKEN`（Discord user token）。

## Work.ink 进度（仍未打通）

已解决：
1. 短链生成（纯 HTTP，带 CSRF）→ `https://work.ink/28PC/bothoster-coin-reward-<6位>`
2. CF Turnstile：纯 HTTP 必然 403；**xvfb + Playwright headful** 点 `challenges.cloudflare.com` 子 frame 的 `(x+30, y+h/2)`，约 3 秒过站
3. work.ink 登录墙：原来以为是死路，其实可以注入 Discord token —— 短链页点 `Sign in with Discord` 会跳到
   `discord.com/login?redirect_to=/oauth2/authorize?client_id=1461383370012623099&redirect_uri=https://work.ink/_api/v2/oauth/discord/callback&scope=identify+email+connections&state={"to":"<短链>","checkoutSession":null,"clearance":"<uuid>"}`
   → 用令牌 `POST discord.com/api/v9/oauth2/authorize?<同一 query>` 拿 `location`，再在那个 tab 里 goto 它 → 登录成功（cookie 落在 work.ink）
   `state` 里的 `clearance` 是 BotHoster 一次性发的一次性凭证，**不能复用**（下次短链 clearance 不同）

卡点：登录成功后短链页直接弹 **`Proxy/VPN Was Detected — Please disable it to support our publishers`**。
沙箱出口是 `136.87.109.162 / AS396982 Google LLC`，机房 IP 必被判 VPN。**换住宅/家宽出口才有可能继续，且后面还有 reCAPTCHA enterprise + 广告 checkpoint。**

## 每天 24 coins 的账

| 来源 | 每天上限 | 备注 |
|---|---|---|
| AFK | 10 coins（10h） | 已跑通，纯 HTTP |
| Work.ink | 24 coins（6×4） | 需住宅 IP + 登录 + 过 checkpoint，未通 |
| 注册赠送 | +5.00 一次性 | 已用掉 |

纯 AFK 时：每天净 **-14 coins**，余额 5.75 撑不了半天就会挂起停机。
