# Exact cobra-ion verification: `4e70d50`

## Scope

This verification covers the prefetch-cutoff contract fix in
`build_auction_reference_bundle`. A preparation observed at an earlier
knowledge cutoff may complete a later Engine evaluation; a preparation after
the evaluation cutoff is rejected. No production service, Rabbit consumer,
Redis/TD writer, notification, or effect path was changed.

## Identity

| Item | Value |
| --- | --- |
| Commit | `4e70d50` |
| Archive | `tmp/engine-core-4e70d50-v1.tar` |
| Archive SHA-256 | `b5400f54e896d600cbebdf8fa9d084186f371a5e637c413a558efa4da333cd9b` |
| Remote archive | `/home/exedev/validation/engine-core-4e70d50-v1.tar` |
| Remote checkout | `/home/exedev/validation/engine-core-4e70d50-v1` |
| Runtime | `/home/exedev/services/engine-next/shared/venv/bin/python` (Python 3.12.3) |

The local archive SHA-256 and the remote archive SHA-256 matched exactly.

## Verification

| Check | Local Windows | cobra-ion Linux |
| --- | ---: | ---: |
| `python -m pytest -q -p no:cacheprovider` | 446 passed | 446 passed |
| `python -m compileall -q src tests` | PASS | PASS |
| `git diff --check` | PASS | n/a (archive checkout) |

The focused auction-reference tests passed before the full suite. The new
case proves that a prefetch at an earlier cutoff can be bound to a later
evaluation while the later-cutoff rejection remains covered.

## Production safety

This was an isolated archive run. `engine-next` and `t1-v2-live` were not
restarted or replaced. No Rabbit ACK/consumer behavior, Redis write, TD write,
notification, order, or other external effect was performed.

