# M2 hot-plates metadata candidate verification v2 — 2026-09-13

## Scope

Candidate-only verification against the exact Cobra release source. The
candidate was produced in a fresh detached clone and was never deployed.
There were no production Redis/TD writes, Rabbit ACK changes, network recovery,
notifications, or strategy effects.

## Candidate identity

```text
baseline_commit: e272842c8f490f55a1b017badb71e71904ce008e
transform_id: engine_next.hot_plates_meta.v2
transform_version: 2
recipe_sha256: be975b49e6fb447078c1ad687f4f93479aadf5f9f21863c68348a22aead92fd4
candidate_commit: 53a45ebb170a33a57d4c46aa4a4d699a17e3fe16
candidate_tree: 11b7e2f6174b5c344850c9e04cf8a467ca500fa4
allowed_path: engine_next/runtime/intraday_data_hub.py
```

The v2 candidate keeps the existing Kaipan request, Redis key layout, row
normalization, ranking, and writer side effects. It adds explicit
`HotPlatesV1` metadata, fixed field units, writer-observed availability,
canonical payload identity, and exact preserved-cache validation. A preserved
cache is reusable only when its metadata and the canonical hash of the actual
current Redis payload agree; units or payload mismatches fail closed.

## Candidate behavior checks

The isolated candidate was exercised with a fake Redis/Kaipan boundary only to
test the deterministic writer contract:

```text
fresh rows                 -> explicit metadata + deterministic payload hash   PASS
exact preserved metadata   -> availability/hash preserved                       PASS
payload hash mismatch      -> availability unknown; no salvage                   PASS
field units mismatch       -> availability unknown; no salvage                   PASS
```

The transform plan was initially found ambiguous because the baseline text
occurred in three writer methods. The recipe was tightened to include the
unique hot-plates fetch context, then passed `plan`, `apply`, and idempotent
`check` with exactly one changed path.

## Trusted verification

```text
task_id: M2-HOT-PLATES-META-20260913-R3
task_spec_hash: 1644bb9aa1b882c2845e9ec6184e4270313a79317d7b8ab2618020e164960c34
attempt_id: attempt-91e1b4731e794a52b375530838d90be0
verifier_manifest_hash: 972ced0e73a84241508b3562e24da5b438cbdeb53ffccbc3f693aa5816242808
command: python -m py_compile engine_next/runtime/intraday_data_hub.py
environment: Ubuntu WSL/Podman, pinned Python image
exit_code: 0
```

Trusted verification status is `PASS` for the exact candidate commit and
command.

## Independent audit

The read-only auditor was invoked against the same candidate and verifier
manifest. It did not produce a verdict because the local bridge failed with:

```text
Internal Windows PowerShell error; managed Windows PowerShell loading failed
with error 8009001d
```

This is an infrastructure block, not a code verdict. The candidate therefore
remains `IN_DOUBT` for independent audit and is not eligible for integration
or production deployment.

## Cobra-ion Core verification

The exact tracked Core commit containing this evidence was exported without
the local uncommitted M2 files and executed in an isolated remote directory:

```text
commit: 52a57db
tracked-commit pytest: 227 passed
```

To verify the current local 271-test working-tree suite as well, a separate
tar snapshot (SHA-256
`94bac82c778f3c2d231f4681a3c79cd42243fec639947353523a63e44abdedee`) was
extracted under `/tmp/engine-core-worktree-20260913` on cobra-ion. The server
Python 3.12.3 environment ran:

```text
pytest: 271 passed in 1.38s
compileall (src/tests/examples): PASS
```

This is explicitly a working-tree verification, not a claim that the
uncommitted files are part of commit `52a57db`. The production `engine-next`
service remained active with `MainPID=3181295`, `NRestarts=0`; its
`cache:hot_plates_meta:2026-09-10` still lacks the v2 fields, confirming that
no candidate was deployed.

## Decision

```text
V2_CANDIDATE_BEHAVIOR        PASS
TRUSTED_EXACT_VERIFICATION   PASS
INDEPENDENT_AUDIT            BLOCKED_INFRA / IN_DOUBT
CURRENT_PRODUCTION_METADATA  BLOCKED
M2_AUTHORITY_CLOSURE         BLOCKED
```

The next permitted step is a fresh independent read-only audit when the bridge
is usable, followed by a normal-run Cobra shadow. No production change is
authorized by this evidence.
