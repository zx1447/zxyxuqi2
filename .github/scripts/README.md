# nodarooAFK

免费主机 [Nodaroo Free](https://free.nodaroo.com)（Heliactyl 面板）的挂机赚积分脚本，
纯 Python（requests + websockets），无浏览器依赖。

## 机制

- 登录：Discord OAuth（token 代授权，纯 API），cookie 三级回退：
  加密存档 → `NODAROO_COOKIE` → OAuth 现登
- 挂机：`GET /afk` 取 `data-afk-token`，作为 subprotocol 连
  `wss://free.nodaroo.com/afkwspath`，服务端每 60 秒推 `{"balance": N}`，+1 credit
- 站点规则：每账号只允许一个收益连接；断线后可重连

## 调度（2026-11-16 起的两棒节奏）

用户指定：**每次挂机 6 小时 → 冷却 30 分钟 → 循环**。GitHub 单 job 上限 360 分钟，
一个 6 小时会话装不下，拆成两棒接力：

- 第 1 棒（phase=1）：挂机 178 分钟 → 立即派发第 2 棒
- 第 2 棒（phase=2）：挂机 178 分钟 → **冷却 30 分钟** → 派发下一会话的第 1 棒

178 + 178 = 356 分钟 ≈ 6 小时挂机；中途换棒重连几秒，站点明确允许
（"Reconnect if your browser sleeps"）。schedule 每 6 小时兜底防断链。
state 加密（`cryptography.Fernet`）后由 CI 提交回本分支。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `NODAROO_DISCORD_TOKEN` | Discord 用户 token（登录兜底，主要方式） |
| `NODAROO_SESSION_KEY` | Fernet key，加密存档 cookie 用 |
| `NODAROO_COOKIE` | 静态 cookie（备用） |
| `NODAROO_VLESS_URL` | 家宽 VLESS 订阅，直连被 CF 拦时脚本内拉 xray 兜底 |
| `NODAROO_FARM_MINUTES` | 本轮挂机分钟数（workflow 按 two-leg 传 178） |
| `NODAROO_NO_EARN_MINUTES` | 连续无收益 N 分钟提前收工（默认 10） |
| `NODAROO_PROXY` | 出口 SOCKS5（workflow 起 xray 后注入；不设则直连） |
| `XRAY_BIN` | xray 二进制路径（兜底用，默认 `~/.xray/xray`） |

日志里 cookie / token 一律打码。
