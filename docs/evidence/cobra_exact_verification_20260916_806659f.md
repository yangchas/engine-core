# Cobra-ion exact verification — 2026-09-16

## Identity

```text
commit: 806659fd77cc5115a519fe375fea1099f90c9de6
archive_sha256: 6c35d326097df1df10ffb1a45fd378239cd1f9435e894138b229cb3cde9d34b1
remote_checkout: /home/exedev/validation/engine-core-806659fd77cc5115a519fe375fea1099f90c9de6
```

The remote checkout was created by extracting the exact local `git archive`.
The archive hash was checked locally and on Cobra-ion before execution.

## Verification

```text
local pytest: 420 passed
local compileall: PASS
remote pytest: 420 passed
remote compileall: PASS
```

Remote tests used the existing shared runtime environment:

```text
Python: 3.12.3
timezone: CST (Asia/Shanghai host convention)
locale: POSIX
pytest: /home/exedev/services/engine-next/shared/venv/bin/python
```

The local developer interpreter is Python 3.9.13; the formal runtime result is
the Cobra-ion Python 3.12.3 result. This verification is an exact archive and
test-suite check, not a claim that every pytest case opens a live connection.
Live Redis/TD/third-party probes remain separate evidence.

## Scope and safety

This commit only adds a read-only real PreviousDayStats evidence record. No
production service was restarted or replaced, no Rabbit consumer or ACK path
was changed, and no Redis/TD/effect write was performed.

