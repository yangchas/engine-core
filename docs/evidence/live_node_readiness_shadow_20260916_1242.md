# Live node-boundary readiness shadow — 2026-09-16 12:42 CST

The latest exact code commit `2bbb1ae` was run on cobra-ion in the isolated,
write-once directory:

```text
/home/exedev/validation/live-morning-node-recheck-20260916-1242
```

This was a late-start `RECOVERY_CATCHUP` run, not an exact replay of the
09:26/09:32 production moments.

```text
AUCTION_0926:  Q2 PARTIAL, coverage 1.0, TD rows 3,
               AnchorDeltaFactV1 READY
OPENING_0932:  Q2 PARTIAL, coverage 1.0, TD rows 3,
               OpeningTransitionFactV1 READY
```

The Q2 source cohort was complete by row count but stale under the explicit
60-second policy. Each node performed exactly one Q2 readiness read. The
auction fact remained independently TD-owned; Q2 was not substituted into the
auction calculation. The opening path remained Q2-dependent.

```text
AUCTION_0926.json: 56e350749e031190c46717765cba64d665e35ca1320645f81c7ca90556f30a2e
OPENING_0932.json: 88a6f03869b8c5b9531e6fb8ca6e3772bb18c784c344af43ec4621dd90c13bb6
manifest.json:     2e58966a9ca254c52672bd1442389a4f7f69eaf9462f645b6c40979b30789ace
startup.json:      2bfa10f9fd33fd0496acf9f20e13d2bb6ee836405c8e12b61b21732f4f6b1131
```

No Rabbit consumption or ACK, Redis/TD write, recovery/backfill,
notification/effect, or production restart occurred.
