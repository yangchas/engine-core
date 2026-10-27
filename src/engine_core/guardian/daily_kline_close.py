"""15:05 daily-kline close capture: one ladder rung per call (C566).

The guardian ladder (schedule.json profile ``daily_kline_close``) owns the order, attempts, backoff and
timeout of the rungs; each rung calls this module once with ``args.providers`` naming exactly the sources
that rung may read.  Nothing here knows which rung comes first.  Existing catalog versions of equal or
better quality make the call a no-op (exit 20 ALREADY_SATISFIED), so a restart or a late guardian start
never refetches and never lowers the partition.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from .commands import _daily_kline_provider_arg, build_handler_command, daily_kline_budget_seconds

PROVIDER_RANK = {"baostock": 0, "wencai": 1, "q2_final": 2}
UNKNOWN_RANK = 3
_TIER_CODE_PREFIX = "TIER_"


def _rank(manifest: Mapping[str, Any]) -> int | None:
    if manifest.get("sample_scope"):
        return None
    provider = str(manifest.get("provider") or "")
    if provider in PROVIDER_RANK:
        return PROVIDER_RANK[provider]
    if str(manifest.get("business_status", "")).upper() == "FORMAL" and not manifest.get("degraded"):
        return 0
    source = str(manifest.get("source") or "")
    return PROVIDER_RANK.get(source.replace("legacy_", ""), UNKNOWN_RANK)


def best_existing_rank(root: str | Path, day: str) -> int | None:
    """Best (lowest) quality rank among committed daily_kline versions of ``day``; None when none."""
    from ..data_pipeline.storage import ArtifactCatalog

    entry = ArtifactCatalog(root).read().get("datasets", {}).get("daily_kline", {})
    best = None
    for item in entry.get("dates", {}).get(day, []):
        try:
            manifest = json.loads(Path(str(item.get("manifest") or "")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        rank = _rank(manifest)
        if rank is not None and (best is None or rank < best):
            best = rank
    return best


def _reply(status: str, code: str, retryable: bool, detail: Any, exit_code: int) -> tuple[int, dict]:
    return exit_code, {"contract": 1, "status": status, "error_code": code, "retryable": retryable,
                       "artifacts": [], "detail": detail}


def run_close_tier(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False,
                   runner: Callable[..., Any] = subprocess.run,
                   env: Mapping[str, str] | None = None) -> tuple[int, dict]:
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    try:
        providers = _daily_kline_provider_arg(args.get("providers"))
    except ValueError as exc:
        return _reply("failed", "CONFIG_ERROR", False, str(exc), 64)
    day = str(request.get("data_date") or request.get("trade_date") or "")
    if not day:
        return _reply("failed", "CONFIG_ERROR", False, "trade_date_required", 64)
    wanted = min(PROVIDER_RANK[name] for name in providers.split(","))
    have = best_existing_rank(root, day)
    if have is not None and have <= wanted:
        return _reply("not_applicable", "ALREADY_SATISFIED", False,
                      {"partition": day, "existing_rank": have, "wanted_rank": wanted, "providers": providers}, 20)
    if dry_run:
        return _reply("not_applicable", "", False, "guardian_dry_run_no_fetch", 0)
    repo = Path(__file__).resolve().parents[3]
    timeout = request.get("timeout_seconds")
    event = SimpleNamespace(timeout_seconds=timeout or None, args=args)
    opts = SimpleNamespace(job="daily_kline", write=True, simulate_clock=False)
    cmd = build_handler_command("kaipan_job", event, opts, Path(root), day, sys.executable, repo)
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONPATH"] = str(repo / "src") + os.pathsep + str(repo)
    if "LEGACY_REDIS_URL" not in child_env and child_env.get("REDIS_URL"):
        child_env["LEGACY_REDIS_URL"] = child_env["REDIS_URL"]
    try:
        completed = runner(cmd, cwd=repo, env=child_env, capture_output=True, text=True,
                           timeout=float(timeout) if timeout else None, check=False)
    except subprocess.TimeoutExpired:
        return _reply("failed", "FETCH_TIMEOUT", True, "daily_kline_close_subprocess_timeout", 30)
    lines = [line for line in str(completed.stdout or "").splitlines() if line.strip()]
    contract: Mapping[str, Any] = {}
    try:
        parsed = json.loads(lines[-1]) if lines else {}
        contract = parsed if isinstance(parsed, dict) and parsed.get("contract") == 1 else {}
    except ValueError:
        contract = {}
    code = str(contract.get("error_code") or "")
    detail = {"providers": providers, "partition": day, "returncode": completed.returncode,
              "version": contract.get("version"), "source": contract.get("source"),
              "coverage": contract.get("coverage"), "stderr_tail": str(completed.stderr or "")[-300:]}
    if completed.returncode == 0:
        return _reply("ok", "", False, detail, 0)
    if completed.returncode == 10:
        return _reply("degraded", "", False, detail, 10)
    if completed.returncode == 30 or contract.get("retryable") is True:
        return _reply("failed", code or "RETRYABLE_FAILURE", True, detail, 30)
    if code.startswith(_TIER_CODE_PREFIX):
        return _reply("failed", code, False, detail, 40)
    return _reply("failed", "DETERMINISTIC_FAILURE", False, detail, 40)
