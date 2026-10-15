from datetime import datetime

from examples.run_live_shadow import run_loop


def test_cli_dry_run_does_not_publish():
    class TD:
        def states_at(self, cutoff): return {}
    class Redis:
        def set(self, *args, **kwargs): raise AssertionError("dry run wrote Redis")
    logs = []
    n = run_loop(TD(), Redis(), trade_date="2026-10-15", start=datetime(2026, 10, 15, 9, 15, 20),
                 end=datetime(2026, 10, 15, 9, 15, 20), dry_run=True, sleep_fn=lambda _: None,
                 now_fn=lambda: datetime(2026, 10, 15, 9, 15, 20), log_fn=logs.append)
    assert n == 1 and len(logs) == 1
