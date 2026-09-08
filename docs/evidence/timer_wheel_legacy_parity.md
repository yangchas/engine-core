# Timer wheel-local legacy parity

Scope: deterministic calculation of due business timers for one already-built
SessionPlan.  This is not an Engine queue, workflow, persistent scheduler, or
producer snapshot trigger.

| legacy_capability | legacy_location | new_wheel | behavior_contract | parity_status | evidence |
| --- | --- | --- | --- | --- | --- |
| Match scheduled minute | `engine_next/app_main.py:_build_loop_decision` | `TimerSpec` + `due_timer_firings` | Explicit strict business anchor is due when crossed | MATCH | Legacy 09:25/09:26/15:05/17:40 branches |
| Same-process duplicate avoidance | `_execute_scheduled_event` stores only `_last_scheduled_event_token` | caller-supplied `already_fired` identities | Each timer id is returned at most once when prior firing identities are supplied | INTENTIONAL_CHANGE | Removes dependency on only the immediately previous event token |
| Missed-node restart behavior | No general legacy catch-up | inclusive frontier + `RECOVERY_CATCHUP` origin | Cold recovery returns all due, not-yet-fired business timers since local midnight | INTENTIONAL_CHANGE | Required arbitrary-restart contract |
| Actual firing time | Legacy minute string is both match and execution context | `scheduled_time_ms` vs `fired_time_ms` | Business anchor and actual execution time remain distinct | INTENTIONAL_CHANGE | Prevents late execution from rewriting the anchor |
| t1-v2 snapshot settling gates | `C/t1_v2/snapshot_trigger.cpp` | Not represented | 09:20:03/09:24:10/09:25:06 are producer source-publication gates, not Engine business timers | NOT_APPLICABLE | Gate B source/consumer boundary evidence |
| 09:25 field-morphology finalization | Trading-day source audit | Not represented | A wall timer cannot prove the final auction-shaped tick was consumed | NOT_APPLICABLE | Tick-shape audit remains separate |

The lower frontier is inclusive.  If logical time reached the scheduled
instant but the firing identity was not durably recorded before a crash, the
timer is returned again.  If it was recorded, `already_fired` suppresses it.
The wheel itself does not claim persistence.
