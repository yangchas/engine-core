# Exact-build real Core read-only shadow: 2026-09-16

The following probes ran from the exact cobra-ion archive `ba309b8` described
in `cobra_exact_verification_20260916_ba309b8.md`:

```text
/home/exedev/validation/core-shadow-20260916-ba309b8
```

## Redis Q2

`run_live_q2_probe.py` read the production Redis Q2 cohort using only
`SMEMBERS`/`HGETALL`:

```text
requested / returned: 5221 / 5221
coverage: 1.0
status: PARTIAL
consistency: BEST_EFFORT_PARTIAL
stale symbols: 5221 (60s policy)
newest source lag: about 2759 seconds at 12:16 CST
projection hash: 9c635cbd9d0dd4190d7a97e8a678f2969892735a52115a9c212aa5a815bb829a
engine snapshot hash: a093b5b5183c2af4e342b5548f4db6e8b81fa3c89c966ee2edc20f82a642473b
engine probe hash: 54a44ccb40f0ce8d133a974843587e0e04ef4043ca3323e89ceb2425f9c32316
```

The same observed input was evaluated twice and produced identical snapshot
and probe hashes. `coverage=1.0` was not promoted to fresh/complete.

Artifact SHA-256:

```text
7f02040e6fa0ec19aba5a60a417f1cd3a46923173103f61c5d8b96de204c5ddc  q2.json
```

## TD auction projection

`run_real_auction_engine_shadow.py` performed a bounded TD `SELECT` for
`auction_snapshot_v2`, symbol `600519`, and traversed the public Core Engine
signal path:

```text
processed_signals: 6
strategy_result_count: 3
engine_fact_status: PARTIAL
engine_fact_only: true
semantic_hash_equal: true
fact hash: 8a89c528335a30b78efc47926c05cd34bc5466a056fb9d7034fefae934557d2a
source times: 0920=1789521603122, 0924=1789521850201, 0925=1789521906097
```

Artifact SHA-256:

```text
fe7778aa50a79cb9d7b869fc228ffc9f74043d0aa6688fb6f5596b19754eadb7  auction_engine_600519.json
```

The direct fact and Engine fact hashes matched. This remains a read-only,
single-symbol, fact-only shadow; it is not a replacement or strategy
acceptance.

## Previous-day reference

The exact-build TD read for request date `2026-09-16` derived
`previous_trade_date=2026-09-15` and returned zero rows:

```text
status: MISSING
completeness: 0.0
available_at_ms: null
rows_seen: 0
content_hash: 7953f2212fa0fc80802ee844a64b1703dba8fad934ecd7a3b66cd0bb771af462
```

No fallback date or write was attempted. Artifact SHA-256:

```text
6b261460656a20da18d18aa53dcba95470d1d4c5d7f91088d531ff053451ec0e  previous_day_stats.json
```

## Safety and conclusion

The exact-build real probes did not consume Rabbit, ACK/publish, write Redis
or TD, recover data, notify, or execute effects. `engine-next` and `t1-v2-live`
remained active with zero restarts. The evidence closes exact-build read-only
fact-path verification, but current Q2 freshness and previous-day data
availability remain blocked; Core is not ready to replace `engine-next`.

