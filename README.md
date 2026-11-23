# blinky（独立分支）

Baltic Host（blinky.baltichost.online）续期相关的代码。本分支只放这一个站点。

- `blinky_probe.py`：登录探针，抓面板页面与站点 JS，找续期/启动接口。
- 运行入口在 main 分支：`.github/workflows/blinky_probe.yml`（checkout `ref: blinky`）。
- 凭据走 secrets：`BLINKY_USER`、`BLINKY_PASS`（不写进代码）。
