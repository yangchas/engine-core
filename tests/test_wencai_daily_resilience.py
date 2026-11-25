"""C569: cookie (optional), same-page cooldown resume, market-cap segmented fallback. Offline only."""
from __future__ import annotations

import json
import logging

import pytest

from engine_core.data_pipeline.providers import wencai_daily_adapter as adapter
from engine_core.data_pipeline.providers.wencai_daily_adapter import fetch_daily_kline

DAY = "2026-10-26"
COMPACT = "20261026"
COOKIE = "SECRETCOOKIE-v=Aabc123XYZ; other=zzTOPSECRET"
N = 55            # stocks; stock i (0-based) has circulating market cap i+1 yi
PAGE = 10


def _row(i: int, date: str = COMPACT) -> dict:
    return {
        "股票代码": f"{600000 + i}.SH",
        f"开盘价:不复权[{date}]": 10.0 + i, f"最高价:不复权[{date}]": 11.0 + i,
        f"最低价:不复权[{date}]": 9.0 + i, f"收盘价:不复权[{date}]": 10.5 + i,
        f"前收盘价:不复权[{date}]": 10.0 + i, f"涨跌幅:前复权[{date}]": 1.5,
        f"成交量[{date}]": 1000 + i, f"成交额[{date}]": 5000.0 + i,
    }


class Upstream:
    """Fake WenCai. Paged (whole-market) queries truncate until the virtual clock reaches ``heal_at``;
    segment queries (they mention 流通市值) truncate until ``segment_heal_at``."""

    def __init__(self, *, heal_at=0.0, segment_heal_at=0.0, truncate_from=3, boundary_inclusive=False,
                 cookie_rejected=False, segment_date=COMPACT, total=N):
        self.now = 0.0
        self.heal_at, self.segment_heal_at = heal_at, segment_heal_at
        self.truncate_from, self.boundary_inclusive = truncate_from, boundary_inclusive
        self.cookie_rejected, self.segment_date, self.total = cookie_rejected, segment_date, total
        self.calls: list[dict] = []

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self, **kw):
        self.calls.append(kw)
        if kw.get("cookie") and self.cookie_rejected:
            raise RuntimeError(f"login required for {kw['cookie']}")
        query, page, perpage = kw["query"], kw["page"], kw["perpage"]
        segment = "流通市值" in query
        members = list(range(self.total))
        date = COMPACT
        if segment:
            date = self.segment_date
            lo, hi = 0.0, float("inf")
            for part in query.split(";")[1:]:
                if part.startswith("流通市值大于"):
                    lo = float(part[len("流通市值大于"):-1])
                elif part.startswith("流通市值小于"):
                    hi = float(part[len("流通市值小于"):-1])
            members = [i for i in members if (lo <= i + 1 <= hi if self.boundary_inclusive else lo < i + 1 < hi)]
            healed = self.now >= self.segment_heal_at
        else:
            healed = self.now >= self.heal_at
        window = members[(page - 1) * perpage: page * perpage]
        if not healed and page >= self.truncate_from:
            window = []
        return {"data": [_row(i, date) for i in window], "row_count": len(members)}


def _run(up, **kw):
    options = dict(query_fn=up, page_size=PAGE, max_pages=20, sleep_fn=up.sleep, clock_fn=lambda: up.now,
                   cooldown_seconds=60.0, segment_edges_yi=(0, 20, 40, None), segment_max_rows=100)
    options.update(kw)
    return fetch_daily_kline(DAY, **options)


def _symbols(payload) -> list[str]:
    from engine_core.wencai_fallback import _symbol
    return [_symbol(r) for r in payload["rows"]]


def _universe():
    return [f"{600000 + i}" for i in range(N)]


# ---- scenarios (also used by the mutation tests) ------------------------------------------------------------

def scenario_cooldown_resume():
    up = Upstream(heal_at=100.0)           # truncated from page 3 until 100 virtual seconds have passed
    sleeps: list[float] = []
    real_sleep = up.sleep
    payload = _run(up, sleep_fn=lambda s: (sleeps.append(s), real_sleep(s))[1])
    assert payload["fetch_mode"] == "paged"
    assert sleeps == [60.0, 120.0]          # exponential cooldown, then the same page succeeds
    assert payload["cooldown_count"] == 2
    assert payload["partial"] is False and len(payload["rows"]) == N
    pages = [c["page"] for c in up.calls]
    assert pages == [1, 2, 3, 3, 3, 4, 5, 6]   # only page 3 is re-requested; no whole restart
    assert len(set(_symbols(payload))) == N
    return payload


def scenario_dedupe_across_segments():
    up = Upstream(heal_at=1e9, boundary_inclusive=True)     # whole-market paging never recovers
    payload = _run(up, max_cooldowns_per_page=1, universe_symbols=_universe())
    assert payload["fetch_mode"] == "segmented" and payload["partial"] is False
    symbols = _symbols(payload)
    assert len(symbols) == len(set(symbols)) == N           # boundary stocks (20, 40 yi) were fetched twice
    assert payload["duplicate_rows"] >= 2
    assert payload["universe_coverage"] == 1.0
    return payload


def scenario_cookie_hygiene(tmp_path, caplog, capsys):
    cookie_file = tmp_path / "wencai.cookie"
    cookie_file.write_text(COOKIE + "\n", encoding="utf-8")
    cookie_file.chmod(0o600)
    up = Upstream(heal_at=100.0)
    with caplog.at_level(logging.DEBUG):
        payload = _run(up, cookie_file=cookie_file, checkpoint_path=tmp_path / "ckpt.json",
                       universe_symbols=_universe(), max_total_requests=4)
    assert all(c.get("cookie") == COOKIE for c in up.calls)        # it IS sent upstream
    assert payload["cookie_status"] == "used" and payload["cookie_length"] == len(COOKIE)
    out = capsys.readouterr()
    haystack = "\n".join([out.out, out.err, caplog.text, json.dumps(payload, ensure_ascii=False, default=str),
                          (tmp_path / "ckpt.json").read_text(encoding="utf-8")])
    assert "SECRETCOOKIE" not in haystack and "TOPSECRET" not in haystack and COOKIE not in haystack
    assert "cookie: used" in caplog.text
    return payload


# ---- behaviour -----------------------------------------------------------------------------------------------

def test_truncated_page_cools_down_and_resumes_the_same_page():
    scenario_cooldown_resume()


def test_without_cookie_file_fetch_is_anonymous_and_not_an_error(tmp_path, caplog):
    up = Upstream()
    with caplog.at_level(logging.INFO):
        payload = _run(up, cookie_file=tmp_path / "missing.cookie")
    assert payload["cookie_status"] == "absent" and payload["cookie_length"] == 0
    assert all("cookie" not in c for c in up.calls)
    assert payload["partial"] is False and len(payload["rows"]) == N
    assert "cookie: absent" in caplog.text


def test_cookie_value_never_appears_in_output(tmp_path, caplog, capsys):
    scenario_cookie_hygiene(tmp_path, caplog, capsys)


def test_expired_cookie_falls_back_to_anonymous_with_one_warning(tmp_path, caplog, capsys):
    cookie_file = tmp_path / "wencai.cookie"
    cookie_file.write_text(COOKIE, encoding="utf-8")
    up = Upstream(cookie_rejected=True)
    with caplog.at_level(logging.INFO):
        payload = _run(up, cookie_file=cookie_file)
    assert payload["cookie_status"] == "expired" and payload["partial"] is False and len(payload["rows"]) == N
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "expired" in warnings[0].getMessage()
    assert sum(1 for c in up.calls if c.get("cookie")) == 1          # tried once, never again
    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err + json.dumps(payload, ensure_ascii=False, default=str)
    assert "SECRETCOOKIE" not in everything and COOKIE not in everything


def test_valid_cookie_but_failing_upstream_is_not_blamed_on_the_cookie(tmp_path):
    cookie_file = tmp_path / "wencai.cookie"
    cookie_file.write_text(COOKIE, encoding="utf-8")

    def broken(**_kw):
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        fetch_daily_kline(DAY, query_fn=broken, cookie_file=cookie_file, sleep_fn=lambda _s: None)


def test_persistent_truncation_falls_back_to_market_cap_segments_and_dedupes():
    scenario_dedupe_across_segments()


def test_segments_that_stay_truncated_give_tier_partial(tmp_path):
    up = Upstream(heal_at=1e9, segment_heal_at=1e9, truncate_from=2)
    payload = _run(up, max_cooldowns_per_page=1, universe_symbols=_universe(),
                   segment_edges_yi=(0, 20, 40, None), segment_min_width_yi=100.0)
    assert payload["fetch_mode"] == "segmented"
    assert payload["partial"] is True and payload["error_code"] == "TIER_PARTIAL" and payload["retryable"] is True
    assert payload["universe_coverage"] < 0.95
    assert 0 < len(payload["rows"]) < N


def test_every_segment_checks_its_column_dates():
    up = Upstream(heal_at=1e9, segment_date="20261027")      # segment columns carry the next day
    payload = _run(up, max_cooldowns_per_page=1, universe_symbols=_universe())
    assert payload["partial"] is True and payload["error_code"] == "TIER_PARTIAL"
    reasons = {s.get("reason") for s in payload["segments"]}
    assert reasons == {"date_mismatch"}
    # no wrong-day row leaked into the merge (only the primary pages 1-2 remain)
    assert all("[20261027]" not in "".join(r) for r in payload["rows"])


def test_oversize_segment_is_split_before_paging():
    up = Upstream(heal_at=1e9, boundary_inclusive=True)
    payload = _run(up, max_cooldowns_per_page=1, universe_symbols=_universe(), segment_max_rows=15,
                   segment_edges_yi=(0, 40, None))
    assert payload["partial"] is False
    assert any(s.get("split") and s["reason"] == "oversize" for s in payload["segments"])
    assert len(set(_symbols(payload))) == N


def test_segment_fallback_can_be_disabled():
    up = Upstream(heal_at=1e9)
    payload = _run(up, max_cooldowns_per_page=1, segment_enabled=False)
    assert payload["fetch_mode"] == "paged" and payload["partial"] is True
    assert payload["error_code"] == "TIER_PARTIAL"
    assert not any("流通市值" in c["query"] for c in up.calls)


def test_request_budget_caps_total_requests_and_checkpoint_resumes(tmp_path):
    ckpt = tmp_path / "pages.json"
    up = Upstream(heal_at=1e9)
    first = _run(up, max_total_requests=4, segment_enabled=False, checkpoint_path=ckpt)
    assert len(up.calls) == 4 and first["partial"] is True and first["reason"] == "request_budget_exhausted"
    assert ckpt.exists()
    up2 = Upstream()                                          # upstream healthy on the next attempt
    second = _run(up2, checkpoint_path=ckpt)
    assert second["partial"] is False and len(second["rows"]) == N
    assert [c["page"] for c in up2.calls][0] > 1              # cached pages are not fetched again
    assert not ckpt.exists()                                  # cleared on success


def test_segment_first_page_timeout_is_cooled_down_and_retried():
    up = Upstream(heal_at=1e9, boundary_inclusive=True)
    seen = {"failed": False}

    def flaky(**kw):
        if "流通市值小于20" in kw["query"] and kw["page"] == 1 and not seen["failed"]:
            seen["failed"] = True
            raise TimeoutError("ReadTimeout")
        return up(**kw)
    payload = _run(up, query_fn=flaky, max_cooldowns_per_page=1, universe_symbols=_universe())
    assert seen["failed"] and payload["partial"] is False and len(set(_symbols(payload))) == N
    assert payload["cooldown_count"] >= 1


def test_throttle_bounds_are_validated():
    with pytest.raises(ValueError):
        fetch_daily_kline(DAY, query_fn=Upstream(), cooldown_factor=0.5)
    with pytest.raises(ValueError):
        fetch_daily_kline(DAY, query_fn=Upstream(), segment_edges_yi=(10, 20, None))


# ---- wiring ---------------------------------------------------------------------------------------------------

def test_schedule_ladder_args_become_cli_flags():
    from types import SimpleNamespace
    from pathlib import Path
    from engine_core.guardian.commands import build_handler_command

    schedule = json.loads((Path(adapter.__file__).parents[2] / "guardian" / "schedule.json").read_text(encoding="utf-8"))
    rung = schedule["profiles"]["daily_kline_close"]["ladder"][0]
    assert rung["name"] == "wencai" and rung["args"]["wencai_cookie_file"] == "~/core-data/secrets/wencai.cookie"
    event = SimpleNamespace(timeout_seconds=1500, args=rung["args"])
    opts = SimpleNamespace(job="daily_kline", write=True, simulate_clock=False)
    repo = Path(adapter.__file__).parents[4]
    cmd = build_handler_command("kaipan_job", event, opts, Path("/x"), DAY, "py", repo)
    joined = " ".join(cmd)
    for flag in ("--wencai-cookie-file ~/core-data/secrets/wencai.cookie", "--wencai-cooldown-seconds 60",
                 "--wencai-max-total-requests 150", "--wencai-segments on",
                 "--wencai-segment-edges-yi 0,15,25,35,50,80,150,500,none"):
        assert flag in joined


# ---- mutations must be caught --------------------------------------------------------------------------------

def test_mutation_no_cooldown_is_caught(monkeypatch):
    monkeypatch.setattr(adapter._Run, "cooldown", lambda self, attempt: None)
    with pytest.raises(AssertionError):
        scenario_cooldown_resume()


def test_mutation_no_dedupe_is_caught(monkeypatch):
    monkeypatch.setattr(adapter, "dedupe_rows", lambda rows, trade_date: (list(rows), 0))
    with pytest.raises(AssertionError):
        scenario_dedupe_across_segments()


def test_mutation_logging_the_cookie_is_caught(monkeypatch, tmp_path, caplog, capsys):
    original = adapter._Run.call

    def leaky(self, question, page, **kw):
        adapter.logger.info("request headers cookie=%s", self.cookie)
        return original(self, question, page, **kw)
    monkeypatch.setattr(adapter._Run, "call", leaky)
    with pytest.raises(AssertionError):
        scenario_cookie_hygiene(tmp_path, caplog, capsys)


def test_mutation_cookie_in_returned_payload_is_caught(monkeypatch, tmp_path, caplog, capsys):
    original = adapter.fetch_daily_kline

    def leaky(*a, **kw):
        payload = original(*a, **kw)
        payload["cookie_debug"] = adapter._read_cookie(kw.get("cookie_file"))
        return payload
    monkeypatch.setitem(globals(), "fetch_daily_kline", leaky)
    with pytest.raises(AssertionError):
        scenario_cookie_hygiene(tmp_path, caplog, capsys)
