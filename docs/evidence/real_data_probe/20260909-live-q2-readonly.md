# 2026-09-09 live Q2 read-only probe

## Build identity

- probe commit: 1a48bd8
- uploaded source archive SHA-256: 19b554609b1005cd9a43eed01e8febb2d933489968eef3268855c7f89375666c
- runtime: /home/exedev/services/engine-next/shared/venv/bin/python
- Redis endpoint: loopback Redis (127.0.0.1:6379), read-only adapter surface

## Observation

- trade date: 2026-09-09
- observed at: 10:19:59 Asia/Shanghai
- source key: q2:active:20260909
- requested / returned: 5218 / 5218
- row coverage: 1.0
- missing symbols: 0
- Redis calls: smembers=1, hgetall=5218; no write method was exposed
- newest source time lag: 1027s at read completion
- strict freshness result (stale_after_ms=120000): STALE
- consistency: BEST_EFFORT_STALE
- input canonical SHA-256: 108fe89898eae0ac85a900b43c428aece2a8baedff0c37e62417fe7559f9d220
- projection semantic hash: 836c605ec27708b5273323b1e28727e7c884e2ff20270b27ce6b02ec29f35a91

The newest Q2 source timestamp was about 17 minutes behind the wall clock. This is a real upstream consumption delay, not an empty-universe or parser failure. A second diagnostic read with a wider 30-minute freshness window found 11 symbols still at the midnight initialization timestamp; it therefore remained mixed-freshness rather than complete.

## Determinism and side effects

The same immutable projection was submitted to two new deterministic Engine instances. Snapshot and probe hashes matched. The probe assembled neither TD, Redis write, claim, notification, SMTP, nor reporting components. It is a read-only source/engine smoke test, not live-coverage acceptance.

## Production correlation

At the same time, engine-next logged live_quote_ready=False with snapshots=5217 and a latest quote timestamp tracking the same delayed source. t1-v2-live and engine-next processes were active, and t1-v2 held an established RabbitMQ connection. Q2 timestamps continued to advance during sampling, but behind wall clock. The existing stale gate is therefore behaving as intended; do not promote these rows to real-time-ready facts or restart/clear production based only on this probe.

## Decision

REAL_Q2_READ_PATH = PASS

ENGINE_DETERMINISM_ON_ONE_OBSERVATION = PASS

LIVE_Q2_FRESHNESS = NOT_PASS (upstream lag)

LIVE_MARKET_COVERAGE = NOT_PROVEN_BY_ACTIVE_SET
