# Scheduled read-only evidence capture — 2026-09-15

At 2026-09-14 19:23 CST on `cobra-ion`, three bounded, self-owned read-only
processes were rescheduled for the next trading day after a read-only timing
audit found the earlier relative sleeps would miss their intended windows:

- Core morning shadow: PID `51823`, output
  `/home/exedev/validation/live-morning-shadow-20260915-0915`, using the
  exact `engine-core-81e0dd3` archive.  It runs 09:15–09:33 for symbols
  `000001,000002,600519`.
- Existing production ground-truth capture: PID `51820`, output
  `/home/exedev/validation/production-ground-truth-20260915`.  It starts at
  09:09:40 and samples the existing 0920/0924/0925 auction keys plus the
  configured Q2 slots through 09:32.
- Post-capture Core audit: PID `51826`, scheduled for 09:40, reads that
  capture directory with `run_production_chain_shadow.py` and writes
  `/home/exedev/validation/production-chain-shadow-20260915`.

The replacement was guarded by exact command-line checks on the prior PIDs;
only the three self-owned waiting shells were stopped.  The new waits use
server-side absolute target timestamps (09:09:40, 09:15:00 and 09:40:00),
not hard-coded sleeps copied from an earlier observation time.

The second command is the existing release tool
`/home/exedev/services/engine-next/current/tools/production_capture/ground_truth_capture.py`
with its read-only Redis wrapper; it does not add a consumer, change ACKs, or
write Redis/TD/mail.  Both output paths were verified absent before scheduling
and are outside production data directories.  `engine-next` and `t1-v2-live`
were `active` at scheduling time and were not restarted.

This is a scheduled observation, not an acceptance result.  The next useful
evidence is the actual slot status and manifest after the trading session.

## Preflight recheck — 2026-09-14 20:47 CST

The three waiting shells were still alive (`51820`, `51823`, `51826`) and the
target output directories were still absent.  `engine-next` and `t1-v2-live`
remained `active`.  The server root filesystem was at 89% (about 2.0 GB free)
and `/home/exedev/validation` used 611 MB; no cleanup or process restart was
performed.  This capacity is recorded as an operational warning for the
bounded capture, not as evidence that any capture slot has completed.
