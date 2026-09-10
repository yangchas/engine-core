# 2026-09-10 pre-open read-only source probe

## Scope

This is a server-side observation, not a production acceptance run.  The
probe used the existing production Redis client and the deployed
`engine_next` connector paths.  It did not add a consumer, acknowledge a
Rabbit message, write Redis/TDengine, restart a service, or send a report.

## Runtime identity

| Item | Observed value |
|---|---|
| Host | `cobra-ion` |
| Observation time | `2026-09-10T08:47:15+08:00` (Q2 probe) |
| engine-next | active, `MainPID=2900910`, `NRestarts=0` |
| t1-v2-live | active, `MainPID=2878024`, `NRestarts=0` |
| engine-next release | `20260903_e272842` |
| engine-next source commit | `e272842c8f490f55a1b017badb71e71904ce008e` |
| root filesystem | 19G total, 13G used, 4.5G available (75%) |

## Redis Q2 observation

The current-day active set was `q2:active:20260910`.  The read-only adapter
requested 5,219 symbols and read 5,219 hashes:

```text
coverage                 = 1.0
missing symbols          = 0
quotes                   = 5,219
Redis operations         = smembers: 1, hgetall: 5,219
source phase             = ph=0 for all sampled rows
source record time       = 2026-09-09 16:00:00 UTC / 2026-09-10 00:00:00 Asia/Shanghai (initialization cohort)
freshness policy result  = STALE (all 5,219 rows under 30-minute policy)
engine repeatability     = same_observation_engine_deterministic=true
```

This is a valid pre-open initialization observation.  It is not evidence that
the market feed is live, and `coverage=1.0` is not promoted to `READY` when
freshness/completeness is stale.

At the same observation time the following auction keys did not yet exist:

```text
market:auction:20260910:0920
market:auction:20260910:0924
market:auction:20260910:0925
market:auction:anchor:20260910
```

## External reference-source observation

Using the existing release connectors for the audit target `2026-09-09`, all
six bounded calls connected successfully:

| Source | Getter | Rows | Date/availability conclusion |
|---|---|---:|---|
| BaoStock | `fetch_daily_kline` | 1 | requested and returned date both `2026-09-09`; contract `PASS` |
| Kaipan | `fetch_hot_plates` | 3 | request date observed; response is not self-dated |
| Kaipan | `fetch_yesterday_bans_pool` | 3 | request date observed; response date is not required by legacy parser |
| Kaipan | `fetch_ban_reasons` | 1 | result has no verified historical source date |
| Wencai | `fetch_limitup_with_lb_days` | 3 | current query, no structured historical date; replay role `UNAVAILABLE` without dated evidence |
| THS | `fetch_hot_rank` | 3 | current query, no date parameter; replay role `UNAVAILABLE` |

The successful network response proves connectivity and current schema
observability only.  It does not turn an undated current query into a
historical runtime input.  `available_at` remains unknown unless separately
proven; `observed_at` is retained only as audit/provenance information.

## Next bounded action

Wait for a market-time observation before interpreting Q2 fields as active
auction data.  The next probe should compare two read-only observations and
record field changes, then use existing TD `auction_snapshot_v2` only as a
separate historical projection source.  No new provider or production hook is
required for this step.
