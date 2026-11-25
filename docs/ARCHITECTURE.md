# 系统生产架构与运维手册 (`v1.0`)

> 更新日期：2026-11-25（对齐主干 `main` 与生产 `config_version=53`）

## 1. 线上服务与目录拓扑

生产机仅保留 **2 个核心常驻服务**（老 `engine-next.service` 已停用并禁用）：

| 服务 | 管理方式 | 运行路径 | 职责 |
|---|---|---|---|
| **`t1-v2-live.service`** | systemd | `/home/exedev/services/t1-v2/current/content/bin/t1_v2` | 由 `native/tickd/` 编译发布，实时写 Redis `q2:*`（含 `b1a/a1a`）与 TD `stock_tick_v2` |
| **`guardiand`** | `supervisor.sh` + `@reboot keepalive.sh` | `/home/exedev/core-data/guardian-native/guardiand` | 读取 `config.json`，调用 `/home/exedev/repos/engine_core-live` 执行全天 117 个编排事件 |

关键目录边界：
- `/home/exedev/repos/engine_core`：开发与发布主仓（唯一主干 `main`）
- `/home/exedev/repos/engine_core-live`：生产只读工作树（`detached HEAD`，发布时从主仓 fast-sync）
- `/home/exedev/core-data/`：生产运行数据根（`guardian-native/config.json`、`reports/<date>/`、`secrets/wencai.cookie`）

## 2. 全天工作流编排（`guardiand` 12 阶段）

```text
NIGHT (00:00-08:00)      → 日历刷新、昨日涨停池/原因/DDE、盘后回补（cost=heavy）
PRE_MARKET (08:00-09:15) → startup_0830/0900、preopen_check_0835、09:10 重型任务硬截断（heavy_cutoffs）
AUCTION (09:15-09:25)    → 竞价监控、09:20/09:24 截点检查、开盘啦热板抓取
AUCTION_END (09:25-09:30)→ 09:25 截点检查、auction_seal_facts_0926、09:26 竞价 V3 报告（邮件 #1）
OPEN (09:30-09:45)       → 09:35 开盘初验（邮件 #2）、09:45 开盘终验（邮件 #3）、高频热板与涨停池
INTRADAY (09:45-14:57)   → 定时热板与 limit_up_pool_today（实时 b1a/a1a 真封单）
CLOSE (15:00-15:30)      → 15:05 日K收盘链、15:20 涨停池终局（Redis + 问财双确认）
POST_MARKET (15:30-24:00)→ 17:40 baostock_probe → adjust_factor → price_volume → chip_peak & 收盘复盘（邮件 #4）
```

## 3. 容错、退避与重型任务冲突保护

- **开机初始化**：5 个 `startup` 事件（`boot_trade_day` → `boot_data_health` → `boot_reconcile` → `boot_apply_plan` → `fetch_item`）。
- **门禁与 DAG**：9 种 `gate`（`calendar_ready` / `snapshot_092x_ready` / `daily_kline_ready` 等）+ 11 条 `depends_on`（支持 `accept_degraded: true`）。
- **迟到回补与退避**：`catchup=run_in_window` + `late_until` + 指数退避（`backoff: 5s*2 / 60s*2`）+ `silent_until_last`。
- **盘后重型退让**：`cost=heavy` 任务受 `heavy_max_load5=3.0`、`heavy_load_defer_max_polls=6`、`on_phase_mismatch=defer` 与 `PRE_MARKET 09:10` 硬截断保护，绝不抢占竞价与盘中资源。
- **2 级 `ladder` 降级**：`auction_report_0926`、`open_report_0935/0945`、`limit_up_today_1010/1125/1430` 均配置 `rung 0 (primary, require_live_data=true) → rung 1 (degrade_requested=true)`，平时零外部降级开销，`tickd` 断源时自动无感接管。

## 4. 邮件投递与收件人隔离

- **正式报告（4 封）**：由 `config.json` 中对应事件的 `args.email_to` 控制（当前配置为 3 位正式收件人）。
- **测试 / 模拟 / 告警**：`commands.py::_report_email_recipients` 在 `simulate_clock` 模式下强制只发管理员单人邮箱；`Core [ALERT]` 与夜间报告同样硬隔离为管理员单人邮箱。

## 5. 发布标准流程

1. 在 `engine_core` 主仓独立分支开发、跑通单测并合入 `main`；
2. 同步生产树：`git -C /home/exedev/repos/engine_core-live fetch /home/exedev/repos/engine_core <HEAD> && git -C /home/exedev/repos/engine_core-live checkout --detach <HEAD>`；
3. 推送 GitHub：`git -C /home/exedev/repos/engine_core push origin main`；
4. 若修改了 `config.json`，先跑 `guardiand validate`，确认 `VALID` 后 `kill -TERM $(pgrep -x guardiand | head -n 1)` 平滑重启加载。
