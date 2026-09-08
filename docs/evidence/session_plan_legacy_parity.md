# SessionPlan wheel-local legacy parity

Scope: trading-day validation and coarse runtime phase classification only.
This is not a scheduler, timer registry, market-status feed, or strategy state
machine.

| legacy_capability | legacy_location | new_wheel | behavior_contract | parity_status | evidence |
| --- | --- | --- | --- | --- | --- |
| Coarse time-of-day phase | `engine_next/runtime/startup_self_check.py:infer_run_phase` | `SessionPlan.phase_at` | Aware instant is converted to the plan timezone and classified by ordered half-open intervals | MATCH | Legacy source and boundary tests |
| Trading-day eligibility | Legacy caller-dependent; `infer_run_phase` itself ignores the calendar | `build_a_share_session_plan` | Calendar snapshot must confirm the requested date is a trading day | INTENTIONAL_CHANGE | Prevents weekend/holiday AUCTION and INTRADAY phases |
| Morning/lunch/afternoon boundaries | `infer_run_phase` | A-share session intervals | `[09:30,11:30)`, `[11:30,13:00)`, `[13:00,15:00)` | MATCH | Existing legacy lunch-boundary tests |
| 15:00 boundary | `infer_run_phase` includes exactly 15:00 in INTRADAY | A-share session intervals | Exactly 15:00 starts POSTMARKET under the universal half-open rule | INTENTIONAL_CHANGE | Removes a one-instant right-closed exception |
| NIGHT phase | `infer_run_phase` returns NIGHT only from 23:59:59 through midnight while `original_timeline.py` describes a different night-recap window | Not represented in V1 plan | Ambiguous legacy label is not promoted into the new contract | NOT_APPLICABLE | Conflicting legacy code/document behavior |
| Auction source freeze gates | `C/t1_v2/snapshot_trigger.cpp` at 09:20:03, 09:24:10, 09:25:06 | Not part of SessionPlan | Source publishing gates remain separate timer/business-anchor evidence | NOT_APPLICABLE | Gate B evidence; phase classification must not merge producer and consumer gates |

Rules:

- `TradingCalendarSnapshot` is the only trading-day authority.
- Calendar guard dates support boundary lookup only; they cannot be promoted
  into a runtime session outside declared coverage.
- `SessionPlan` accepts one explicit trading date and never substitutes a
  nearby date.
- All phase intervals cover one local day without gaps or overlap.
- Aware datetime and epoch-millisecond entry points share the same timezone
  and boundary contract.
- Window anchors and session intervals share `parse_clock_time_ms`; only an
  exclusive interval endpoint may use `24:00:00`.
- The plan has no machine-clock access and is usable unchanged by LIVE,
  replay, and unit tests.
