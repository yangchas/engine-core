"""D-1 limit-up pool: one ladder rung per call (C580).

The guardian ladder (schedule.json profile ``limit_pool_d1``) owns the order, attempts, backoff and timeout of the
rungs; each rung calls ``run_pool_tier`` once with ``args.source`` naming exactly the one source that rung may read
(``kaipan`` / ``wencai`` / ``td``).  Nothing here knows which rung comes first and nothing falls back by itself: only
a downgrade written in the ladder happens.

Thin adapter (design/24 section 1): config -> request -> pure core (``data_pipeline.limit_pool``) -> catalog commit
(transitional ledger) -> TD sink (cold-data store).  Exit codes: 0 ok / 10 degraded / 20 not applicable (a version of
equal or better quality exists) / 30 retryable / 40 deterministic (``TIER_NO_DATA`` and ``TIER_DATE_MISMATCH``
advance to the next rung at once) / 64 configuration.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..data_pipeline import limit_pool as core
from ..data_pipeline.limit_pool import PoolSourceError

EVENT_ID = "limit_pool_d1"
SOURCE_PROVIDER = {"kaipan": core.KAIPAN, "wencai": core.WENCAI, "td": core.TD_DERIVED}
AS_OF_TIME = "15:00:00+08:00"          # the pool describes the close of its trade date
KAIPAN_MAX_LEVELS = 30


class _Config(Exception):
    pass


def reply(exit_code: int, status: str, code: str, retryable: bool, detail: Any) -> tuple[int, dict]:
    return exit_code, {"contract": 1, "status": status, "error_code": code, "retryable": retryable,
                       "artifacts": [], "detail": detail}


class _LazyKaipanClient:
    def _sdk(self):
        import importlib
        return importlib.import_module("pykaipan.pykaipan")

    def get_his_bans(self, day: str, level: int) -> Any:
        return self._sdk().getHisBans(date=day, ban=str(level), size="2000")


def fetch_kaipan_pages(day: str, args: Mapping[str, Any], client: Any = None) -> dict[str, Any]:
    from ..data_pipeline.providers.kaipan_provider import KaipanProvider, ProviderCircuitOpen
    provider = KaipanProvider(client or _LazyKaipanClient(), max_requests=int(args.get("kaipan_max_requests", 30)),
                              min_interval_seconds=float(args.get("kaipan_min_interval_seconds", 10)))
    try:
        result = provider.limit_up_pool(day, max_levels=KAIPAN_MAX_LEVELS, not_ready_ok=True)
    except ProviderCircuitOpen as exc:
        raise PoolSourceError("PROVIDER_BLOCKED", str(exc), exit_code=40) from None
    except (RuntimeError, ValueError) as exc:
        raise PoolSourceError("UPSTREAM_ERROR", str(exc)[:200], exit_code=30, retryable=True) from None
    return {"pages": result["pages"], "request_count": result["request_count"]}


@dataclass
class PoolDeps:
    """Everything that touches the outside world; tests inject fakes, production uses the defaults."""
    kaipan: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] = fetch_kaipan_pages
    wencai: Callable[..., Mapping[str, Any]] | None = None
    td_bars: Callable[[str], Mapping[str, Any]] | None = None
    universe: Callable[[Path, str], tuple[Any, Any]] | None = None
    td_store: Callable[[], Any] | None = None
    now_utc: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


def _args(request: Mapping[str, Any]) -> dict[str, Any]:
    return dict(request["args"]) if isinstance(request.get("args"), Mapping) else {}


def _hhmmss(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        time.strptime(text, "%H:%M:%S")
    except ValueError:
        raise _Config(f"{name}_must_be_HH:MM:SS") from None
    return text


def _local_clock(request: Mapping[str, Any]) -> str | None:
    now = request.get("now")
    if not now:
        return None
    try:
        return datetime.fromisoformat(str(now)).strftime("%H:%M:%S")
    except ValueError:
        return None


def existing_rank(root: str | Path, day: str) -> int | None:
    """Best (lowest) provider rank among committed contract-v2 versions of ``day``; None when none."""
    from ..data_pipeline.storage import ArtifactCatalog
    entries = ArtifactCatalog(root).read().get("datasets", {}).get(core.DATASET, {}).get("dates", {}).get(day, [])
    best = None
    for item in entries:
        try:
            import json
            manifest = json.loads(Path(str(item.get("manifest") or "")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) < core.SCHEMA_VERSION \
                or manifest.get("sample_scope"):
            continue
        rank = core.PROVIDER_RANK.get(str(manifest.get("provider")))
        if rank is not None and (best is None or rank < best):
            best = rank
    return best


def _td_sync(root: Path, day: str, deps: PoolDeps) -> dict[str, Any]:
    """Copy the best catalog version into TD.  Best effort: the catalog write already decided success."""
    from ..data_pipeline.limit_pool_td import LimitPoolTdSink, PoolTaosStore
    from ..data_pipeline.td_sink import TdUnavailable, TdVerifyError
    from .daily_kline_coverage import td_settings
    store = None
    try:
        if deps.td_store is not None:
            store = deps.td_store()
        else:
            settings = td_settings()
            if settings is None or not os.environ.get("TD_CONFIG"):
                return {"status": "skipped", "reason": "td_not_configured"}
            store = PoolTaosStore(str(settings["database"]))
        outcome = LimitPoolTdSink(store, root).sync_date(day)
        return {"status": "ok", "outcome": outcome.as_dict()}
    except TdUnavailable as exc:
        return {"status": "unavailable", "error_code": "TD_UNAVAILABLE", "error": str(exc)[:200]}
    except TdVerifyError as exc:
        return {"status": "verify_failed", "error_code": "TD_VERIFY_FAILED", "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - never break a fetch for a sink bug
        return {"status": "error", "error_code": "TD_SYNC_ERROR", "error": type(exc).__name__ + ":" + str(exc)[:200]}
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def _build(source: str, day: str, args: Mapping[str, Any], root: Path, deps: PoolDeps,
           clock: str | None) -> tuple[core.Partition, dict[str, Any]]:
    """Fetch + pure build for one source.  Returns the partition and extra provider detail."""
    if source == "kaipan":
        try:
            fetched = deps.kaipan(day, args)
            partition = core.build_kaipan(day, fetched["pages"])
        except PoolSourceError as exc:
            until = _hhmmss(args.get("retry_until"), "retry_until")
            if exc.code == "KAIPAN_NOT_READY" and until and clock and clock >= until:
                # The configured wait for the vendor is over: no data for this rung, the ladder moves on.
                raise PoolSourceError("TIER_NO_DATA", f"kaipan_not_published_by_{until}", exit_code=40) from None
            raise
        return partition, {"request_count": fetched.get("request_count")}
    if source == "wencai":
        if deps.wencai is None:
            from ..data_pipeline.providers.limit_pool_sources import fetch_wencai_pool
            wencai = fetch_wencai_pool
        else:
            wencai = deps.wencai
        fetched = wencai(day, cookie_file=args.get("wencai_cookie_file"),
                         max_requests=int(args.get("wencai_max_total_requests", 3)),
                         cooldown_seconds=float(args.get("wencai_cooldown_seconds", 60)),
                         cooldown_max_seconds=float(args.get("wencai_cooldown_max_seconds", 300)),
                         max_cooldowns=int(args.get("wencai_max_cooldowns", 2)))
        partition = core.build_wencai(day, fetched["records"], reported_total=fetched.get("reported_total"))
        return partition, {"request_count": fetched.get("request_count"), "cookie_status": fetched.get("cookie_status")}
    if source == "td":
        if deps.td_bars is None:
            from ..data_pipeline.providers.limit_pool_sources import read_td_bars
            bars = read_td_bars(day, lookback_calendar_days=int(args.get("td_lookback_calendar_days", 40)))
        else:
            bars = deps.td_bars(day)
        if deps.universe is None:
            from ..data_pipeline.providers.limit_pool_sources import universe_names
            names, st_source = universe_names(root, day)
        else:
            names, st_source = deps.universe(root, day)
        partition = core.build_td_derived(day, bars, names=names, st_source=st_source)
        return partition, {"td_symbols": len(bars)}
    raise _Config("args.source_must_be_kaipan_wencai_or_td")


def run_ladder_once(day: str, root: str | Path, *, schedule_path: str | Path | None = None,
                    deps: PoolDeps | None = None) -> tuple[int, dict]:
    """One pass over the configured rungs, no waiting: the path of ``fetch_item`` / ``boot_reconcile`` / the 08:50 job
    when the event itself is long over.  The rung list is read from schedule.json (profile of event
    ``limit_pool_d1``), so order and rung args stay config-driven; attempts/backoff belong to the event itself."""
    import json
    path = Path(schedule_path) if schedule_path else Path(__file__).with_name("schedule.json")
    schedule = json.loads(path.read_text(encoding="utf-8"))
    event = next((e for e in schedule["events"] if e["event_id"] == EVENT_ID), None)
    if event is None:
        return reply(64, "failed", "CONFIG_ERROR", False, f"event_{EVENT_ID}_not_in_schedule")
    ladder = schedule["profiles"][event["profile"]]["ladder"]
    tried: list[dict[str, Any]] = []
    for rung in ladder:
        code, body = run_pool_tier({"data_date": day, "args": rung.get("args") or {}, "tier": rung["name"]}, root,
                                   deps=deps)
        tried.append({"tier": rung["name"], "exit_code": code, "error_code": body.get("error_code"),
                      "status": body.get("status")})
        if code in (0, 10, 20):
            body["detail"] = {**(body["detail"] if isinstance(body["detail"], dict) else {"note": body["detail"]}),
                              "ladder_pass": tried}
            return code, body
    return reply(40, "failed", "TIER_NO_DATA", False, {"partition": day, "ladder_pass": tried,
                                                       "reason": "every configured rung failed; pool MISSING"})


def run_pool_tier(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False,
                  deps: PoolDeps | None = None) -> tuple[int, dict]:
    from ..data_pipeline.storage import ArtifactCatalog
    from ..data_pipeline.td_sink import TdUnavailable

    deps = deps or PoolDeps()
    root = Path(root).expanduser()
    args = _args(request)
    day = str(request.get("data_date") or "")
    try:
        source = str(args.get("source") or "")
        if source not in SOURCE_PROVIDER:
            raise _Config("args.source_must_be_kaipan_wencai_or_td")
        provider = SOURCE_PROVIDER[source]
        chain = [item.strip() for item in str(args.get("chain") or provider).split(",") if item.strip()]
        if provider not in chain or any(item not in core.PROVIDER_RANK for item in chain):
            raise _Config("args.chain_must_list_known_providers_including_this_one")
        if not day:
            raise _Config("data_date_required")
        _hhmmss(args.get("retry_until"), "retry_until")
    except _Config as exc:
        return reply(64, "failed", "CONFIG_ERROR", False, str(exc))
    wanted = core.PROVIDER_RANK[provider]
    have = existing_rank(root, day)
    if have is not None and have <= wanted:
        sync = _td_sync(root, day, deps) if not dry_run else {"status": "dry_run"}
        return reply(20, "not_applicable", "ALREADY_SATISFIED", False,
                     {"partition": day, "existing_rank": have, "wanted_rank": wanted, "source": source,
                      "td_sync": sync})
    if dry_run:
        return reply(0, "not_applicable", "", False, "guardian_dry_run_no_fetch")
    started = time.monotonic()
    fetched_at = deps.now_utc()
    try:
        partition, extra = _build(source, day, args, root, deps, _local_clock(request))
    except PoolSourceError as exc:
        return reply(exc.exit_code, "failed", exc.code, exc.retryable,
                     {"source": source, "partition": day, "reason": exc.detail})
    except TdUnavailable as exc:
        return reply(30, "failed", "TD_UNAVAILABLE", True, {"source": source, "reason": str(exc)[:200]})
    except Exception as exc:  # noqa: BLE001 - an unclassified bug must be loud, not retried forever
        return reply(40, "failed", "DETERMINISTIC_FAILURE", False,
                     {"source": source, "reason": type(exc).__name__ + ":" + str(exc)[:200]})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    manifest = dict(partition.manifest)
    manifest.update(
        provider_chain=chain,
        provider_attempts=[{"provider": provider, "result": "ok", "elapsed_ms": elapsed_ms, "reason": "",
                            "row_count": len(partition.rows)}],
        fetched_at=fetched_at.isoformat().replace("+00:00", "Z"),
        captured_at=fetched_at.isoformat().replace("+00:00", "Z"),
        as_of=f"{day}T{AS_OF_TIME}", source_asof=f"{day}T{AS_OF_TIME}", ladder_tier=str(request.get("tier") or ""))
    manifest["provider_details"] = {**manifest.get("provider_details", {}), **extra}
    committed = ArtifactCatalog(root).commit(core.DATASET, day, partition.rows, manifest, source=provider,
                                             authority="LIVE", clock=deps.now_utc)
    sync = _td_sync(root, day, deps)
    detail = {"partition": day, "source": provider, "version": committed.get("version"),
              "row_count": committed.get("row_count"), "business_status": committed.get("business_status"),
              "coverage": committed["quality"].get("coverage"), "td_sync": sync, "tier": request.get("tier")}
    degraded = bool(manifest.get("degraded"))
    return reply(10 if degraded else 0, "degraded" if degraded else "ok", "", False, detail)
