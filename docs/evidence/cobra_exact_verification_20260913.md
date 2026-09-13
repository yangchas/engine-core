# Cobra-ion exact verification (2026-09-13)

## Identity

```text
repository branch = codex/feature-session-engine-integration
commit            = e21bf00caf8a2a278dc7b233c04e0be3d8ed0250
remote host       = cobra-ion
python            = 3.12.3 (/home/exedev/services/engine-next/shared/venv/bin/python)
timezone          = Asia/Shanghai
PYTHONHASHSEED    = 0
```

The source was streamed with `git archive HEAD` into a unique remote
`/tmp/engine-core-verify.*` directory. The verification directory was
temporary and was removed after the run. No production service, Redis key,
TDengine table, Rabbit consumer/ACK path or notification path was changed.

## Commands and result

```text
python -m pytest -q       = 305 passed in 1.67s
python -m compileall -q src tests = PASS
```

The local run on the same commit also produced `305 passed in 1.04s`,
`compileall` PASS and `git diff --check` PASS. Test count is evidence for the
same deterministic suite only; it is not evidence that every test connects to
production data. Real TD/Redis/provider probes remain separate read-only
commands and are recorded in their corresponding evidence files.

## Classification

```text
same_commit_local_remote = PASS
same_test_suite          = PASS
production_mutation      = 0
formal_milestone_tag     = not created
```
