# EvaluationPlan boundary evidence

`EvaluationPlanV1` is plain immutable data.  It answers only:

> For this exact trigger, which query-data functions, fact functions and
> strategies are part of the evaluation, and in what declared order?

It does not answer when the trigger fires; `SessionTimerV1` does that.  It does
not obtain data; DataFunction/DataProvider do that.  It does not execute facts
or strategies; the Engine integration layer may compose already-tested wheels
later.

Legacy evidence:

| legacy_capability | legacy_location | new_contract | parity_status |
| --- | --- | --- | --- |
| Time branch selects an action | `engine_next/app_main.py:_build_loop_decision` | Timer identity selects exactly one node | MATCH |
| Controller mixes data loading, facts, decisions and rendering | `engine_next/runtime/controllers/auction_runtime_controller.py` | Ordered names explicitly separate data/fact/strategy roles | INTENTIONAL_CHANGE |
| Conditional business paths and state transitions | Many controller branches | Remain in Strategy/StateMachine, not EvaluationPlan | NOT_APPLICABLE |
| Retry, fallback and network recovery | Runtime/data hub | Remain outside EvaluationPlan | NOT_APPLICABLE |

Hard boundary:

- no YAML/DSL;
- no dynamic DAG;
- no if/else or conditional jumps;
- no retry or parallel-execution semantics;
- no “strategy A succeeded, then run B” behavior;
- no Provider or callable objects stored in the plan.
