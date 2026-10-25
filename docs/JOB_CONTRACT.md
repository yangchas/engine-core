# 子作业返回约定

## 现状

`prep_trade_day._run` 直接启动 `run_data_jobs.py`、`capture_ths_hot_rank.py`、`build_auction_baseline.py` 和 `build_plate_amount_baseline.py`；guardian 通过 `core_guardian.py` 启动业务脚本，并由 `guardian/startup.py` 按 `datasets.registry.json` 启动动态取数脚本。表内“正常退出码”列是脚本显式返回值；未捕获异常通常为 1，argparse 参数错误为 2。

| 脚本 | 退出码取值 | stdout 形式 |
| --- | --- | --- |
| `examples/prep_trade_day.py` | 0（READY/PARTIAL），1（其他报告状态） | 多行 JSON readiness 报告 |
| `examples/run_data_jobs.py` | 0（正常处理，包括部分 provider unavailable 输出），未捕获异常 1 | 作业相关 JSON 行；`plate_members` 可输出多行及末尾汇总行；未统一字段 |
| `examples/capture_daily_kline_degraded.py` | 0（委托 degraded 日 K 脚本显式返回 0），未捕获异常 1 | 单行 JSON |
| `examples/capture_ths_hot_rank.py` | 0（正常），未捕获异常 1 | 多行 `CAPTURED_AT`、`TRADE_DATE`、`ROW_COUNT` 等键值文本 |
| `examples/build_auction_baseline.py` | 0（READY/PARTIAL），2（其他状态） | 单行 JSON |
| `examples/build_plate_amount_baseline.py` | 0（READY/PARTIAL），2（其他状态） | 单行 JSON |
| `examples/core_data_daily.py` | 0（READY/SKIPPED_NON_TRADING_DAY），2（其他状态） | 单行 JSON |
| `examples/run_daily_reports.py` | 0（正常），未捕获异常 1 | `daily_pipeline` 输出 `REPORT_TIMINGS ...` 和 `trade_date=...` 文本行 |
| `examples/capture_kaipan_plates.py` | 0（正常或跳过），未捕获异常 1 | 多行 `CAPTURED`、`SKIPPED_EXISTING` 或 `SKIPPED_DATE_MISMATCH` 文本 |
| `examples/backfill_daily_history.py` | 0（完成；指标重建分支也为 0），2（未完成） | 单行 JSON |
| `examples/build_daily_report_features.py` | 0（正常），未捕获异常 1 | 单行 JSON |
| `examples/fetch_auction_item.py` | 0（返回 payload，包括 degraded），未捕获异常 1 | 单行 JSON |
| `examples/core_data_backfill.py` | 0（完成），2（仍缺数据或对账不匹配） | 单行 JSON |
| `examples/fetch_registry_item.py` | 0（status=ok），1（其他状态或未捕获异常） | 单行 JSON |
| `examples/guardian_handler.py` | 0（handler 已返回），64（配置错误），75（可重试失败），外围异常 1 | 单行 `contract: 1` JSON |
| `CORE_BAOSTOCK_PROBE_COMMAND`（可配置外部命令） | 外部命令自定；guardian 按非零退出处理 | 外部命令自定 |

以上脚本的 argparse 错误通常退出 2。Guardian handler 自身使用 `contract: 1` JSON 行，字段词汇为 `status`（`ok`、`degraded`、`not_applicable`、`failed`）、`error_code`、`retryable`、`artifacts`、`detail`；0 表示 handler 已返回（仍需检查业务后置条件），64 表示配置错误，75 表示可重试失败。此 handler 协议保持原义。

## `contract: 1` 子作业返回行

`engine_core.data_pipeline.job_contract` 是无服务副作用的纯 Python 模块。`emit(result)` 将一个 `JobResult` 写成 stdout 最后一行 JSON，并返回固定退出码：

| status | retryable | 退出码 |
| --- | --- | ---: |
| `ok` | false | 0 |
| `degraded` | false | 10 |
| `not_applicable` | false | 20 |
| `failed` | true | 30 |
| `failed` | false | 40 |

64 和 75 保留给 guardian handler，不由子作业约定占用。返回行字段为 `contract`（固定 1）、`job`、`trade_date`、`status`、`tier`、`source`、`dataset`、`version`、`row_count`、`coverage`、`error_code`、`retryable`、`attempts`、`elapsed_ms`、`detail`。`parse(stdout_text, returncode)` 校验最后一行 JSON 与退出码；缺行、结构无效或二者不一致会返回 `failed`、`CONTRACT_VIOLATION`、不可重试。非约定旧退出码会映射为可重试 `failed`，错误码为 `LEGACY_EXIT_<码>`。

`run_with_degrade` 按 `(tier, source, fetch_callable)` 顺序尝试各层。每层共用同一组结果字段；`tier` 和单一真实 `source` 标识实际命中层，失败记录在 `detail.failures`。异常或质量校验失败会继续尝试下一层；全部失败时返回统一的 `failed` 结果。

`symbol_universe` 的 tier 0 使用 Baostock `query_all_stock`；tier 1 从目标日前五个交易日范围内选最近已发布且可读的 universe，原样顺延到目标日，写入独立 `source=carried_forward` 目录。顺延 manifest 带 `carried_forward_from`、`universe_asof`、`tier=1`、`degraded=true`、`quality.status=DEGRADED` 和非 `FORMAL` 的 `business_status`。`prep_trade_day` 将退出码 10 报为 `PARTIAL`（`tier=1 carried_forward`），不增加错误；退出码 20 报为跳过。

后续 `prep_trade_day --write` 遇到仍有效的 `tier>=1` 降级版本时，会再尝试一次 symbol 子作业。发布 tier 0 后，该步骤报告 `READY`；再次返回降级结果时，保持 `PARTIAL`，相同内容按同一版本幂等；主来源尝试失败但原降级版本仍有效时，不报 `COMMAND_FAILED`。同日同时有 tier 0 与降级版本时，`ArtifactCatalog.locate` 和 `data_pipeline.access.get` 都优先读 tier 0；其他数据集的选版规则不变。
