# Trading-day capture started — 2026-09-14

## Runtime state

At `2026-09-13T23:49:08+08:00` on `cobra-ion`, the existing audited
read-only capture tool was started for the next trading date:

```text
command:
  /home/exedev/services/engine-next/shared/venv/bin/python
  /home/exedev/audit/tools/ground_truth_capture.py capture
  --trade-date 2026-09-14
  --output-dir /home/exedev/audit/production_ground_truth/20260914

pid: 4014185
run_id: 20260914-2878024-1789314548
capture_tool_sha256:
  ca766d2c91c7fe7f2b61aae01dd153c46f26cfac97e6322fc3497ab3f8a8f80b
capture_started_sha256:
  608f5668e287695397aa151ca599de08acdbb3de6db672a1308c158ceab43e52
```

The process is running as the existing `exedev` account and writes only the
bounded audit directory.  It does not add a RabbitMQ consumer, ACK messages,
write Redis/TDengine, invoke recovery, send mail, or produce an external
effect.  The production `engine-next` and `t1-v2-live` services remain
unchanged and active.

## Expected slots

The already-reviewed tool waits for and captures the configured auction and
Q2 slots, including:

```text
auction: 09:20:05, 09:24:05, 09:25:10 (Asia/Shanghai)
Q2:      09:30:10 through 09:32:00 (10-second slots)
```

The capture must be sealed only after the process reaches its normal terminal
state.  Missing or late slots remain explicit in the manifest; they are not
filled from TD or current Redis state.

## Follow-up

After the process exits, retain the manifest and per-slot hashes, then run the
existing read-only comparisons:

```text
captured Redis projections
→ legacy loader behind GuardRedis
→ core fact wheels
→ authority/coverage/lineage comparison
```

No adjacent `0920 → 0924` or strategy parity claim is allowed unless both
slots are present and their field contracts are comparable.

