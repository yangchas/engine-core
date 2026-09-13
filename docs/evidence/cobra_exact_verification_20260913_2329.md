# Cobra-ion exact verification — 2026-09-13 23:29 CST

## Identity

```text
repository       = engine_core
branch           = codex/feature-session-engine-integration
commit           = 12045dd (20fbe95 code baseline plus evidence only)
host             = cobra-ion
python           = /home/exedev/services/engine-next/shared/venv/bin/python (3.12.3)
timezone         = Asia/Shanghai
PYTHONHASHSEED   = 0
```

The exact `git archive HEAD` was streamed into a unique temporary directory on
Cobra.  The directory was removed after verification.  No production service,
Redis key, TDengine table, Rabbit consumer/ACK path or notification path was
changed.

## Results

```text
local  python -m pytest -q -p no:cacheprovider       PASS (306 passed, 1.12s)
local  python -m compileall -q src tests examples    PASS
local  git diff --check                              PASS
remote python -m pytest -q -p no:cacheprovider       PASS (306 passed, 1.68s)
remote python -m compileall -q src tests examples    PASS
```

The test suite is the same offline deterministic contract suite on both hosts.
Its passing count does not imply that every test used live data; production TD
and Redis observations remain separate explicit read-only probes.

## Classification

```text
same_commit_archive                 PASS
same_python_major_minor             PASS (3.12)
same_test_suite                     PASS
production_mutation                 0
formal_replacement_of_engine_next  NOT_CLAIMED
```

The newest real TD fact shadow for this commit is recorded separately in
`real_td_auction_shadow_20260913_2316.md`.
