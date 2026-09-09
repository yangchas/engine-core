# engine_next / engine_core 真实数据交接

> 只读审计交接。本文不改变生产代码、生产配置或部署状态。

## 当前版本

### 生产服务

| 服务 | 状态 | 运行版本 |
|---|---|---|
| `engine-next.service` | active，`NRestarts=0` | `/home/exedev/services/engine-next/releases/20260903_e272842`，`e272842c8f490f55a1b017badb71e71904ce008e` |
| `t1-v2-live.service` | active，`NRestarts=0` | `/home/exedev/services/t1-v2/releases/20260903_6fb3164`，`6fb3164baab00d840886da5f056587ec32f3d86a` |

服务器 2026-09-09 早间检查：两个服务均正常，根分区约 73% 使用率，剩余约 4.8 GB。

生产 release 没有 `.git` 元数据。本地主仓库 `codex/engine-core-v0.3.1` 的 HEAD 为 `67822fdd...`，与生产工作区存在大量差异；`engine_next`/`C/t1_v2` 当前有未提交修改。因此生产行为必须以服务器 `current/content/source` 或上述两个 release commit 为准，不能以本地脏工作区推断生产语义。

### engine_core

独立仓库当前分支：`codex/feature-session-engine-integration`。

当前已验证 commit：`3fc07bd77d2a361bbd269978b4ee45040497b308`，已完成 EvaluationPlan 与 deterministic engine 集成、真实数据探针的时间权威分级，共有 154 项默认离线测试通过。该 commit 已部署到 cobra-ion 的隔离验证目录 `/home/exedev/validation/engine-core-3fc07bd` 并以 Python 3.12.3 运行同一套 154 项测试；它没有部署为生产服务，也不接管 Rabbit、t1-v2、TD/Redis 写入或正式 effect。

证据：

- `engine_core/docs/evidence/engine_integration_audit.md`
- `engine_core/docs/evidence/engine_integration_correctness_20260904.md`
- `engine_core/docs/evidence/engine_integration_live_q2_smoke_20260904.md`
- `engine_core/docs/evidence/q2frame_replay_audit_20260904.md`
- `engine_core/docs/evidence/td_event_time_replay_20260904.md`

## t1-v2 原生 Redis 数据

### Q2

```text
q2:{symbol}                  Redis Hash
q2:active:{YYYYMMDD}         当日活跃 symbol Set
```

已确认或可安全使用的字段语义：

- `px/pc/mx/mn`：milli-price。
- `amt/ia/ln/amt2m/amt5m`：金额或金额增量，单位为元。
- `spd1m/vec3m/vec5m`：basis points。
- `a20/a24/a25`：竞价节点价格，milli-price。
- `am/br/ar`：竞价金额及未匹配盘口金额代理，整数元。
- `ts`：source record timestamp ms。已观察到与 TD `stock_tick_v2.ts` 对齐，但没有证据证明它是交易所逐笔时间或 Rabbit arrival time。
- `ph/ls/mk`：阶段、涨跌停状态、市场。

`vol` 的累计量单位已经由生产 Q2 的量纲交叉证据确认是手（board lots）。2026-09-09 对 `000001/300750/600519` 的真实 Q2 读取中，`amt / (vol * 100)` 与当前价格比值分别约为 `1.0015/1.0005/1.0036`，而按股解释会得到约 100 倍价格；因此生产头文件和 converter 中的 shares 注释是陈旧注释，canonical 字段保持 `volume_lots`。`iv` 未进入当前 Wheel 的业务字段，仍不因 `vol` 的结论被自动升级。

Q2 是按股票滚动更新的 projection cohort，不是同一时刻的全市场快照。读取时必须保存 coverage、stale 数、`oldest_source_time_ms`、`newest_source_time_ms`；coverage 100% 不能自动代表 freshness 或 completeness READY。

### A2 与竞价快照

```text
a2:{YYYYMMDD}:{0920|0924|0925|latest}
market:auction:{YYYYMMDD}:0920
market:auction:{YYYYMMDD}:0924
market:auction:{YYYYMMDD}:0925
market:auction:{YYYYMMDD}:latest
market:auction:anchor:{YYYYMMDD}
```

`a2` 和 legacy `0920/0924/0925` 主要是 TopN/summary projection，不是完整市场集合；`market:auction:anchor:{date}` 是 0925 后的候选归档，通常比 TopN 完整，但仍只包含满足候选条件的股票。

当前生产 SnapshotTrigger 阈值为：

```text
a20: logical time >= 09:20:03 and < 09:24
a24: logical time >= 09:24:10 and < 09:25
a25: logical time >= 09:25:06 and < 09:30
```

`engine_next` 的 0925 finalize 最早为 09:25:10。它们是当前生产实现，不代表已经完成 Tick Shape Audit 后的最终业务合同。

代码证据：`C/t1_v2/redis_v2_writer.cpp`、`C/t1_v2/snapshot_trigger.cpp`、`C/t1_v2/auction_calculator.cpp`、`C/t1_v2/config_v2.h`。

## 非原生 Redis 数据源

### BaoStock

- `availability_check(now, target_date, previous_trade_date)`。
- `fetch_daily_kline(request)`。
- `fetch_daily_kline_range(symbol, start_date, end_date)`。
- 支持显式日期；当前用途为日线和上一交易日数据。
- 旧连接/session、登录重试和 `adjustflag=3` 语义应优先抽取，不重新设计连接层。

### Kaipan

- `fetch_hot_plates(trade_date)`：显式历史日期。
- `fetch_today_hot_plates()`：空日期的当日接口，不等价于历史日期查询。
- `fetch_yesterday_bans_pool(trade_date, max_ban)`：显式日期。
- `fetch_ban_reasons(symbol)`：接口本身没有结构化日期，返回结果必须校验来源日期后才能用于历史语义。

### Wencai

- `fetch_dataframe(WencaiQuerySpec)`：日期只能通过已验证的 query 文本表达，当前没有统一结构化日期参数。
- `fetch_broken_boards()`、`fetch_first_failed()`、`fetch_limitup_with_lb_days()`：当前实现偏向当日/事后真值，历史可见性不能凭今日可查询结果推断。

### THS

- `fetch_hot_rank(top_n)`：无日期参数，作为当日低频关注度代理，按交易日缓存。

2026-09-09 已在 cobra-ion 通过生产 `engine_next` release 的真实连接路径逐一执行有界只读探针：BaoStock 日线、开盘啦热板块/昨日涨停池/涨停原因、问财涨停真值、THS 热榜均连接成功。只有 BaoStock 日线同时闭合了请求日期与响应日期；开盘啦部分接口只证明请求日期或当前响应，问财和 THS 当前接口没有结构化历史日期。因此后五者的“连接成功”不能升级成历史 replay 时点的数据可用性证明。

## 补齐、读取和持久化 owner

- `IntradayDataHub.recover_auction_anchor()` 的链路是 Redis full anchor → Redis 0925 → legacy preview → TD → Wencai；成功恢复可能回写 Redis anchor，因此不是纯读取函数。
- 盘中晚启动且 anchor 缺失时，`EngineApp._ensure_late_start_auction_recovery()` 在 09:30–15:00 每交易日最多尝试一次 0926 follow-up。
- `IntegratedSyncExecutor.sync_pipeline()` 负责日线、因子、筹码、DDE 的增量同步、gap-fill、checkpoint 和持久化。
- `IntradayContextBuilder._ensure_hot_rank_cache()` 按阶段 freshness 判断是否刷新 THS 缓存。
- t1-v2 负责原始 Tick/Q2/A2/auction state 到 Redis/TD 的生产写入；engine_next 主要读取 Redis view 并调用 TD/网络源补齐。

需要特别验证：晚启动恢复首次失败后，同一进程是否允许再次尝试；恢复分支缺失 RB/RA 时是否如实返回 PARTIAL/UNAVAILABLE，而不是把不完整结果标成完整 anchor。

## 关键时间动作与输出 owner

```text
08:30 / 09:00       startup audit checkpoints
09:25:10            auction_finalize_0925
09:26                auction_followup_0926
15:05                market_close_1505
17:40                postmarket_settlement_1740
```

- 启动自检：`StartupBootstrapController` + `RuntimeStartupCoordinator`。
- 竞价：`AuctionRuntimeController`。
- 盘中/开盘：`LiveRuntimeController` + `IntradayContextBuilder`。
- 收盘/盘后：`PostmarketRuntimeController` + `SettlementController`。
- 报告渲染：`EngineApp.run()` / `run_forever()`。
- SMTP/Webhook：`NotificationService`。
- 通知去重：内存 digest + Redis digest，TTL 2 天；历史 replay 和非实时交易日请求被拒绝。

## Replay 现状

`engine_next` 已有历史请求识别、Q2Frame fixture 注入，并跳过历史 replay 的收盘/结算外部动作。

`engine_core` 已有：

- `VirtualClock`。
- `Q2FrameReplaySource` 与 Q2Frame replay。
- `TDEventTimeReplaySource` 与 TD event-time replay。
- 相同 Engine 计算链供 fixture、Q2Frame、TD event-time 使用。

限制仍然明确：

```text
没有 REPLAY_RECORDED
不恢复 Rabbit arrival/batch
没有 watermark、late correction、checkpoint、effect
TD 同 timestamp/symbol 的 hash tie-break 只是 synthetic ordering
```

## 最小真实接入建议

1. 先做薄的 Redis Q2 只读适配：`q2:active:{date}` → pipeline 读取 `q2:{symbol}` → normalize/validate → CurrentMarketState；保留 cohort 时间范围和质量状态。
2. 沿生产 TD 连接和 SQL 语义抽取 `TDPreviousDayStatsProvider`，由 `PreviousDayStatsFunction` 和 `TemporalDataGuard` 统一日期与可见性判断。
3. 竞价只读读取 Redis 0920/0924、anchor 或 TD `auction_snapshot_v2`；只比较 authority 已证明相同的 shared fields，TopN 不冒充全市场。
4. 先让一条真实 09:20 → 09:24 Auction Shadow 规则跑通逐字段 trace 和 differential；再扩大字段和 Provider。

当前最重要的阻塞项是：

```text
生产 Q2 在交易时段持续出现约 3--8 分钟 source-time 延迟，engine_next 的真实日志同时给出 `live_quote_ready=False`；
t1-v2 虽与 Rabbit、Redis、TD 均保持 ESTABLISHED，但当前运行时没有持续批次/ACK/commit counters，无法定位延迟首次发生于 Rabbit、decode、批处理还是 writer；
生产 release 6fb3164 与本地脏工作区 auction_calculator 公式存在差异，必须以生产 release 为 oracle。
```

针对上述可观测缺口，已从生产精确基线 `6fb3164...` 建立独立分支
`codex/fix-t1-live-observability`，提交 `9fd4a42b3f3944235da89e1ae2278ea93cff193c`。
它仅记录累计 batch/source/decode/ACK/Redis/TD 指标、最近一批 pipeline/commit/ACK
耗时及 wall lag；cobra-ion 隔离候选二进制已用完整生产依赖编译并通过 self-test。
该提交尚未部署到生产，当前仍不能据此判断 decode、TD、Redis 或 ACK 哪一步最慢。

安全边界：不接管 Rabbit、不改 ACK、不新增 consumer、不写 Redis/TD、不在 replay 中启用 SMTP、下单或其他 effect。
