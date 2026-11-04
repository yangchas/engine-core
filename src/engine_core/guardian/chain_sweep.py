"""Scheduled entry of the dataset chain sweep (P1): returns the contract exit code and a JSON detail."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from ..data_pipeline.chain import Runner, exit_code, load_chain_config, make_status_fn, read_manifest_file, sweep

DEFAULT_BUDGET_SECONDS = 1200.0


def registry_runner(root: str | Path) -> Runner:
    """Build one dataset/day with its registry ``fetch_job``; the answer is the job's exit code, nothing else."""
    from .registry import load_registry
    from .startup import FetchJobError, _fetch_spec, _run_fetch_job

    items = {entry["id"]: entry for entry in load_registry()}

    def run(dataset: str, day: str) -> int:
        item = items[dataset]
        timeout = _fetch_spec(item)[1]
        try:
            _run_fetch_job(item, day, timeout, root)
        except FetchJobError as exc:
            code = str(getattr(exc, "error_code", ""))
            from ..errors import lookup
            spec = lookup(code)
            if spec is not None and not spec.get("reserved"):
                # a declared, registered code says it directly; the sweep's own convention is 20 satisfied / 30 retry / 40 final
                return 20 if spec["exit"] == 20 else (30 if spec["retryable"] else 40)
            if code.startswith("FETCH_EXIT_") and code[len("FETCH_EXIT_"):].isdigit():
                return int(code[len("FETCH_EXIT_"):])
            return 30 if code == "FETCH_TIMEOUT" else 40
        return 0
    return run


def run(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False,
        runner: Runner | None = None, lease: Any = None,
        snapshot_fn: Callable[[], Mapping[str, Any]] | None = None) -> tuple[int, dict[str, Any]]:
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    settings = args.get("chain") if isinstance(args.get("chain"), Mapping) else {}
    today = str(request.get("trade_date") or "")
    if not today or not settings.get("name"):
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR", "retryable": False,
                    "artifacts": [], "detail": "trade_date_and_chain_name_required"}
    from .registry import load_registry
    with_job = {entry["id"] for entry in load_registry() if entry.get("fetch_job")}
    try:
        config = load_chain_config(str(settings["name"]), registry_ids=with_job)
    except ValueError as exc:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR", "retryable": False,
                    "artifacts": [], "detail": str(exc)}
    base = Path(root).expanduser()
    if snapshot_fn is None:
        from ..data_pipeline.storage import ArtifactCatalog
        snapshot_fn = ArtifactCatalog(base).read
    if lease is None and not dry_run and str(settings.get("lease", "redis")).lower() != "off":
        import redis
        from ..data_pipeline.lease import RedisLease
        url = os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        lease = RedisLease(redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5),
                           f"core:lease:chain_sweep:{config.name}", ttl_seconds=int(settings.get("lease_ttl_seconds", 300)))
    result = sweep(snapshot_fn, config, today=today, runner=runner or registry_runner(base),
                   status_fn=make_status_fn(read_manifest_file), lease=lease,
                   dry_run=dry_run or bool(settings.get("plan_only")),
                   budget_seconds=float(settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS)))
    code = exit_code(result)
    if code == 0:
        return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False, "artifacts": [], "detail": result}
    if code == 20:
        return 20, {"contract": 1, "status": "not_applicable", "error_code": "ALREADY_SATISFIED", "retryable": False,
                    "artifacts": [], "detail": result}
    if code == 30:
        return 75, {"contract": 1, "status": "failed", "error_code": "CHAIN_RETRY", "retryable": True,
                    "artifacts": [], "detail": result}
    return 40, {"contract": 1, "status": "failed", "error_code": "CHAIN_FAILED", "retryable": False,
                "artifacts": [], "detail": result}
