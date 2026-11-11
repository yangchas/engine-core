"""D-1 limit-up pool: one ladder rung per call (C580).

The guardian ladder (schedule.json profile ``limit_pool_d1``) owns the order, attempts, backoff and timeout of the
rungs; each rung calls ``run_pool_tier`` once with ``args.source`` naming exactly the one source that rung may read
(``kaipan`` / ``wencai`` / ``td``).  Nothing here knows which rung comes first and nothing falls back by itself: only
a downgrade written in the ladder happens.

Thin adapter: config -> source -> pure core -> atomic Redis view. Historical TD is a recovery source.  Exit codes: 0 ok / 10 degraded / 20 not applicable (a version of
equal or better quality exists) / 30 retryable / 40 deterministic (``TIER_NO_DATA`` and ``TIER_DATE_MISMATCH``
advance to the next rung at once) / 64 configuration.
"""
from __future__ import annotations

import json
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
    redis_client: Any = None
    calendar_root: str | Path | None = None


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


def existing_partition(root: str | Path, day: str) -> tuple[int, list[dict[str, Any]], dict[str, Any]] | None:
    """Load the already committed result so a retried producer can finish its TD write idempotently."""
    from ..data_pipeline.storage import ArtifactCatalog
    catalog = ArtifactCatalog(root)
    entries = catalog.read().get("datasets", {}).get(core.DATASET, {}).get("dates", {}).get(day, [])
    best = None
    for entry in entries:
        try:
            import json
            manifest = json.loads(Path(str(entry.get("manifest") or "")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) < core.SCHEMA_VERSION \
                or manifest.get("sample_scope"):
            continue
        rank = core.PROVIDER_RANK.get(str(manifest.get("provider")))
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, {**manifest, **entry})
    if best is None:
        return None
    return best[0], catalog.read_rows(Path(str(best[1]["manifest"]))), best[1]


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
        if body.get("error_code") in {"TD_UNAVAILABLE", "TD_VERIFY_FAILED", "TD_SYNC_ERROR", "HOT_PUBLISH_FAILED"}:
            body["detail"] = {**(body["detail"] if isinstance(body["detail"], dict) else {"note": body["detail"]}),
                              "ladder_pass": tried}
            return code, body
    return reply(40, "failed", "TIER_NO_DATA", False, {"partition": day, "ladder_pass": tried,
                                                       "reason": "every configured rung failed; pool MISSING"})


def run_pool_tier(request: Mapping[str, Any], root: str | Path, *, dry_run: bool = False,
                  deps: PoolDeps | None = None) -> tuple[int, dict]:
    from ..data_pipeline import limit_pool_hot_view as hot
    from ..data_pipeline.storage import canonical_json, sha256
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
        datetime.strptime(day, "%Y-%m-%d")
        _hhmmss(args.get("retry_until"), "retry_until")
    except (_Config, ValueError) as exc:
        return reply(64, "failed", "CONFIG_ERROR", False, str(exc))
    wanted = core.PROVIDER_RANK[provider]
    client = deps.redis_client
    try:
        client = client if client is not None else hot._client()
        key = hot._key(day, 'core:view:limit_up_pool')
        raw = client.get(key)
        current = hot._validate(json.loads(raw), day) if raw else None
        now = deps.now_utc()
        deadline = hot.deadline_for(day, deps.calendar_root)
        if (current and core.PROVIDER_RANK[current['provider']] <= wanted
                and hot._instant(current['available_at']) <= now and deadline > int(now.timestamp())):
            outcome = ({"status": "dry_run"} if dry_run else
                       {"status": "unchanged", "version": current['version'], "expire_at": deadline}
                       if hot.expiry_matches(client, key, deadline, now) else
                       hot.publish(day, current['rows'], current, redis_client=client,
                                   calendar_root=deps.calendar_root, clock=deps.now_utc, lock_root=root))
            return reply(20, "not_applicable", "ALREADY_SATISFIED", False,
                         {"partition": day, "existing_rank": core.PROVIDER_RANK[current['provider']],
                          "wanted_rank": wanted, "source": source, "version": current['version'],
                          "hot_write": outcome})
    except Exception:
        current = None
    if dry_run:
        return reply(0, "not_applicable", "", False, "guardian_dry_run_no_fetch")

    # A missing hot view may be restored from the existing physical partition. A
    # failed cold read never prevents a fresh source from publishing.
    if current is None:
        store = None
        try:
            store = deps.td_store() if deps.td_store else hot._td_store()
            recovered = hot._td_value(day, store)
            if (core.PROVIDER_RANK[recovered['provider']] <= wanted and
                    hot._instant(recovered['available_at']) <= deps.now_utc()):
                columns = {
                    core.KAIPAN: core.COMMON_COLUMNS + core.VALUE_COLUMNS + core.KAIPAN_COLUMNS,
                    core.WENCAI: core.COMMON_COLUMNS + core.WENCAI_COLUMNS,
                    core.TD_DERIVED: core.COMMON_COLUMNS + core.VALUE_COLUMNS + core.TD_COLUMNS,
                }[recovered['provider']]
                names = [column[0] for column in columns]
                rows = [{name: row[name] for name in names}
                        for row in sorted(recovered['rows'], key=lambda item: item['symbol'])]
                payload = b'\n'.join(canonical_json(row) for row in rows) + (b'\n' if rows else b'')
                if sha256(payload) != recovered.get('source_sha256'):
                    raise ValueError('td_source_digest_mismatch')
                recovered['degraded'] = True
                outcome = hot.publish(day, rows, recovered, redis_client=client,
                                      calendar_root=deps.calendar_root, clock=deps.now_utc, lock_root=root)
                return reply(20, "not_applicable", "ALREADY_SATISFIED", False,
                             {"partition": day, "source": source, "version": outcome['version'],
                              "recovery_source": "td", "hot_write": outcome})
        except Exception:
            pass  # Recovery is optional; the requested upstream is still tried.
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    started = time.monotonic()
    try:
        partition, extra = _build(source, day, args, root, deps, _local_clock(request))
    except PoolSourceError as exc:
        return reply(exc.exit_code, "failed", exc.code, exc.retryable,
                     {"source": source, "partition": day, "reason": exc.detail})
    except TdUnavailable as exc:
        return reply(30, "failed", "TD_UNAVAILABLE", True, {"source": source, "reason": str(exc)[:200]})
    except Exception as exc:
        return reply(40, "failed", "DETERMINISTIC_FAILURE", False,
                     {"source": source, "reason": type(exc).__name__ + ":" + str(exc)[:200]})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    completed = deps.now_utc().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = b"\n".join(canonical_json(dict(row)) for row in partition.rows) + (b"\n" if partition.rows else b"")
    digest = sha256(payload)
    manifest = {**partition.manifest, "version": "sha256-" + digest[:16], "sha256": digest,
                "available_at": completed, "captured_at": completed, "fetched_at": completed,
                "as_of": f"{day}T{AS_OF_TIME}", "source_asof": f"{day}T{AS_OF_TIME}",
                "provider_chain": chain, "provider_attempts": [{"provider": provider, "result": "ok",
                "elapsed_ms": elapsed_ms, "reason": "", "row_count": len(partition.rows)}],
                "provider_details": {**partition.manifest.get("provider_details", {}), **extra}}
    try:
        outcome = hot.publish(day, partition.rows, manifest, redis_client=client,
                              calendar_root=deps.calendar_root, clock=deps.now_utc, lock_root=root, fresh=True)
    except Exception as exc:
        return reply(30, "failed", "HOT_PUBLISH_FAILED", True,
                     {"partition": day, "source": provider, "hot_write": {"status": "error",
                      "reason": type(exc).__name__ + ":" + str(exc)[:160]}})
    if outcome['status'] == 'preserved_better':
        return reply(20, "not_applicable", "ALREADY_SATISFIED", False,
                     {"partition": day, "version": outcome['version'], "hot_write": outcome})
    detail = {"partition": day, "source": provider, "version": outcome['version'],
              "row_count": len(partition.rows), "business_status": manifest.get("business_status"),
              "coverage": manifest['quality'].get('coverage'), "hot_write": outcome, "tier": request.get("tier")}
    degraded = bool(manifest.get("degraded"))
    return reply(10 if degraded else 0, "degraded" if degraded else "ok", "", False, detail)
