# TD Event-Time Replay — 2026-09-04

## Contract

本阶段只实现：

```text
TD stock_tick_v2 rows
→ TDEventV1 normalization
→ event-time + symbol + raw-content stable order
→ 3s half-open EVENT_SLICE
→ VirtualClock
→ per-event MARKET_UPDATE signal
→ existing DeterministicEngine
```

`EVENT_SLICE` 只是 transport/scheduling batch，保存全部逐条事件，不做 OHLC、Feature 或策略预聚合。

明确不支持：

- Rabbit arrival/batch order recovery；
- watermark、late correction；
- REPLAY_RECORDED；
- checkpoint 或外部写入。

## Local verification

- `python -m pytest -q`: **80 passed**
- `python -m compileall -q src tests`: **PASS**
- `git diff --check`: **PASS**

覆盖：

- 乱序输入得到相同 slice/event hash；
- 同时间同代码使用保留原始字段内容 hash 稳定排序；
- 事件不被提前聚合；
- VirtualClock 只按事件时间推进；
- 同一 Engine 逐事件处理；
- 缺字段、跨交易日、越过 slice anchor、未知 symbol 均 fail-closed。

## cobra-ion read-only smoke

- 远端临时副本：`/home/exedev/tmp/engine_core_validation_20260904_1244`
- Python：server venv Python 3.12.3
- 源：`market_data1.stock_tick_v2`
- 查询：2026-09-03 09:20:00（含）至 09:20:09（不含），symbol 过滤 `000001`,`600519`
- 真实读取行数：`3`
- 实际返回 symbol：`600519`
- 3 秒 slice 数：`3`
- 每 slice 事件数：`[1, 1, 1]`
- Engine processed signals：`3`
- reducer revision：`3`
- final logical time：`1788398408000`
- VirtualClock：`2026-09-03T01:20:08+00:00`
- expected symbol coverage：`0.5`
- completeness：`PARTIAL`
- 写入：`False`

该结果确认真实 TD 行可以通过最小 event-time source 进入同一个 Engine；由于查询窗口内没有 `000001`，结果正确保持 PARTIAL，不能当作全市场完整回放。

## Equivalence boundary

本阶段是 `DETERMINISTIC_EVENT_TIME_ONLY`。TD 保存的事件时间和行字段被稳定消费，但 TD 未保存 Rabbit arrival order、原始 batch boundary 或批内生产顺序，因此不宣称生产批处理等价。
