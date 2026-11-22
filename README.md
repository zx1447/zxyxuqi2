# LemonHost AFK 挂机（lemo）

[app.lemonhost.me](https://app.lemonhost.me) AFK 赚 credits。

## 接口

```
GET  /api/client/account                  账户（credits 余额）
POST /api/client/account/afk/claim        领取，body {"sessionId":"<任意>"}（camelCase！）
```

**没有** `/api/client/servers`。

## 规则

| 项 | 值 |
|---|---|
| 单次收益 | +1 credit |
| 冷却 | ~60 秒 |
| 日上限 | 1000 |

## 强度（用户指定，不要再调高）

```
cron: 37 */8 * * *      单轮 5 分钟      TICK 90 秒
```

## 文件

| 文件 | 说明 |
|---|---|
| `earn_lemonhost.py` | 挂机脚本 |
| `lemo_state.json` | 状态缓存 |

## 环境变量

`LEMONHOST_API_KEY`（`ptlc_...`）

workflow 在 `main` 分支（`.github/workflows/earn_lemonhost.yml`）。
