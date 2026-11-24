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


def test_refresh_partial_reports_provider_failure_details(tmp_path, monkeypatch):
    from pathlib import Path
    from examples import run_data_jobs
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    real = Path(__file__).parent / 'fixtures' / 'real'
    paths = sorted(real.glob('kaipan_plate_members_2026-10-28_*_i0_*.json'))[:2]
    responses = {path.name.split('_')[4]: json.loads(path.read_text()) for path in paths}
    codes = list(responses)

    class Replay:
        def get_his_plate_ids(self, plate, day, index, size):
            return responses[plate]

    partial = KaipanProvider(Replay(), plate_codes=codes, max_requests=1,
                             sleep_fn=lambda _: None).plate_members('2026-10-28', concurrency=1)
    assert partial['partial'] and len(partial['failed_plates']) == 1
    monkeypatch.setattr(run_data_jobs, '_plate_registry_rows',
                        lambda *args: [{'plate_code': code} for code in codes])
    monkeypatch.setattr(KaipanProvider, 'plate_members', lambda *args, **kwargs: partial)
    with pytest.raises(RuntimeError) as exc:
        run_data_jobs._refresh_plate_members_latest('2026-10-28', str(tmp_path),
                                                   persist=False, max_requests=1)
    message = str(exc.value)
    assert message.startswith('kaipan_plate_members_incomplete_retry_all:')
    for part in ('failed=1', 'completed=1', 'queried=2', 'unqueried=1',
                 'query_trade_date=2026-10-28', 'requests=1', 'elapsed_ms=',
                 'failure_types={"RuntimeError":1}', f'failed_plate_codes={codes[1]}'):
        assert part in message


def test_plate_members_aborts_on_initial_real_error_response():
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    class Replay:
        def get_his_plate_ids(self, plate, day, index, size):
            return {'errcode': '1020', 'errmsg': '参数出错'}

    codes = [f'801{i:03d}' for i in range(50)]
    result = KaipanProvider(Replay(), plate_codes=codes, max_requests=100,
                            sleep_fn=lambda _: None).plate_members('2026-11-24')
    assert result['aborted_early'] is True
    assert result['partial'] is True
    assert result['completed_plate_count'] == 0
    assert result['unqueried_plate_count'] == 50
    assert result['request_count'] <= 14
    assert all(row['error_type'] == 'ValueError' and '1020' in row['error_message']
               for row in result['failed_plates'])


def test_plate_members_success_prevents_initial_abort():
    from pathlib import Path
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    fixture = next((Path(__file__).parent / 'fixtures' / 'real').glob(
        'kaipan_plate_members_2026-10-28_*_i0_*.json'))
    response = json.loads(fixture.read_text())
    codes = [f'801{i:03d}' for i in range(20)]

    class Replay:
        def get_his_plate_ids(self, plate, day, index, size):
            return response if plate == codes[0] else {'errcode': '1020', 'errmsg': '参数出错'}

    result = KaipanProvider(Replay(), plate_codes=codes, max_requests=100,
                            sleep_fn=lambda _: None).plate_members('2026-10-28', concurrency=1)
    assert result['aborted_early'] is False
    assert result['completed_plate_count'] == 1
    assert result['request_count'] == len(codes)
    assert len(result['failed_plates']) == len(codes) - 1
