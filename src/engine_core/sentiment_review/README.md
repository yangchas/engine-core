# Sentiment review

This offline research package accepts normalized, as-of snapshots; it does not
connect to services when imported or run with JSON input. `build_timeseries_dataset`
accepts a date-keyed mapping. Each day has `snapshots` keyed by `HH:MM`; each
snapshot is a complete symbol-keyed mapping. Rows use yuan prices (`px`, `pc`,
`high`, optionally `open`, `low`, bid/ask prices), share volumes, `amount_yuan`,
and optionally `pct`, `float_cap`, `pos20`, `ma20`, and `chip_profit_ratio`.
The caller supplies point-in-time `members` (theme to symbols), prior-session
`prior_features` and explicitly available `kaipan` entries. No membership or
historical source availability is inferred. Dates omitted from the input cannot
be detected as missing trading sessions; supply all sessions in order. Daily
close must be named `15:00`. CLI build consumes this format as JSON.

Theme factors use only snapshots received through the current cut and prior
auction amounts. Their universe may change as observations arrive. Optional
outcomes are returned separately and the pipeline never injects them. A caller
is responsible for withholding later or retrospectively fetched records. The
Kaipan adapter retains the actual capture/fetch timestamp and unknown positional
columns as `raw_fields`; a 09:26 capture is not a 09:25 observation. Historical
apphis records fetched later do not establish historical availability.

Percentile ranks include the current observation, use at most 60 same-cut
observations and require 20 nonmissing observations per pillar at the daily close. Intraday defaults to 10 prior observations plus the current observation; `min_history` is configurable and `history_n` counts prior same-cut observations.
The inverse downside percentile follows the reference formula. Regime thresholds
are an explicit research convention: <=10 is 双冰点 (假冰点 if the change from
09:25 is positive), <30 偏冷, <60 中性, <80 活跃, otherwise 高潮. This
classification is not established by the reference scripts. Missing pillars,
empty ratios and unavailable seal amounts remain null. The reference's 50%
empty seal-rate and zero fallback returns are intentionally not used.

The TD reader is a low-level read-only row loader, not an as-of join or a
normalization adapter. It requires taos only when called. Results need independent
availability and provenance validation before use. `audit` checks output ordering
and outcome separation; it cannot prove the provenance or availability of inputs.

`build-real` reads TD and Kaipan captures for offline research and writes a
compressed pickle; load only trusted pickle files. By default its metadata
marks `RETROSPECTIVE_BACKFILL`: historical Kaipan and latest plate membership
are retrospective, not evidence of live capture. `--strict-capture-time`
excludes historical Kaipan fetched after the cut and uses as-of membership;
indicator records with unknown or later availability are excluded. The TD
source does not establish historical availability for every input. Morning
points end at 09:45:30; close and next-open observations are outcomes. Historical
Kaipan historical pools supply prior-day ladder and broken cohorts in retrospective mode. The floor pool has separate feedback; rotten remains a daily-bar-derived cohort. Per-day cross-checks retain daily-derived cohort counts. `daily_days` counts close records, and `intraday_tick_days` (also `intraday_days`) counts days with nonempty morning snapshots; each day carries `has_intraday_ticks`. Audit checks ordering, reports per-cut critical-field coverage, fails on zero coverage for a nonempty cut, and checks that theme outcomes are absent from theme features;
it does not prove input provenance or complete market coverage.

Float-cap weights use amount divided by turnover fraction from the preceding session for intraday, and the same-session value or preceding-session fallback at the daily close. Hot-member performance uses normalized labels and deduplicated member symbols; membership in retrospective builds is not historically point-in-time verified. Daily 20-day range position uses the current close relative to the preceding 20 trading bars.

The two structural factors use the prior-session streak cohort and current
snapshot, and current eligible theme returns, respectively. Theme comparisons
normalize labels; the real loader groups symbols by the first member label,
which is a retrospective theme proxy rather than verified plate membership.
A missing prior hot list or 09:25 leader leaves the related
comparison unavailable. Theme continuation outcomes use matched symbols and
are separate from features. Intraday temperature uses breadth, net sealed
limits, prior Top100 return, prior 2+ return (1-board fallback), and reversed
5% downside count. The package's pre-existing daily scoring is unchanged.
