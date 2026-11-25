# 2026-09-30 09:25 auction universe reconciliation

Status: `MISMATCH_VISIBLE` (diagnostic only; does not block or suppress Core output).

The new pure Core function in `src/engine_core/auction_universe_reconciliation.py`
compares a declared anchor symbol set with already-read TD auction rows. It
reports membership counts, bounded symbol details, duplicate rows, and a
descriptive profile for TD-only rows. It does not infer inactive securities,
invoke the legacy production gate, or alter any opening fact or task status.

## Sealed source inputs

- Trade date/tag: `2026-09-30` / `0925`.
- Redis anchor capture:
  `/home/exedev/validation/task008-live-freeze-20260930-0925/live_anchor_observation_2.json`
  SHA-256: `75c9fee7d240dff7091d30bbd2ad9a53e78d8f0b0ca03a09876abe3a764e73c9`.
- TD SELECT capture:
  `/home/exedev/validation/task008-live-freeze-20260930-0925/td_0925_observation_2.json`
  SHA-256: `0fed873cecdb163996263ab5ad63547843acd1d1ef95b943a510ada3a038c545`.
- The capture summary records the Redis anchor as present with 5,210 symbols
  and the TD observation as `SELECT_ONLY`, 5,220 rows / 5,220 unique symbols.

The read-only example runner verified both input hashes before adapting the
captured TD row arrays. Two complete function runs returned the same hashes:
the compact machine-readable output is pinned at
`docs/evidence/task008-auction-universe-reconciliation-20261010.json`
(SHA-256 `a1a86a1373064e35fcd09b533a4310d948b4df3cc843254ee66b6539d15a0d36`).

```text
anchor symbols             5,210
TD rows / unique symbols   5,220 / 5,220
shared                      5,210
anchor-only                     0
TD-only                         10
TD-only profile                 10 ZERO_AMOUNT_NULL_CHANGE
input_content_hash        e7272f44b5390ac3d7eb235844196d318c98a01e1e318ad424cff10600e55b8c
content_hash              43c323faaf1d8e55a13e9a44a4798c8f8ac2226b0e67c55e521ee071f85347d8
repeat hash equality      true
```

`ZERO_AMOUNT_NULL_CHANGE` means the observed `match_amt_yuan`,
`rest_bid_amt_yuan`, and `rest_ask_amt_yuan` values are zero and `chg_bp` is
null. It is not an inactive/delisted classification. The mismatch is visible
and nonblocking; it does not prove the full assembler ran or that any report
was suppressed.

## Tests and limits

The compact test fixture is an exact projection of two shared symbols and all
ten TD-only symbols from the two hash-pinned inputs. Its SHA-256 is
`4a2156df1293454003c5a000f9cf4e728af9b1e2fc44b306bae5e3e80aff3ce5`.
The full sealed captures remain outside Git.

Focused tests: 22 passed. Full Core suite: 978 passed. `compileall` and
`git diff --check` passed. No Redis/TD/Rabbit connection, service operation,
production write, or existing runner integration occurred. This feature is
not a production-universe gate and does not change TASK-008 or M3-1 status.

## Full replay sidecar A/B — 2026-10-10

The diagnostic sidecar was then evaluated through the complete captured
2026-09-30 Q2Frame replay and compared with a no-sidecar control. Both runs used
the same Core worktree/code fingerprint, Q2Frame input, and frozen pressure
context; the worktree already had unrelated uncommitted changes, which were
preserved. At the replay pre/post fingerprint check, HEAD was
`f7535d18b623fb73211ef507eeae6635d68fb28c`, the `src/`+`examples/` file-set
fingerprint was `452c100f1f5b00700516759f81436543a46e8854cbdb21a3c23e1514612401a7`,
and the worktree diff fingerprint was
`29d32be181a2553c1a9128e9ec84e05c6cd07289f82a32f6617d65bcadfa242f`; these
were unchanged across the two completed runs. The Q2Frame SHA-256 is
`1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`; the
pressure-context content hash is
`534fa2289d64da132c1ed0da5514ba9a34c253c3fae86676944b6cfcef96cb85`. The
sidecar SHA-256 is
`a24e3bd1155e2b85a5b0925ddceb7ce6d81395006fa80ea9b14759b58de861cf`.

| Run | Report | SHA-256 |
|---|---|---|
| Sidecar | `/home/exedev/validation/task008-auction-universe-sidecar-e2e-20261010T092626+0800/core_with_stale_universe_sidecar.json` | `64b40dd0bbd8843fd5edb5c42bc509d20c847c5cf48a64b360126f9c958639d4` |
| Control (attempt 2) | `/home/exedev/validation/task008-auction-universe-sidecar-e2e-20261010T092626+0800/core_without_stale_universe_sidecar_attempt2.json` | `9af9b8887c3f308a608f0b72b89e7583c25bbbb6d58a32a14f5e5b1eb263c299` |

After sorting JSON object keys, the reports are equal when excluding only the
report contract version, the root sidecar result, and the ordered/repeat
`OPENING_0932.auction_universe_attribution` sidecar fields. Thus the added
diagnostic did not change the remaining report outputs on this input. Both
reports processed 754 frames / 428,586 updates, emitted 758 signals at reducer
revision 754, and ended at logical time 09:32:10. Ordered and repeat final
state hashes both equal
`78fe99285be0a6b60a455ea7f15d9b37568fdf62bf6c5675734b5ed3fc4f0dc4`.
The sidecar's attribution is diagnostic only: seven stale TD-only rows match
the zero-amount/null-change profile, three other rows with that profile are
not stale, and causality is not established.

The first no-sidecar control attempt was interrupted after roughly 29 minutes;
it produced no report and is not counted as a result. The second control run
completed successfully. The replay process was run at low priority (`nice 19`)
after CPU contention was observed; service state remained active, with no
restart observed. Inputs were sealed local files and execution was in-memory:
no Redis, TDengine, RabbitMQ, production service, or external effect was
accessed. The pre-run full suite recorded `1001 passed, 3 warnings`,
`compileall` PASS, and `git diff --check` PASS; code/input fingerprints were
unchanged across the A/B runs. Runtime (about 35 minutes with the sidecar and
39 minutes for control) is recorded as an observation, not a pass/fail gate.

This establishes non-interference of this diagnostic sidecar for this pinned
input and code state only. It does not establish cross-date behavior, causal
explanation, live Redis/TD parity, Rabbit delivery/arrival parity, full-market
coverage, or NORMAL acceptance. TASK-008 remains `PARTIAL_EVIDENCE` and
`NORMAL_OPENING_ACCEPTANCE` remains `UNPROVEN`.
