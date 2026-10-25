# Daily K-line

## 现状

### 写入路径

| commit 路径 | `source` 标签 | 权威 `provider` 与尝试记录 | 状态与时间 |
|---|---|---|---|
| `data_pipeline/providers/daily_kline_chain.py:147`（Q2 快照） | 保留 `legacy_q2_final` | `q2_final`；一次带 `ACCEPTED`、尝试时间、耗时和原因的尝试 | writer 按活跃 universe 重算覆盖率与字段完整度，分类为 `PROVISIONAL`；新时间统一为上海时区 |
| `data_pipeline/providers/daily_kline_chain.py:497`（provider chain） | 保留各 provider 原标签 | 实际胜出 provider；所有尝试按顺序记录。Baostock 另记录 `login_ms`、`query_ms`、调用结果和错误码 | 业务状态由共享分类器决定；Baostock 调用向 `job_log/<日期>.jsonl` 追加 |
| `daily_kline_degraded.py:266` | 保留组合来源目录标签 | 按实际填充字段来源写 `q2_final`/`wencai`；来源无法唯一判定时写 `UNKNOWN`；记录各来源尝试 | 分类器决定业务状态；时间统一为 `+08:00` |
| `data_pipeline/jobs.py` 的 `JobRunner.run` | 保留 job source | 使用 fetch metadata 的 provider/attempts；仅有 Baostock 计时时写计时项；缺少尝试信息时标为 `UNKNOWN`，不伪造尝试时间 | 所有 commit 经 `ArtifactCatalog.commit` 重算质量、分类状态并规范时间 |
| `daily_history.py:512`（历史回填） | `baostock` | 从精确 source 得到 `baostock`；上游没有尝试元数据时记录 `UNKNOWN` 尝试，时间和耗时为空 | 同一 writer 重算质量与业务状态；该旧回填调用未提供逐调用计时 |

上述 catalog commit 共用 `data_pipeline/storage.py:_prepare_daily_kline_manifest`：覆盖率分母取同日 universe 中 `trade_status != "0"` 的股票；缺 universe 时写 `coverage=null`、`expected_basis=universe_unavailable`。它保存 `suspended_in_universe`、`suspended_without_bar` 与八个价量字段完整度，再调用 `classify_daily_kline`。`available_at`、`fetched_at`、`as_of` 和 `provider_attempts[*].attempted_at` 的可解析值统一转成带 `+08:00` 的 ISO 字符串；历史 manifest 不回写。

`examples/capture_daily_kline.py` 写独立 capture manifest，不调用 catalog commit；BaostockClient 在该路径也保存 login/query 计时、结果、错误码和尝试记录。

C541: `DailyKlineProviderChain.fetch_chain` 和 `run_data_jobs.py --job daily_kline` 使用整链墙钟预算，默认读取 `CORE_DAILY_KLINE_BUDGET_SECONDS`，未设置时为 240 秒；`--baostock-budget-fraction` 默认 0.6，显式 `--baostock-total-timeout-seconds` 继续与这部分预算取较小值。Baostock 到预算上限时保存 checkpoint 并返回已完成股票数，随后链路在整链剩余时间内尝试 Wencai、q2_final；下一次同日运行从 checkpoint 续取。非 Baostock 版本的 `provider` 记录实际来源，业务状态为 `PROVISIONAL`。Baostock 尝试记录 `result`、完成/预期股票数和 `checkpoint_saved`；catalog writer 保留这些字段。CLI 降级成功返回 job_contract 退出码 10，全部来源失败返回 30。链路验收和最终 manifest 的覆盖率都以同日活跃 universe 为分母。

`_supersede_prior`（`data_pipeline/providers/daily_kline_chain.py:394-399`）读取既有 `status == "SUPERSEDED"` 以跳过已替代版本；对其他旧版本写入 `status`、`superseded_by` 和 reconciliation 并原子替换旧 manifest。本卡不改其行为。

### 读取方

- `data_pipeline/access.py:41-53,86-100,248-263`：读 manifest/catalog 的 `business_status` 与 `quality.coverage`，选择版本并标注非 FORMAL 结果。
- `data_pipeline/registry/gate.py:112-126`：用 `quality.coverage` 和 `quality.status` 生成数据 gate。
- `guardian/registry.py:22-28,55-64,115-118`、`guardian/commands.py:55-58`：读取 coverage、quality status、business status，判断待升级和后置条件。
- `guardian/gates.py:201-252`：根据 available_at、行数和 universe 行数计算 daily_kline ready/coverage。
- `data_pipeline/ops.py:148,441-471`、`data_pipeline/jobs.py:341`、`examples/run_data_jobs.py:63-65,421`：库存、缓存发布和 job 报告读取 business status、quality status 或 coverage。
- `daily_history.py:563-564`：重建历史前检查 coverage；`data_pipeline/lineage.py:13-18`：把 daily_kline 的 quality status/source 写入派生输入 lineage。
- `slices/__init__.py:602`：将 partial business status 传播为 slice 结果状态。
