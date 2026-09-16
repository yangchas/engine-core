# Real reference-source probe — 2026-09-16

## Scope

This is a bounded, read-only connectivity and schema probe through the
existing `engine_next` connector implementations. It is evidence for future
Provider extraction only; it is not a runtime DataFunction call and it does
not make current online results valid for historical replay.

## Runtime

| Item | Value |
|---|---|
| Server | `cobra-ion` |
| Core probe commit | `9dfec6558dfc50f10c7b833878b4ba3c7b9f4c32` |
| Legacy release | `/home/exedev/services/engine-next/releases/20260903_e272842` |
| Audit date | `2026-09-16` |
| Sample symbol | `600000` |
| Connector calls | `6/6 connection_status=PASS` |
| Side effects | No Redis/TD writer, repair, SMTP, notification or strategy component |

## Observed capabilities

| Source / dataset | Result | Runtime time-safety |
|---|---|---|
| BaoStock daily kline | 0 rows for requested `2026-09-16`; contract `FAIL` (`DATE_MISMATCH`) | No historical runtime use until a dated response is available |
| Kaipanla hot plates | 3 rows; fields and values observed | No self-dated response; `OBSERVED` only |
| Kaipanla yesterday limit pool | 0 rows | `MISSING` for this probe |
| Kaipanla ban reasons | 1 row, but no source trade date | `OBSERVED`; date semantics unresolved |
| THS hot rank | 3 rows | Current query has no date; historical runtime `UNAVAILABLE` |
| Wencai limit truth | 3 rows | Current query has no structured date; historical runtime unavailable without dated evidence |

The probe confirms that the existing connectors can be invoked and normalized,
but connectivity is not equivalent to a verified historical data contract.
Results without a provable `available_at`/dated response remain suitable for
online observation, oracle comparison and fixture capture only. They must not
be silently used as a replay-time input.

## Extraction decision

No new connector hierarchy was added. The current evidence supports later
thin wrapping of the existing access behavior, subject to source-specific
contracts:

```text
BaoStock daily kline       = RECHECK (empty requested date)
Kaipanla hot plates        = OBSERVED / dated semantics pending
Kaipanla limit pool        = MISSING for this probe
Kaipanla ban reasons       = OBSERVED / source date pending
THS hot rank               = ONLINE_ONLY until dated history exists
Wencai limit truth         = ONLINE_ONLY until dated query evidence exists
```

No source is promoted to a Core runtime fallback by this probe.

