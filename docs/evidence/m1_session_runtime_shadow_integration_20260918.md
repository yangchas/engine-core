# M1 Session Runtime / Morning Shadow Integration — 2026-09-18

## Change

The bounded read-only `run_live_morning_shadow.py` now obtains timer firings
from `SessionRuntimeCoordinator` and acknowledges a firing only after its node
evidence has been written successfully. The coordinator is not a second
scheduler and does not perform I/O.

The runner deliberately keeps the node-level input boundary:

- `AUCTION_0926` may execute through the independent TD auction fact path when
  Q2 is missing or stale.
- `OPENING_0932` still calls the node Q2 read and raises/fails closed when that
  read is unavailable.

This avoids a second high-frequency Q2 read merely to calculate timer due-ness
and preserves the existing production-shadow behavior.

The coordinator also keeps deferred/pending timer identities alive across an
acknowledgement, so a late-start poll at 09:33 cannot lose an earlier
deferred 09:32 timer after acknowledging the 09:26 timer.

## Verification

| Check | Result |
|---|---|
| Local full suite | `463 passed` |
| Local compileall (`src tests examples`) | PASS |
| Local diff-check | PASS |
| cobra-ion isolated suite | `463 passed` |
| cobra-ion compileall (`src tests examples`) | PASS |
| Production services changed | NO |
| Rabbit consumer/ACK changed | NO |
| Redis/TD writes | NO |

The remote validation copy was `/home/exedev/validation/engine-core-6511981-v1`
and used `/home/exedev/services/engine-next/shared/venv/bin/python`. It is not
the production checkout.

## Scope boundary

This closes only the in-memory timer-to-node shadow integration. It does not
claim fresh live Q2, historical reference-data availability, exact Rabbit batch
membership, durable checkpoint identity, or permission to replace
`engine-next`.
