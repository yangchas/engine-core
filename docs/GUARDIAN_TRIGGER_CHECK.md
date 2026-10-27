# Guardian Baostock trigger check

Run from the repository root with the isolated service interpreter:

```sh
/home/exedev/services/engine-next/shared/venv/bin/python examples/guardian_baostock_trigger_check.py
```

The script builds an isolated `guardiand`, runs eight scenarios on its fake clock,
and stores artifacts under `/home/exedev/validation/s1/C560/runs`. Scenario C
uses one 3-second real handler timeout. G crosses the real 17:30 event deadline
while a handler is running; H checks the real 20:00 event after its deadline.
Success ends with `passed=8/8`; a nonzero exit or a smaller count is a failure.

Run selected cases with `--only A,C`. `--schedule-file PATH` points at a copied
schedule template for mutation checks; it never edits the repository schedule.
For example, change policy_11's first-tier `backoff` or `attempts` in a copy and
run `--only A --schedule-file PATH`; the fixed expectations must fail.
