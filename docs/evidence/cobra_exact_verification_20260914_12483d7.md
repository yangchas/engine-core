# Cobra exact verification — 2026-09-14 (`12483d7`)

## Identity

```text
repository             engine_core
commit                 12483d7
archive                tmp/engine-core-12483d7.tar
archive_sha256         a9bfb150cde36cf155c1db9372d2392d8c7def2690ce9987fad11efc03a034aa
remote_archive         /home/exedev/validation/engine-core-12483d7.tar
remote_extract         /home/exedev/validation/engine-core-12483d7-v1
formal_python          3.12.3 (/home/exedev/services/engine-next/shared/venv/bin/python)
timezone               Asia/Shanghai (TZ=Asia/Shanghai)
locale                 LC_ALL=C.UTF-8
hash_seed              PYTHONHASHSEED=0
```

## Verification

The final branch commit was archived with repository-root paths preserved,
copied to cobra-ion, and extracted into a fresh isolated validation
directory. The formal Python 3.12.3 run completed:

```text
python -m pytest -q -p no:cacheprovider   404 passed in 2.06s
python -m compileall -q src tests          PASS
```

The archive SHA-256 matched the local value. The run is the same offline
contract suite as the local verification; it does not turn deterministic
fixtures into live-provider tests.

## Boundary

No production service was restarted or modified. The verification did not
add a Rabbit consumer, change ACK behavior, write Redis/TDengine, send
notifications, or execute effects. Live-provider and Core-replacement status
remain unchanged.
