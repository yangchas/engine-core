# M3 09:24/09:25 follow-up node validation — 2026-09-18

## Scope

This evidence covers the new bounded follow-up node seam only. It does not
claim a normal in-session 09:24/09:25 capture because the remote validation was
run after the market window. `engine-next` and `t1-v2-live` remained the
production owners.

Safety boundary:

- no Rabbit consumer or ACK change;
- no Redis/TD write;
- no recovery/backfill or fallback source;
- no notification, order, or other effect;
- isolated validation copy only.

## Local verification

```text
pytest -q -p no:cacheprovider: 532 passed
compileall: PASS
git diff --check: PASS
```

The new tests cover normal 09:24 dispatch, 09:25 prior-anchor ownership,
late-normal fail-closed behavior, recovery cutoff rejection, and prior-tag
identity validation.

## Cobra-ion verification

Validation copy:

```text
/home/exedev/validation/engine-core-m3-followup-20260918
```

Runtime:

```text
/home/exedev/services/engine-next/shared/venv/bin/python
Python 3.12
```

The exact isolated copy ran:

```text
pytest -q -p no:cacheprovider: 532 passed
compileall: PASS
```

The two changed files had matching local/remote SHA-256:

```text
examples/run_m3_auction_followup_shadow.py
a76d497421ef84b46684ba840a385974d442ae50893fa4fe504ade51b94a9dd0

tests/test_m3_auction_followup_shadow.py
f568c6378dbccd2faf2b31bc75fd39d3e3c6d18a6cc8d13e333d4f4a5453df52
```

## Real Redis recovery guard

Using the same isolated copy, a real read-only Redis run for `2026-09-18`,
symbol `000338`, node `0925`, and `origin=RECOVERY_CATCHUP` returned:

```text
preflight_gate=BLOCKED
node_dispatched=false
reason=projection_observed_after_0925_cutoff
```

The run did not use the post-market observations to reconstruct the old
business anchor. A second real invocation with `origin=NORMAL` outside the
bounded window returned `normal_capture_window_invalid` before source read.

## Conclusion

The follow-up seam is code-verified and real-source fail-closed verified. A
normal 09:24/09:25 trading-window capture remains open for a future session;
this artifact must not be read as production-node acceptance.
