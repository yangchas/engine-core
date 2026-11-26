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
observations and require 20 nonmissing observations per pillar by default.
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
