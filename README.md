# SkyCastle AFK 与服务器续期

代码在 `skycastle` 分支，工作流在 main 的 `.github/workflows/skycastle_afk.yml`；使用 `SC_DISCORD_TOKEN` 登录，默认纯 HTTP 直连。原有 cron 和挂机节奏不变。

## AFK

`POST /api/user/billingafk/start` 后，每 65 秒 `POST /api/user/billingafk/work`。站点返回 0.1 credit/分钟，work 最小间隔 60 秒。2026-11-25 的 Actions 实际记录显示余额从 9 增加到 10，不只是 HTTP 200。工作流单轮原有设置为 14 分钟。

## 续期与电源

- `GET /api/user/billingresourcesnewservers/renewals` 列出服务器与续期窗口。
- 用户抓包：`POST /api/user/billingresourcesnewservers/renewals/{id}`。
- 对所有返回的服务器，只有到期不超过 48 小时且 `can_renew` 为 true 才请求续期；日期按站点当前观察到的 UTC 解析。失败不会自动重试扣费；续期后重新读取续期列表并检查到期时间确实延后，否则整轮标记失败。
- 每台服务器均检查 `/api/user/servers/{uuidShort}/resources`，未处于 running/starting 时请求 `/power/start`，再复查状态；即使续期失败也尝试启动。
- 2026-11-26 00:05 UTC 用户手动续期后，531 到期变为 11-29 00:05:01，续期窗口 11-27 12:05:01 开启，耗费 1 credit，期限 3 天。00:08 UTC 查询为 running。
- **自动续期 POST 在窗口关闭时无法实测**；目前请求体为空，尚未由浏览器请求体或真实自动续期验证。首次符合窗口运行时须查看日志，若未推进到期时间会标记失败。
- GitHub Actions 定时触发存在延迟，不保证恰好到窗口即运行。`--probe` 仅读信息，不续期、不启动、不挂机。
