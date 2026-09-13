# Cobra 真实缓存盘后只读复核 — 2026-09-13

## 身份与边界

- 主机：`cobra-ion`
- 远端时间：`2026-09-13T22:58:00+08:00`（命令执行约 `23:00`）
- 运行环境：`/home/exedev/services/engine-next/shared/venv/bin/python`
- 模式：历史日期、有界、只读探针
- 未执行：Redis/TD 写入、Rabbit 消费或 ACK、repair/recovery、通知和报告发送

探针先读取 Redis key type，再使用 `HLEN/HSCAN/GET` 等只读命令；输出文件位于
`/tmp`，不作为 runtime cache 或生产输入。

## 执行命令

```text
PYTHONPATH=/tmp/engine-core-worktree-20260913/src \
/home/exedev/services/engine-next/shared/venv/bin/python \
examples/run_real_cache_inventory.py \
  --trade-date 2026-09-10 \
  --previous-trade-date 2026-09-09 \
  --output /tmp/core-cache-inventory-20260913.json

PYTHONPATH=/tmp/engine-core-worktree-20260913/src \
/home/exedev/services/engine-next/shared/venv/bin/python \
examples/run_real_redis_td_projection_compare.py \
  --trade-date 2026-09-10 \
  --symbols 600519,000001,000002 \
  --output /tmp/core-projection-compare-20260913.json
```

## 观察结果

历史日期缓存仍可读到，并且扫描前后长度一致：

| Redis key | rows | date check | metadata limitation |
| --- | ---: | --- | --- |
| `cache:chip_peaks:2026-09-10` | 5225 | 5225 条日期一致 | 未作为本轮 runtime 输入 |
| `cache:stock_extra:2026-09-10` | 5200 | 5200 条日期一致 | `real_market_cap` 单位仍按既有合同审计 |
| `cache:hot_plates:2026-09-10` | 50 | 50 条日期一致 | `schema_version/available_at_ms/field_units` 缺失 |
| `cache:hot_rank:2026-09-10` | 100 | 100 条日期一致 | `schema_version/available_at_ms` 缺失 |
| `cache:yest_limit_pool:2026-09-09` | 48 | 48 条日期一致 | `schema_version/available_at_ms/field_units` 缺失 |

`config:plate_mapping:info` 与 `config:plate_mapping:full_sync_info` 当前不存在；
`market:stock_plate` 与 `market:stock_reason` 是非 JSON hash，不能按日期 JSON payload
解释。上述都只是 dialect/结构观察，不代表 core 可消费。

竞价投影比较结果：

```text
symbols: 600519, 000001, 000002
tags: 0920, 0924, 0925
TD rows: 9
MATCH: 0
MISMATCH: 0
NOT_COMPARABLE: 9
reason: redis_top_amount_unavailable
```

artifact SHA-256：

```text
core-cache-inventory-20260913.json
a28ab71f3c26182c4c59eb34f5b7632e2a0aedff71e9fc40494ef856bba3873b

core-projection-compare-20260913.json
dff93870701235e624733f506d3f8e0e20feb2b39506e6ba81c34be3564241b1
```

## 结论

```text
历史缓存结构可扫描                         PASS（观察性证据）
历史缓存可直接作为 replay/runtime 输入       NOT PROVEN
Redis ↔ TD 竞价 projection 等价              NOT_COMPARABLE
Redis/TD writer 一致性                       UNKNOWN
历史 available_at                            UNKNOWN
```

“缓存存在、日期一致、扫描稳定”不等于“历史时点可知”，也不等于“Redis 与 TD
是同一份竞价状态”。因此本证据不能提升 M0/M1/M2 状态，也不能解除 hot plates、
昨日涨停池或竞价 full projection 的阻塞。
