# Scheduled read-only evidence capture — 2026-09-15

At 2026-09-14 19:10 CST on `cobra-ion`, two bounded, self-owned read-only
processes were scheduled for the next trading day:

- Core morning shadow: PID `48703`, output
  `/home/exedev/validation/live-morning-shadow-20260915-0915`, using the
  exact `engine-core-59f0d91` archive.  It runs 09:15–09:33 for symbols
  `000001,000002,600519`.
- Existing production ground-truth capture: PID `49266`, output
  `/home/exedev/validation/production-ground-truth-20260915`.  It starts at
  09:09:40 and samples the existing 0920/0924/0925 auction keys plus the
  configured Q2 slots through 09:32.
- Post-capture Core audit: PID `49695`, scheduled for 09:40, reads that
  capture directory with `run_production_chain_shadow.py` and writes
  `/home/exedev/validation/production-chain-shadow-20260915`.

The second command is the existing release tool
`/home/exedev/services/engine-next/current/tools/production_capture/ground_truth_capture.py`
with its read-only Redis wrapper; it does not add a consumer, change ACKs, or
write Redis/TD/mail.  Both output paths were verified absent before scheduling
and are outside production data directories.  `engine-next` and `t1-v2-live`
were `active` at scheduling time and were not restarted.

This is a scheduled observation, not an acceptance result.  The next useful
evidence is the actual slot status and manifest after the trading session.
