# 2026-09-18 real read-only chain probe (13:12–13:18 CST)

## Scope

This is a bounded, read-only production observation. It does not restart
`engine-next` or `t1-v2-live`, add a Rabbit consumer, change ACK behavior, write
Redis/TDengine, repair a cache, send notifications, or execute a strategy/effect.

Runtime host: `cobra-ion`  
Runtime Python: `/home/exedev/services/engine-next/shared/venv/bin/python`  
Core validation copy: `/home/exedev/validation/engine-core-6511981-v1`  
Legacy release: `engine-next@20260903_e272842`

## Production safety observation

At 13:11 CST:

```text
engine-next   active, MainPID=1041872, NRestarts=0
t1-v2-live    active, MainPID=2878024, NRestarts=0
root disk     93% used, about 1.3G available
```

No production process was restarted and no cleanup was performed.

## Real Redis Q2

Command: `run_live_q2_probe.py`, trade date `2026-09-18`, freshness policy
`stale_after_ms=60000`.

```text
requested / returned          5224 / 5224
row coverage                  1.0
stale symbols                 5224
newest source lag             about 6691 seconds
status                       STALE
consistency                  BEST_EFFORT_STALE
repeated Engine hashes        identical
projection hash               118059e7d1fe22bb4187c447c7f1673195e0af61b13e1d5ef08a4918b7766527
artifact SHA-256              5fa801ed84a19b594af5072742e1be780fec0ce8151e15c93999602feb4428b1
```

The result proves the real Redis read path and deterministic re-computation. It
does not prove current-market freshness. Coverage `1.0` is intentionally not
promoted to `READY`.

The probe also preserved the current Q2 source field as
`source_record_time_ms`; it is not interpreted as Rabbit arrival time or an
exchange-level per-tick ordering key.

## Real engine-next read path

The legacy read-only probes used the exact deployed release and bounded symbols
`000001`, `000002`, `600519`.

```text
auction loader:
0920 = 200 rows, 0924 = 200 rows, 0925 = 200 rows
guard_writes = []
artifact SHA-256 = c54d3e78d2d9082c09d1396ae921b71d048a17960a79e941181ea9b5b1897b3d

context probe:
3 bounded context rows, 3 legacy fact rows
guard_writes = []
latest quote age = about 6696 seconds
artifact SHA-256 = 1bc69156ded409f644dbe1c7aa54781aa9e84cf341ea6d68b1bbf33a3f69da07
```

The auction loader is a Top-200 projection, not a full-market snapshot. The
loader and context probe are different projections and are not declared exact
parity without a shared immutable source/as-of identity.

## Real engine_core shadow

### Opening

The public Core Engine path consumed one real Redis Q2 observation per bounded
symbol. All three runs reported:

```text
projection_status = STALE
projection_coverage = 1.0
stale_symbol_count = 5224
strategy state = OBSERVE
decision_status = FACT_ONLY
fact_status = PARTIAL
read-only boundary = Redis SMEMBERS/HGETALL + in-memory Engine
```

Artifacts:

```text
000001  2b178991c0769587616cf8bbae8aa2ea108c3f295a6eafa33aa76e7f33c8f8fa
000002  b7ca48feed9e4fb2f8cc3d61671de4d70466d7213189be0a02a46750df5fbb31
600519  68991a6e18c972335e7c94d5ae7663d348a941253e13fc8a45934e0a5feac9e2
```

### Auction

The public Core Engine path consumed real TD `auction_snapshot_v2` rows for
0920/0924/0925. For all three bounded symbols:

```text
processed signals              6
strategy results                3
direct fact hash == Engine hash true
engine fact only               true
engine fact status              PARTIAL
side effects                    none
```

This is a real-data fact-path parity result, not a migrated auction strategy or
production owner replacement.

```text
000001  d4b67741474353c79176a815f29305a36570e62017798d4220ea203e9cd13ff6
000002  74777044947fda22cd1bb235a96e795a8c8f934f1763d1c3a5ab2572b0b05270
600519  e0b34ec6f5f8740a20b39acd73605253db806f220c2eddd08cba0451c14ed4ef
```

## Third-party probe boundary

A bounded legacy reference-source probe was started but exceeded the command
budget and made the local SSH broker unresponsive. It was stopped by restarting
the private broker only; production services were not touched. No result from
that incomplete probe is accepted as evidence. This reinforces the runtime
rule: third-party I/O must stay outside the reducer and must have an explicit
timeout; a connector timeout cannot block Core market processing.

## Acceptance

```text
REAL_REDIS_READ_PATH       PASS (freshness WARN)
ENGINE_NEXT_READ_PATH      OBSERVED (read-only, Top-200/bounded)
CORE_OPENING_SHADOW        PASS for read-only fact execution, status PARTIAL
CORE_AUCTION_SHADOW        PASS for direct/Engine fact hash parity, status PARTIAL
REFERENCE_DATA_READINESS   NOT CLOSED
CORE_REPLACEMENT           NOT READY
```

The next implementation step remains M2/M3 boundary work only after the
current real-source evidence is recorded: reference-data readiness/prefetch and
startup/node orchestration. No new Provider family, Rabbit ingestion,
checkpoint, watermark, fallback engine, or effect subsystem is justified by
this probe.
