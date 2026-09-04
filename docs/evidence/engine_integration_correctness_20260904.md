# Engine integration correctness and wheel parity

Date: 2026-09-04 (Asia/Shanghai)
Branch: `codex/feature-engine-integration`

## Correctness gates

```text
same-time signal ordering       PASS
DATA_READY submission ordering  PASS
PARTIAL propagation             PASS
old evaluation isolation        PASS
bounded long-drain state        PASS
duplicate/conflicting signal    PASS
```

The Engine retains only a bounded recent result history and a bounded
in-memory signal-id dedupe horizon. Durable idempotency remains deferred to
the journal/checkpoint phase.

## Wheel to Engine parity

The Foundation 600519 fixture was composed twice: once directly with the
Foundation fact wheels and once inside a test-only Engine strategy. The hashes
matched exactly:

```text
Segment A       27dbe968c3e112b28d556e6d0acbfbe3ea9a00e745de89ece4cb4a6a5b88b3fb
Segment B       383e8cdba6cde2c090f1b7be0c82d65a1f5aeef302f40276f9eb6ccc699438ac
Comparison      b6a52d4339885429eda80a23440084544ce04a2a10c405ced3b47b4071e50907
```

This verifies that the current Engine composition does not alter the frozen
Foundation fact semantics.

## Verification

```text
local: 66 passed
cobra-ion Python 3.12.3: 66 passed
local/remote compileall: passed
```

The remote run used the temporary copy
`/home/exedev/tmp/engine_core_validation_20260904_1244` only.
