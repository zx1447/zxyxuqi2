# ggcart（独立分支）

GGCart 免费服（Community Free，2 GB）精准续期。

- `ggcart_renew.py`：真浏览器（Playwright）登录 → 读到期时间 → 仅在到期前 48 小时内点击 Renew weekly → 用到期日变化核对。
- 运行入口在 main：`.github/workflows/ggcart_renew.yml`（checkout `ref: ggcart`）。
- 凭据走 secrets：`GG_EMAIL`、`GG_PASS`。
