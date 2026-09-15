# Real live Q2 / opening probe — 2026-09-15 11:06 CST

## Scope

This was a bounded, read-only probe against the live Cobra-ion Redis/TD
services. It did not restart `engine-next` or `t1-v2-live`, add a Rabbit
consumer, acknowledge Rabbit messages, write Redis/TD, repair data, send
notifications, or execute an effect.

The runner archive used for the remote execution was the existing validation
copy `engine-core-81e0dd3`. The local repository HEAD at capture time was
`aaf9b61`; this evidence is therefore a real-source observation, not an exact
current-HEAD deployment verification.

## Redis Q2 result

Remote output: `live-q2-probe-20260915-1106.json`.

- Trade date: `2026-09-15`.
- Q2 keys: `5220`; requested/valid rows: `5220/5220`.
- Row coverage: `1.0`; all required raw fields were present.
- `source_record_time_ms` range was preserved, with newest source time
  `1789437007000` and an observed lag of about `4502 s` at the probe time.
- Projection status: `STALE`; all `5220` symbols were stale under the
  explicit `60000 ms` policy.
- Consistency: `BEST_EFFORT_STALE` and universe authority remains
  `NOT_PROVEN_BY_ACTIVE_SET`.
- The same Redis observation was run through Core twice. Both runs produced
  probe hash `50a3affca11613f05345e8d3fcba20872edb1df0e8f9c9e40536bf355e8c97df`
  and snapshot hash
  `d559d53d09d902875f3e1957a339a8526aba603e0229ed5dbae086513f774130`.
- Input canonical hash:
  `a25363d3b2ef065ff430f0b1afed7dee8a5c999b5b83d5bf8d52b5117944d158`.

The source is real and structurally complete, but it is not current enough
for a live-ready decision. `coverage=1.0` does not upgrade freshness or
completeness.

## Opening fact result

Remote output: `live-opening-facts-20260915-1106.json`.

- Trade date: `2026-09-15`; projection rows: `5220`.
- Projection status: `STALE`; freshness status: `STALE_OR_MIXED`.
- Three bounded symbols (`000001`, `000002`, `600519`) produced field-level
  opening facts and a stable semantic hash
  `842ff6b5f47c732aef3b2e3ee831b54b00e4a05b0192d42da5ba676208e4b7dc`.
- Projection hash:
  `1c520a9129585bf81e183c457f312473615ee006a257c8cb17f62525e95e6e01`.
- Individual fact `status=available` means the requested fields for that
  symbol were present and parseable. It does not override the batch-level
  `projection_status=STALE` or `freshness_status=STALE_OR_MIXED`; consumers
  must carry and display both levels.

## TD cross-check

At `2026-09-15T11:07:58+08:00`, a read-only TD query returned:

- `market_data1.stock_tick_v2`: first `09:15:00`, last `09:51:55`,
  `2,300,936` rows for the bounded `09:00–12:00` interval.
- `market_data1.auction_snapshot_v2`: no rows for the same date/interval.

This confirms that TD is receiving real ticks, but the latest persisted tick
was still roughly 76 minutes behind wall time and no current-day auction
snapshot was available at the check.

## Service and safety observation

At the surrounding checks both production services were `active` with
`NRestarts=0`. `t1-v2-live` reported `ack_fail=0` while processing input, but
its `wall_lag_ms` remained around four million milliseconds. This is an
upstream/backlog freshness issue, not evidence of a Core calculation error.

## Acceptance

```text
REAL_SOURCE_REACHABILITY       PASS
Q2_STRUCTURAL_COVERAGE         PASS
CORE_REPEAT_DETERMINISM        PASS
Q2_FRESHNESS                   FAIL/WARN (all stale)
OPENING_PROJECTION_FRESHNESS   FAIL/WARN (stale_or_mixed)
TODAY_AUCTION_SNAPSHOT         UNAVAILABLE
PRODUCTION_SIDE_EFFECTS        0
NORMAL_ORIGIN_MORNING_SHADOW   NOT PROVEN (run started after 09:15)
CORE_REPLACES_ENGINE_NEXT      NOT READY
```

The evidence is retained as a real-data observation. It must not be used to
claim normal-origin 09:26/09:32 evidence, current-live readiness, or
`engine-next` replacement. The next valid replacement gate remains a complete
trading-day run started before 09:15, followed by build-only report parity.
