# Cobra exact verification (2026-09-14 current HEAD)

## Identity

```text
repository             engine_core
commit                 ad098c5 (docs(audit): record current-release startup flow)
archive                tmp/engine-core-ad098c5.tar
archive_sha256         e05e7ae53555f0259fcf1294a377871f756b654e5b382450f741679346d64ec9
remote_archive         /home/exedev/validation/engine-core-ad098c5.tar
remote_extract         /home/exedev/validation/engine-core-ad098c5-v2
formal_python          3.12.3 (/home/exedev/services/engine-next/shared/venv/bin/python)
timezone               Asia/Shanghai (service); locale POSIX
```

## Verification

The archive was extracted with its repository-root paths preserved.  The
first diagnostic extraction attempt used `--strip-components=1`, which moved
`examples/*` to the wrong level and caused import/collection errors.  No code
was changed; the archive was re-extracted into the fresh `-v2` directory and
the same commit passed:

```text
pytest -q -p no:cacheprovider     322 passed in 1.65s
compileall -q src tests           PASS
pytest --collect-only             322 tests collected
```

The initial 14 collection errors were therefore an archive-layout validation
failure, not a Core semantic or runtime failure.  The corrected exact archive
contains `examples/` and is the formal Cobra result for this commit.

## Boundary

This is an isolated test directory.  It does not deploy Core, stop or restart
`engine-next`/`t1-v2-live`, add a Rabbit consumer, write Redis/TDengine, or
send notifications.  Windows Python 3.9 results remain informational only;
Cobra Python 3.12.3 is the formal runtime as specified by the project.
