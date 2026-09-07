# Trading-day production-chain preflight

- Host: `cobra-ion`
- Checked at: `2026-09-07 08:30:05 CST`
- Trading calendar: `2026-09-07` reported as a trading day by the existing calendar service.
- `t1-v2-live.service`: active, `MainPID=2073327`, `NRestarts=0`.
- `engine-next.service`: active, `MainPID=2077012`, `NRestarts=0`.
- Root filesystem: 19G total, about 1.3G available (93% used) after removing only the temporary TD dry-run export created by this audit.
- Inode usage: about 30%.

## Read-only capture

The existing `/home/exedev/audit/tools/ground_truth_capture.py` was started once for trade date `2026-09-07` with output under `/home/exedev/audit/production_ground_truth/20260907`. It waits for the configured premarket/auction/Q2 slots and writes only audit artifacts. It does not add a Rabbit consumer, ACK messages, write Redis/TD, recover data, or send notifications.

Capture marker:

```text
run_id=20260907-2073327-1788740620
capture_tool_sha256=ca766d2c91c7fe7f2b61aae01dd153c46f26cfac97e6322fc3497ab3f8a8f80b
```

## Source availability observed before the session

- Redis responded to a read-only ping.
- `q2:active:*` currently contained only `q2:active:20260904` before today's session.
- Existing auction keys were present for `20260905` and `20260906`; no current-day auction projection existed yet.
- TD ports/processes were listening (`6030`, `6041`; `taosd` active).
- Existing TD read-only dry-run for `2026-09-04` passed using the server venv and the existing taos path. Its retained summary is in `td_connectivity_dry_run_20260907.md`; large temporary exports were removed after verification.

## Static side-effect gate

The `engine_next.runtime.intraday_data_hub.IntradayDataHub.load_auction_snapshots()` path was inspected in the deployed source. The method reads Redis snapshot hashes and performs local normalization/delta calculation; the inspected body contains no Redis write, TD query, network fetch, recovery, notification, or effect call. `recover_auction_anchor()` and other fetch/write methods remain outside this audit path and were not called.

This is a premarket observation only. The final six-layer matrix and 09:25 shape/transition evidence remain pending the live capture window and must not be inferred from TD alone.
