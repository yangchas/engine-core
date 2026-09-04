# Gate K inventory

本文件是 engine_core 首次实现的只读审计索引，不是旧项目运行时依赖。

## 已确认的外部边界

| 能力 | 当前证据 | 新项目处理 |
| --- | --- | --- |
| Rabbit 消费与 ACK | D:/work/Go/C/t1_v2/rabbitmq_tick_source.cpp | 外部 KEEP_BEHAVIOR，首期不接管 |
| t1_v2 批处理 | D:/work/Go/C/t1_v2/runtime_loop.cpp、runtime_pipeline.cpp | 外部 KEEP_BEHAVIOR |
| Q2 Redis 投影 | D:/work/Go/engine_next/runtime/intraday_data_hub.py | WRAP 为 L2_PROJECTION_SNAPSHOT |
| Q2Frame | D:/work/Go/C/t1_v2/q2frame_command_executor.cpp | 目标 Replay Adapter，当前未接入 |
| TD Tick Replay | D:/work/Go/C/t1_v2/td_replay_tick_source.cpp | 目标 Event-Time Replay Adapter，当前未接入 |
| 3 秒轮询语义 | D:/work/Go/engine_next/README.md | 作为当前 vertical-slice 输入节奏 |

## Q2 字段 mapping（当前已观察）

| 旧字段 | 新边界字段 | 当前单位状态 |
| --- | --- | --- |
| px | price_milli | 已观察为 milli price |
| pc | pre_close_milli | 已观察为 milli price |
| amt | amount_native | 累计元，producer evidence 已确认 |
| vol | volume_native | 累计股数，producer evidence 已确认 |
| ts | source_timestamp_ms | 仅接受合法 epoch 秒/毫秒 |
| ph | phase | 已观察为 phase code |
| ls | limit_state | 已观察为 limit state code |
| am/br/ar | auction amount fields | 元金额；br/ar 为二档盘口换算，producer evidence 已确认 |
| amt2m | amount_2m_yuan | 累计金额差（元），producer evidence 已确认 |
| spd1m | speed_1m_bp | 已观察为 bp-scaled integer |

## 当前 UNKNOWN

- Q2 是否真实提供全局 generation。
- Q2 `ts` 的精确定义和 active cohort 的跨日更新顺序。
- Q2 hash 是否由同一时刻 atomic cohort 写入。
- active symbol 集合与 q2 hash 的更新先后。
- TD replay 是否保留生产 arrival order。
- 旧生产链是否在当前部署版本启用 TD 写入。

## Vertical Slice 验收范围

本次首提交只证明：

    Q2 projection fixture
    -> adapter
    -> current market state
    -> one half-open window
    -> engine snapshot
    -> empty frozen data bundle
    -> probe strategy
    -> deterministic trace

不证明：

- RabbitMQ 原始 batch 等价。
- 生产端到端零丢失。
- Q2 同刻全市场一致性。
- 正式竞价或开盘策略已迁移。
