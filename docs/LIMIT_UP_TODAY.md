# Today's limit-up pool (C582)

`limit_up_pool_today` is a point-in-time snapshot keyed by `(symbol, date, as_of)`. The Redis-first builder reads
`q2:active:<YYYYMMDD>` and the corresponding `q2:<symbol>` hashes. It accepts only quote timestamps on the requested
trade date and at or before `as_of`; a historical active-set key does not make a later hash read historical data.
Rows are price-limit candidates using the board band and cent-rounded price. ST is excluded only with an available
same-date name; otherwise `st_status=UNAVAILABLE`. Redis has no verified seal-amount field, so `seal_amount` stays null.

| Q2 field | Observed meaning / use | C582 treatment |
| --- | --- | --- |
| `px`, `pc` | Current and previous close; milli-yuan | Divide by 1000, derive percent and compare board-rounded limit price |
| `amt` | Cumulative turnover, yuan | Not a seal amount |
| `vol` | Observed in lots | Not used for seal amount |
| `br`, `ar` | Residual auction order fields; not a verified close seal amount | Never map to `seal_amount` |
| `ls` | Limit-state indicator; it has no amount unit | Retain as upstream evidence only; not used to fabricate an amount |
| `ph` | Q2 phase code | `ph=3` is terminal evidence for candidate rows only |
| `ts` | Unix epoch milliseconds | Reject future and previous-date quotes |
| `mx`, `mn` | Intraday high/low fields | Not used; field reliability is unverified |

Observed raw response `q2:active:20261028`: 5,226 symbols and 24 hash fields. The `px/pc/amt/vol/br/ar/ph/ls/ts/mx/mn`
sample for `000001` was `11310/11470/1911547776/1687009/5804154/1171456/2/0/1793170800000/11680/11120`.
The captured 2026-10-29 read of that historical active set had `ph=0` for 5,226 hashes and quote timestamps spanning
2026-10-22 through 2026-10-28. This is why the builder rejects previous-date hashes and never infers a close snapshot
from the active-set key alone. Redis is accessed with `EXISTS`, `SMEMBERS`, and pipelined `HGETALL`; no scan or write
is performed by the reader.

The hot-key publisher uses the documented Core namespace and TTL but is not enabled by default in C582 because the
current execution permission is Redis read-only. The dispatcher reports `not_configured` unless a caller explicitly
injects a publisher; no live Redis writes were made during validation.

The board band is 20% for `688/689/30`, 30% for `8/4/92`, and 10% otherwise. ST does not alter the band. Limit price
uses decimal half-up rounding to one cent. A dated WenCai response may enrich seal amount, first/final limit time, and
open count only when its date suffix is the requested date. The captured `涨停封单额[20261028]` numeric values are yuan;
for example, the captured `000554` amount equals `386600` shares times `6.63` yuan. Its question includes `去除st`.

The 18 Redis sample events align with `kaipan_hot_*` at 09:16, 09:21, 09:26:30, 09:28:30, 09:31, 09:33, 09:40,
10:10, 10:20, 10:30, 10:45, 11:00, 11:25, 13:30, 14:00, 14:30, 14:50, and 15:05. Each uses the light-cost
Redis tier, three attempts with one-second backoff, and no WenCai fallback. The sole WenCai event is the configured
15:20 close confirmation, with a 30-minute minimum interval and one request per date.

A complete, non-truncated WenCai result with a matching reported row count and date suffix can establish the close
version when Redis is unavailable. If Redis is terminal (`ph=3`) and WenCai is unavailable, the terminal Redis rows
can establish it; partial WenCai rows are discarded in that case. If both sources are available and their symbol
sets disagree, the merged snapshot is retained as `DEGRADED` with one-sided members recorded in the manifest.

Core hot keys use `core:cache:data:limit_up_pool_today:{date}:{version}`, `...:{date}`, and `...:latest`, with a
604800-second TTL. Only formal versions advance `latest`; every sample retains its immutable version and date pointer.
The cold close target is `core_data.limit_up_pool_today_v1`, with no DDL in C582. C582's TD connection check returned
unavailable, and the table schema/write path could not be verified under the current read-only TD permission.
