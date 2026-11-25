# Guardian daily-kline arguments

- `budget_ratio`: optional number in `(0, 1]`; default `0.9`. It multiplies
  the Guardian event timeout to set `run_data_jobs.py --budget-seconds`.
- `budget_floor_seconds`: optional integer `>= 1`; default `60`. The budget is
  `max(floor, int(timeout * ratio))`. An empty event timeout adds no budget
  argument.
- `providers`: optional list, tuple, or comma-separated string containing only
  `baostock`, `wencai`, and `q2_final`. Whitespace is trimmed and duplicates
  removed while preserving configured order. If omitted, the CLI default is
  used.
- The built-in `baostock_probe` command uses the same ratio and floor for
  `--baostock-total-timeout-seconds`. An external
  `CORE_BAOSTOCK_PROBE_COMMAND` is passed through without that added argument.
