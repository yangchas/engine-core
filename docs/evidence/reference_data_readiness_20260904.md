# Reference Data Readiness Smoke — 2026-09-04

## Scope

验证最小节点前参考数据闭环，不接管生产链路：

```text
TD read-only query
→ TDPreviousDayStatsProvider
→ PreviousDayStatsFunction
→ TemporalDataGuard
→ ReadyDataStore
→ FrozenDataBundle
```

## Local verification

- `python -m pytest -q`: **75 passed**
- `python -m compileall -q src tests`: **PASS**
- `git diff --check`: **PASS**

覆盖的行为：

- `available_at_ms=None` 不再被自动解释为不可用；
- `observed_at_ms <= knowledge_as_of_ms` 的预观察结果可以 READY；
- `observed_at_ms > knowledge_as_of_ms` 仍返回 `UNAVAILABLE`；
- ReadyDataStore 只保存 READY 结果，读取时重新执行 TemporalDataGuard；
- 缓存结果不填充或改写 `available_at_ms`；
- 节点前结果可构造完整 `FrozenDataBundle`。

## cobra-ion read-only smoke

- 远端临时副本：`/home/exedev/tmp/engine_core_validation_20260904_1244`
- Python：server venv Python 3.12.3
- 源：既有 TDengine `taos` 只读连接，`market_data1.daily_kline`
- 查询日期：`2026-09-03`
- symbols：`000001`, `600519`
- `observed_at_ms`：`1788512897196`（本次真实查询时刻）
- `knowledge_as_of_ms`：与本次观察时刻相同
- result：`READY`
- row count：`2`
- `available_at_ms`：`None`
- store size：`1`
- cached result：`READY`
- FrozenDataBundle completeness：`1.0`
- 写入：`False`（未写 Redis、TD、Rabbit，也未改变生产进程）

该 smoke 使用“当前真实查询 + 当前观察时刻作为 cutoff”证明 pre-observed 语义，**不宣称它证明了历史 09:20 的可用性**。历史节点验收仍需在下一交易时段 09:20 前执行一次有界只读预取，并保存当时的观察证据。

## Boundary

```text
查询到历史数据
!=
证明历史 source available_at
```

本阶段只使用 `observed_at_ms` 作为进程已获得数据的证据；未知的上游首次发布时间保持 `None/UNKNOWN`。ReadyDataStore 是进程内最小字典，不是 DataCatalog、authoritative journal 或 checkpoint。
