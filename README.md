# nodarooAFK（独立分支）

Nodaroo Free AFK 挂机的代码与登录态。**本分支只放这一个站点的内容**，与其它站点互不相干。

- `nodaroo_afk.py`：纯 Python 直连挂机（requests + websockets），无浏览器。
- `nodaroo_state.json`：加密登录态（由 workflow 每轮自动提交回本分支）。

调度与运行入口在 main 分支：`.github/workflows/nodaroo_afk.yml`（checkout `ref: nodarooAFK`）。
