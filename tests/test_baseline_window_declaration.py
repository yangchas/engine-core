from pathlib import Path
import ast
import re


def test_real_28_day_baseline_is_ready_in_panorama():
    from test_auction_baseline import _real_input
    from test_auction_panorama import _facts
    from engine_core.auction_baseline import build_auction_baseline
    from engine_core.auction_panorama import build_auction_panorama

    expected, rows = _real_input()
    built = build_auction_baseline(rows, target_date="2026-11-06",
                                   source_dates=expected["td_dates"],
                                   available_at="2026-11-06T00:00:00+00:00")
    values = [row["auction_amount_yuan"] for row in built["rows"]
              if row["row_kind"] == "market_observation"]
    product = build_auction_panorama(_facts(), plate_members=[], yesterday_pool=[],
                                    market_amount_history={**built["market_summary"],
                                                           "values": values, "business_status": built["status"]})
    assert built["observed_days"] == 28
    assert product["overview"]["amount_multiple_64d"]["status"] == "READY"


def test_baseline_window_literal_has_one_declaration():
    root = Path(__file__).resolve().parents[1]
    violations = []
    # Only these expressions indicate an auction baseline window decision;
    # daily_history's full-day bar alignment, SQL widths and hashes differ.
    pattern = re.compile(r'(?:\bWINDOW_DAYS\s*=\s*64\b|len\([^)]*(?:history_values|baseline)[^)]*\)\s*(?:==|!=|<=|>=|<|>)\s*64\b|INCOMPLETE_64_SESSION_WINDOW)')
    for directory in (root / "src", root / "examples"):
        for path in directory.rglob("*.py"):
            if path.name == "auction_baseline.py":
                continue
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    violations.append(f"{path.relative_to(root)}:{line_no}: {line.strip()}")
    assert not violations, "\n".join(violations)


def test_real_baseline_readers_follow_metadata_and_legacy_status():
    from test_auction_baseline import _real_input
    from test_auction_panorama import _facts
    from engine_core.auction_baseline import build_auction_baseline
    from engine_core.auction_panorama import build_auction_panorama
    from engine_core.auction_quickbrief import build_auction_quickbrief

    expected, rows = _real_input()
    built = build_auction_baseline(rows, target_date="2026-11-06", source_dates=expected["td_dates"],
                                   available_at="2026-11-06T00:00:00+00:00")
    values = [row["auction_amount_yuan"] for row in built["rows"] if row["row_kind"] == "market_observation"]
    product = {**built["market_summary"], "values": values, "business_status": "READY"}
    for observed, status, reason in ((28, "READY", None), (4, "PARTIAL", "baseline_below_min_days"),
                                     (None, "READY", "legacy_baseline_version")):
        candidate = dict(product)
        if observed is None:
            candidate.pop("observed_days")
        else:
            candidate["observed_days"] = observed
        panorama = build_auction_panorama(_facts(), plate_members=[], yesterday_pool=[], market_amount_history=candidate)
        quick = build_auction_quickbrief(_facts(), plate_members=[], yesterday_pool=[], market_amount_history=candidate)
        p = panorama["overview"]["amount_multiple_64d"]
        q = quick["metrics"]["market"]
        assert p["status"] == q["baseline_status"] == status
        assert (reason in p["reasons"]) == (reason is not None)
        assert (reason in q["baseline_reasons"]) == (reason is not None)
    candidate["business_status"] = "PARTIAL"
    assert build_auction_panorama(_facts(), plate_members=[], yesterday_pool=[], market_amount_history=candidate)["overview"]["amount_multiple_64d"]["status"] == "PARTIAL"
    assert build_auction_quickbrief(_facts(), plate_members=[], yesterday_pool=[], market_amount_history=candidate)["metrics"]["market"]["baseline_status"] == "PARTIAL"


def test_daily_history_64_is_daily_bar_amount_alignment():
    import inspect
    from engine_core.daily_history import build_daily_report_features
    source = inspect.getsource(build_daily_report_features)
    assert 'len(all_amount_days) == 64 and all(day in amounts_by_day' in source
    assert '"amount_history_dates_64": all_amount_days if len(all_amount_days) == 64' in source


def test_daily_pipeline_passes_baseline_product_to_both_readers():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "src/engine_core/daily_pipeline.py").read_text())
    pipeline = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "run_daily_pipeline")
    assignments = [node for node in ast.walk(pipeline)
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "baseline_product"
                           for target in node.targets)]
    assert len(assignments) == 1
    product = assignments[0].value
    assert isinstance(product, ast.Call) and isinstance(product.func, ast.Name)
    assert product.func.id == "_auction_baseline_product"
    assert [arg.id for arg in product.args if isinstance(arg, ast.Name)] == [
        "baseline_rows", "baseline_meta", "baseline_history"]
    for reader in ("build_auction_panorama", "build_auction_quickbrief"):
        calls = [node for node in ast.walk(pipeline) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == reader]
        assert len(calls) == 1
        history = next(keyword.value for keyword in calls[0].keywords
                       if keyword.arg == "market_amount_history")
        assert isinstance(history, ast.Name) and history.id == "baseline_product"
