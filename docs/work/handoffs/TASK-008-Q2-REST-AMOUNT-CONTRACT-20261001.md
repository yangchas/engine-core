# TASK-008 Q2 auction residual amount contract — 2026-10-01

## Finding

Core previously described Q2 `br`/`ar` only as “derived level-2 resting
amount”. That wording could be mistaken for level-2 price multiplied by
level-2 volume. The pinned t1-v2 producer uses a different auction-specific
formula: level-1 reference price multiplied by level-2 unmatched volume.

## Producer contract

The exact release `20260923_tdstop0945b` source
`/home/exedev/services/t1-v2/releases/20260923_tdstop0945b/content/source/C/t1_v2/auction_calculator.cpp`
has SHA-256
`c3190f2b32c354b6cf67adc75381e21790b86c46e136e438e52854217ccbd932`.
Its auction calculator documents that level 1 carries the price and level 2
the unmatched quantity. In detail:

```text
br = (bp_milli[0] * bv[1] * 100) / 1000
ar = (ap_milli[0] * av[1] * 100) / 1000
```

The division is integer division on positive values. `100` is the board-lot
share count; `price_milli` is converted to yuan per share; nonpositive price or
volume yields zero. These are auction residual notional fields, not the
ordinary second-level quote notional. Q2 publishes them as `br` and `ar`; the
exact release Redis writer source hash is
`9baa218adb6f00cf7ae85929659c74ff9d01ce4882bc9180cc1ef46d9e586940`.

## Real-data evidence

Rechecked the existing same-release comparison artifact:

- Report: `/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2_td_auction_fields_20260930.json`
- Report SHA-256: `277d6bbe8633c7c9ae9cb788c70778c14389c1adb5b240955a24a3022b92a7e0`
- Q2Frame SHA-256: `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`
- Retained TD capture was obtained by `SELECT_ONLY`; exact producer binary
  SHA-256: `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.

For the 5,220 matched symbols, `br` equaled TD
`rest_bid_amt_yuan` for 5,220/5,220 and `ar` equaled `rest_ask_amt_yuan` for
5,220/5,220; both had zero mismatches. This is same-producer state/projection
parity for the captured date, release, and cohort, not an independent market
oracle or proof of Rabbit arrival/availability.

## Bounded Core change and verification

Only the `Q2FieldSpec` descriptions and their contract regression were
changed. Core calculation, values, adapter behavior, production code, and
freshness/acceptance gates were not changed.

```text
targeted tests/test_q2_adapter.py: 34 passed
full Core suite: 761 passed, 3 upstream protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
production/live I/O and side effects: NONE
```

## Alignment

This closes a narrow field-semantics ambiguity on the real Q2 → Core migration
path. It does not promote TASK-008 or prove live equivalence. Continue with a
specific existing downstream consumer whose Q2 fields and calculation
contract are evidenced; do not infer trading meaning from `br`/`ar` or start a
strategy from these parity counts alone.
