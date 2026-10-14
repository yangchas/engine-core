import csv
import hashlib
import json

import pytest

from examples.fetch_kaipan_plate_members import fetch_all


def test_fetch_writes_deduplicated_relation_index_and_manifest(tmp_path):
    responses = {
        "801159": {"list": [["000001", "甲"], ["000001", "甲"], ["600002", "乙"]],
                    "Count": 4, "Day": ["2026-10-13"]},
        "801571": {"list": [], "Count": 0, "Day": ["2026-10-13"]},
    }
    out = tmp_path / "out"
    manifest = fetch_all(lambda code, _date: responses[code], ["801159", "801571"],
                         date="2026-10-13", output_dir=out, sleep_s=0,
                         sleep_fn=lambda _: None)

    with (out / "relation.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [["plateid", "stockid"], ["801159", "000001"], ["801159", "600002"]]
    assert json.loads((out / "plates_index.json").read_text(encoding="utf-8")) == {
        "801159": {"count": 4, "got": 3, "day": "2026-10-13", "status": "OK"},
        "801571": {"count": 0, "got": 0, "day": "2026-10-13", "status": "EMPTY"},
    }
    assert manifest["complete"] is True
    assert manifest["count_mismatch"] == ["801159"]
    assert manifest["request_count"] == 2
    relation = (out / "relation.csv").read_bytes()
    assert not relation.startswith(b"\xef\xbb\xbf")
    assert manifest["sha256"]["relation.csv"] == hashlib.sha256(relation).hexdigest()


def test_resume_skips_existing_member_response_and_rebuilds_outputs(tmp_path):
    out = tmp_path / "out"
    calls = []
    response = {"list": [["000001", "甲"]], "Count": 1, "Day": ["2026-10-13"]}
    first = fetch_all(lambda code, date: calls.append((code, date)) or response, ["801159"],
                      date="2026-10-13", output_dir=out, sleep_s=0, sleep_fn=lambda _: None)
    calls.clear()

    resumed = fetch_all(lambda code, date: calls.append((code, date)) or response, ["801159"],
                        date="2026-10-13", output_dir=out, resume=True, sleep_s=0,
                        sleep_fn=lambda _: None)

    assert first["complete"] is True and resumed["complete"] is True
    assert calls == []
    assert resumed["request_count"] == 0
    assert (out / "relation.csv").read_text(encoding="utf-8").splitlines() == [
        "plateid,stockid", "801159,000001"
    ]


def test_count_mismatch_is_recorded_and_consecutive_failures_stop(tmp_path):
    out = tmp_path / "out"
    response = {"list": [], "Count": 2, "Day": ["2026-10-13"]}
    manifest = fetch_all(lambda _code, _date: response, ["801001"], date="2026-10-13",
                         output_dir=out, sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["count_mismatch"] == ["801001"]

    calls = []
    failed = fetch_all(lambda code, _date: calls.append(code) or (_ for _ in ()).throw(ValueError()),
                       [f"801{i:03d}" for i in range(8)], date="2026-10-13",
                       output_dir=tmp_path / "failed", consecutive_failures=3,
                       sleep_s=0, sleep_fn=lambda _: None)
    assert len(calls) == 3
    assert failed["complete"] is False
    assert failed["failures"] == [{"plate_code": code, "error_type": "ValueError"}
                                  for code in calls]


def test_nonempty_output_directory_requires_resume(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("keep")
    with pytest.raises(FileExistsError):
        fetch_all(lambda _code, _date: {}, ["801001"], date="2026-10-13",
                  output_dir=out, sleep_s=0, sleep_fn=lambda _: None)
