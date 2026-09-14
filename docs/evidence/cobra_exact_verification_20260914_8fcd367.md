# Cobra exact verification — 2026-09-14 (`8fcd367`)

## Identity

```text
repository             engine_core
commit                 8fcd367
archive                tmp/engine-core-8fcd367.tar
archive_sha256         45007ee4f569fe8d9d9ca2bb929047a6d9d9b6933abec62ef8ced36085a3b034
remote_archive         /home/exedev/validation/engine-core-8fcd367.tar
remote_extract         /home/exedev/validation/engine-core-8fcd367-v1
formal_python          3.12.3 (/home/exedev/services/engine-next/shared/venv/bin/python)
timezone               Asia/Shanghai (TZ=Asia/Shanghai)
locale                 LC_ALL=C.UTF-8
hash_seed              PYTHONHASHSEED=0
```

## Verification

The archive was transferred without changing repository contents and
extracted into a fresh validation directory with repository-root paths
preserved. On cobra-ion:

```text
python -m pytest -q -p no:cacheprovider   404 passed in 2.10s
python -m compileall -q src tests          PASS
```

The local run of the same commit completed `404 passed in 1.32s`, with
`compileall` and `git diff --check` passing. The local and remote archive
SHA-256 values are identical.

The four added cases are offline provider-contract tests only: a non-trading
request is rejected before its provider callable is touched, and an empty
verified snapshot is `MISSING`, not `READY`. They do not access Redis,
TDengine, RabbitMQ, notifications, or third-party network sources.

## Boundary

This is an isolated exact-archive verification. It does not deploy Core,
stop or restart `engine-next`/`t1-v2-live`, add a Rabbit consumer, write
Redis/TDengine, or send effects. Live-provider and Core-replacement status is
unchanged.
