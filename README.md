# ggcart（独立分支）

GGCart 免费服（Community Free，2 GB）精准续期。纯 HTTP，不用浏览器。

- `ggcart_renew.py`：登录（Supabase）→ 读 Community claim 到期时间 → 仅到期前 48 小时内发续期请求 → 重新读取，到期日变晚才算成功。
- 运行入口在 main：`.github/workflows/ggcart_renew.yml`（每天 06:20 UTC 一次，即到期时刻；窗口 48 小时内会有两次检查）。
- 凭据走 secrets：`GG_EMAIL`、`GG_PASS`。
