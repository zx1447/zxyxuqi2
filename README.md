# SkyCastle（panel.skycastle.us）AFK 挂机

**⚠️ 状态：接口来自用户浏览器抓包，沙箱侧站点当前不可达，逻辑尚未真实跑通。**

## 已知接口

两个都是 `POST`，返回 `200`（实测抓包）：

```
POST /api/user/billingafk/start     开始 AFK 计费
POST /api/user/billingafk/work      工作 / 心跳（需按站点要求周期调用）
```

请求体：抓包时没看到 payload，脚本默认发 `{}`。
**首次真实运行请先看响应体**，如果要求带字段（如 `sessionId` / `token`），补进 `afk_start()` / `afk_work()`。

## 登录：Discord OAuth（无需密码）

```
GET  /api/user/auth/discord/login
     → 302 到 discord.com/oauth2/authorize
       client_id = 1317246734530908221
       scope     = identify email
POST https://discord.com/api/v9/oauth2/authorize   （带 Discord token）
     body {"permissions":"0","authorize":true,"integration_type":0,
           "guild_id":null,"channel_id":null,"captcha_key":null}
     → {"location": ".../api/user/auth/discord/callback?code=..."}
GET  <那个 location>  → 302 到 /auth/login?discord_token=<handoff>
PUT  /api/user/auth/login   {"discord_token":"<handoff>"}
     → 200 {"success":true,"data":{"user":{...}}}
```

成功后下发 `remember_token` cookie，**有效期 30 天**，脚本会存进 `skycastle_state.json` 复用。

实测账号：

```
用户 laihate · id 452
uuid  b2b58507-c67d-4ee7-9a7d-c6775554accf
email 450144yy@gmail.com
```

## 已有服务器

```
GET /api/user/servers
  id=531  uuid=badd5f53-d84e-4f05-a22e-571d93acdb9e  name=nice  status=running
```

## 用法

```bash
# 先看请求（不发真实请求）
SC_DISCORD_TOKEN=xxx python3 skycastle_afk.py --dry

# 单轮验证（start 一次 + work 一次）
SC_DISCORD_TOKEN=xxx python3 skycastle_afk.py --once

# 正常挂机（默认 14 分钟上限，每 60 秒一次 work）
SC_DISCORD_TOKEN=xxx python3 skycastle_afk.py

# 走住宅代理
SC_DISCORD_TOKEN=xxx SC_PROXY=socks5h://127.0.0.1:10809 python3 skycastle_afk.py
```

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SC_DISCORD_TOKEN` | ✅ | — | Discord 用户 token |
| `SC_BASE` | — | `https://panel.skycastle.us` | 面板地址 |
| `SC_PROXY` | — | 空 | `socks5h://host:port` |
| `SC_WORK_INTERVAL` | — | `60` | work 调用间隔（秒） |
| `SC_MAX_MINUTES` | — | `14` | 单轮上限（分钟） |

## 退出码

`0` 正常 · `2` 有失败 · `3` 登录失败 · `4` 接口不符合预期

## 阻塞点：站点侧拒绝 TLS 握手

2026-11-22 12:3x UTC 起，`panel.skycastle.us`（`49.13.124.85`）无法建立 TLS：

| 检查 | 结果 |
|---|---|
| DNS | `49.13.124.85` ✅ |
| TCP 443 | ✅ 可连 |
| **TLS 握手** | ❌ 超时（curl / python / requests 均如此） |
| 换 SNI（`skycastle.us` / `example.com`） | ❌ 同样超时 → **与 SNI 无关** |
| 直连 vs 住宅代理 `83.168.94.237` | ❌ 都一样 |
| 同期其他站点（代理内） | ✅ 200 → 代理本身正常 |
| 根域 `skycastle.us` | ✅ 200（Cloudflare） |

**结论**：这不是 SNI 封锁，也不是单纯 IP 维度封锁（住宅 IP 同样被拒），
而是这台源站整体不再正常响应 TLS。只能等站点侧恢复。

## 待确认（拿到响应体后才能定）

1. `billingafk/work` 的**最短/最长调用间隔**——太慢可能不计费，太快可能被判刷
2. 是否需要人类活动证据（有些站要求随机鼠标事件）
3. 单次收益 / 日上限 / 冷却
4. 收益的用途（换服务器时长？还是商店币？）
5. 是否有 `stop` / `status` 类接口

站点恢复后按上面清单实测，把结论补进本文件。
