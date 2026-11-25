# DATA: real:td/plate_amount_2day.json.gz, td/plate_amount_slice_halving.json.gz, td/errors/query_memory_exhausted_0x073a.json
"""The recorded-TD replay is strict (an unrecorded statement is an error) and faithful (real rows, real errors)."""
import json
from datetime import datetime

import pytest

from tests.replay import FIXTURES, RecordedError, ReplayTd, UnrecordedQuery, load_recording, normalize


def _auction_sql():
    return next(s["sql"] for s in load_recording("plate_amount_2day")["statements"] if "auction_snapshot_v2" in s["sql"])


def test_an_unrecorded_statement_is_an_error_not_an_empty_result():
    cursor = ReplayTd("plate_amount_2day").cursor()
    with pytest.raises(UnrecordedQuery, match="no recording for statement"):
        cursor.execute("SELECT 1")


def test_recorded_rows_come_back_with_their_real_types():
    cursor = ReplayTd("plate_amount_2day").cursor()
    assert cursor.execute(_auction_sql()) == 5223
    rows = cursor.fetchall()
    assert len(rows) == 5223 and isinstance(rows[0][0], datetime) and isinstance(rows[0][1], str)
    assert [d[0] for d in cursor.description] == ["ts", "symbol", "trade_date", "auction_tag", "match_amt_yuan"]


def test_whitespace_and_a_trailing_semicolon_do_not_change_which_statement_matches():
    cursor = ReplayTd("plate_amount_2day").cursor()
    messy = "  " + _auction_sql().replace(" FROM ", "\n   FROM\t") + " ;"
    assert normalize(messy) == _auction_sql()
    assert cursor.execute(messy) == 5223


def test_a_recorded_real_error_is_raised_with_its_real_text():
    real = json.loads((FIXTURES / "errors" / "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))
    failing = next(s for s in load_recording("plate_amount_slice_halving")["statements"] if "error" in s)
    assert failing["error"]["message"] == real["message"] == "[0x073a]: Query memory exhausted"
    with pytest.raises(RecordedError, match=r"\[0x073a\]: Query memory exhausted"):
        ReplayTd("plate_amount_slice_halving").cursor().execute(failing["sql"])


def test_the_statements_the_code_asked_are_logged_in_order():
    connection = ReplayTd("plate_amount_2day")
    cursor = connection.cursor()
    sqls = [s["sql"] for s in load_recording("plate_amount_2day")["statements"]]
    for sql in reversed(sqls):
        cursor.execute(sql)
    assert connection.statements == list(reversed(sqls))
