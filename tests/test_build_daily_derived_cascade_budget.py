import json

import pytest

from examples import build_daily_derived as job
from engine_core.guardian import registry


@pytest.mark.parametrize(
    ("build_seconds", "expected_built", "expected_cut"),
    [
        (30, ["2026-10-20", "2026-10-21", "2026-10-22"], []),
        (60, ["2026-10-20", "2026-10-21"], ["2026-10-22"]),
    ],
)
def test_cascade_budget(monkeypatch, tmp_path, capsys, build_seconds, expected_built, expected_cut):
    clock = [0.0]
    built = []

    def build(catalog, dataset, day, args, redis):
        built.append(day)
        clock[0] += build_seconds
        return {"dataset": dataset}

    monkeypatch.setattr(job.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(job, "_build", build)
    monkeypatch.setattr(job, "_later_days", lambda catalog, day, limit: ["2026-10-21", "2026-10-22"])
    monkeypatch.setattr(registry, "derived_needs_build", lambda *args: True)
    result = job.main(["--dataset", "daily_report_features", "--date", "2026-10-20",
                       "--artifact-root", str(tmp_path), "--min-upstream-status", "PROVISIONAL"])
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["cascade_cut"] == [
        {"trade_date": day, "dataset": "daily_report_features"} for day in expected_cut
    ]
    assert built == expected_built
