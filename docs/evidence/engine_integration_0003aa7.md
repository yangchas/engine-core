# Engine Integration Contract Verification

## Scope

This record covers code commit `0003aa7` (`fix(engine): close contract integrity gates`).
It does not change or access the production RabbitMQ/t1_v2/TD/Redis chain.

## Local check

Environment:

```text
Python: 3.9.13 (Windows edit/light-check environment)
formal runtime: Linux Python 3.12
pyproject SHA256: 13702DC97EC1BDCD5A50ED709597CD018B88F9EA4C927D7EFF881D229F9E4C9A
```

Results:

```text
python -m pytest -q       87 passed
python -m compileall -q    PASS
git diff --check            PASS
```

## cobra-ion check

Validation directory:

```text
/home/exedev/tmp/engine_core_validation_0003aa7
```

The archive was produced from the exact commit above. The remote check used
the existing read-only Python environment at
`/home/exedev/services/engine-next/shared/venv`.

```text
Python: 3.12.3
pytest: 87 passed in 0.74s
compileall: PASS
TZ: CST
LC_CTYPE: POSIX
```

Fixture hashes matched the local copies:

```text
tests/fixtures/facts/auction_600519_20260903.json
689c02c89f6b0fee99712de4a0bae0287b83e6bb5388e197488a5d02f51e18ba

tests/fixtures/replay/q2frame_600519_20260903.jsonl
b1884710f8a44b6b6213436bf131b6b1046b1b5deea90c801eb9d23d970d02a6
```

## Gate interpretation

The commit verifies immutable semantic payloads and derived hashes, strict
`available_at` knowledge-cutoff gating, Engine-owned evaluation completion,
same-time causal generations, per-consumption VirtualClock advancement, and
the versioned Semantic/Evidence/Submission hash contracts. `observed_at` is
retained only for audit/provenance/submission identity. TD Event-Time Replay
remains deterministic event-time only; Rabbit arrival/batch, watermark,
checkpoint and effect semantics remain deferred.
