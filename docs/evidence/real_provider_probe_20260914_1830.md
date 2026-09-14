# Cobra-ion 真实 Provider 只读复验（2026-09-14 18:30 CST）

## 执行边界

- 运行目录：`/home/exedev/validation/engine-core-8ed642b`（代码归档；本次仅使用现有只读 runner）。
- 生产服务 `engine-next`、`t1-v2-live` 未重启。
- 未新增 Rabbit consumer，未改变 ACK，未写 Redis/TD，未执行 repair、通知或 effect。
- 目标标的：`000001,000002,600519`；Q2 为真实当前 cohort（全量读取 5220 个 symbol）。

## Redis Q2

- artifact：`/home/exedev/validation/real-q2-probe-20260914-1830.json`
- SHA-256：`d4b2b4df34fb9c919096ffadff20edba4bd8b1fee88871c6a17f926dccef48e`
- rows：5220/5220，coverage=1.0。
- 状态：`STALE`，stale symbols=5220；`newest_source_time_ms=1789369205000`，观测时刻约为 18:32 CST，source lag 约 3.5 小时。
- Q2 semantic/projection hash：`9f9ef963acd6ad1abc62f577a721935f13e30951444ddcf7eca30cd42d4e89a4`。
- 两次 Engine 计算的 probe/snapshot hash 相同，证明同一真实观察可确定性重算；不证明盘中 freshness 或 universe authority。
- `source_record_time_ms` 仅作为源记录时间、freshness 和 future-skew 证据，不解释为 Rabbit arrival 或交易所逐笔顺序。

## TD PreviousDayStats

- artifact：`/home/exedev/validation/real-previous-day-stats-20260914-1830.json`
- SHA-256：`7e6d5a124426db12719bafa34217b1d28ba7e92dfe3e8ef3c0a06673bcf78fb4`
- 请求交易日：`2026-09-14`；日历派生上一交易日：`2026-09-11`；返回 3 行。
- close/amount 数据真实可读，但 `available_at_ms=null`，缺失 `available_at_unknown`，结果按时间合同为 `UNAVAILABLE`。
- `observed_at_ms` 只记录本次查询，不反推历史知识可见性。

## Redis/TD Auction Projection

- artifact：`/home/exedev/validation/real-redis-td-compare-20260914-1830.json`
- SHA-256：`8f083c427c75c8c8e6b540fd1136a965fc2d29cb113a87803ccf9214167e1e4e`
- 9 个 symbol/tag 对照：`mismatch=0`、`partial_comparable=5`、`not_comparable=4`、`match=0`。
- 可比较的 match amount、rest bid 和 source timestamp 均未发现数值冲突；Redis Top-200 窗口外标的保持 `NOT_COMPARABLE`，缺失 ask 字段保持 `NOT_COMPARABLE`，没有用 0 或 TD 值补齐。
- compare semantic hash：`22ad3344fbf2723aabf52f5fdf75951603df2813f9fce0d8018793034102351f`。

## 结论

```text
真实 Redis Q2 读取             PASS（但全量 STALE）
真实 TD PreviousDayStats       READ PASS / runtime UNAVAILABLE
Redis-TD 可比较字段            no mismatch；整体 PARTIAL/NOT_COMPARABLE
Core 替代 engine-next          NOT READY
```

该复验强化了真实 Provider 与 fail-closed 证据，但不能替代下一交易日 in-session Shadow，也不能证明 Rabbit batch、AuctionState freeze、engine-next 完整消费链或 Core 生产替代。
