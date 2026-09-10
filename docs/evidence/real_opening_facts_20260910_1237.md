# Real Redis Q2 opening-fact validation — 2026-09-10 12:37 CST

## Scope

This is a bounded, read-only validation of the first Opening migration wheel.
The isolated checkout called the existing Redis Q2 read dialect through
`RedisQ2ProjectionAdapter`, then applied the pure `build_open_fact` function.
It did not start `engine_next`, consume RabbitMQ, write Redis/TDengine, repair
data, send notifications, or run a strategy.

## Runtime evidence

| Item | Value |
|---|---|
| Host | `cobra-ion` |
| Runtime | `/home/exedev/services/engine-next/shared/venv/bin/python` |
| Core commit | `67822fddc762cc82e75e0be6fded1bd7d49f6bfa` |
| Verification archive SHA-256 | `14a251796a2c43c8cf7ec3581427307197e94bcf26ac769037e1c3ebdd1f8956` |
| Trade date | `2026-09-10` |
| Symbols | `000001`, `300750`, `600519` |
| Q2 projection status | `READY` |
| Cohort coverage | `1.0` |
| Freshness at observation | `FRESH` |
| Q2 source timestamp | `1789011000000` (same for the bounded symbols) |
| Core semantic hash | `2a8f6c2af072e2d7d3da665b1e292e7219cd9dd414985f7f1b08fbf9a4b1e97b` |
| Read boundary | `SMEMBERS/HGETALL` only |
| Result file SHA-256 | `a374100e8f5873f29bb5b0e920e186ee2ed49591ac9a5f4fd41c380e47cd1544` |

## Observed facts

| Symbol | `change_pct` (%) | `amount_2m_yuan` | `limit_state` | Fact status |
|---|---:|---:|---:|---|
| `000001` | `0.7692307692` | `715264` | `0` / available | available |
| `300750` | `0.8431302696` | `17578496` | `0` / available | available |
| `600519` | `-0.5345190878` | `2567680` | `0` / available | available |

The result is stored locally as:

`D:\work\Go\tmp\core-real-opening-20260910-123706.json`

## Parity boundary

The pure functions mirror the deployed release's `engine_next/runtime/open_confirmation.py`:

- `price_milli` and `previous_close_milli` must both be finite and positive;
- `change_pct` is percentage-point output, not ratio or basis points;
- `amount_2m_yuan` remains independent from price validity;
- `limit_state` has its own available/invalid/unavailable status and is not
  inferred from the percentage change;
- missing symbols remain unavailable and are not zero-filled.

This validates the extraction and real Redis path. It does not claim that the
full `engine_next` Opening report has been replaced; loader selection,
auction-to-opening assembly, and strategy/report migration remain later work.
