"""Small, service-free contract for subprocess data jobs."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping


_EXIT_CODES = {
    ("ok", False): 0,
    ("degraded", False): 10,
    ("not_applicable", False): 20,
    ("failed", True): 30,
    ("failed", False): 40,
}
_VALID_STATUSES = {"ok", "degraded", "not_applicable", "failed"}
_FIELDS = {
    "contract", "job", "trade_date", "status", "tier", "source", "dataset",
    "version", "row_count", "coverage", "error_code", "retryable", "attempts",
    "elapsed_ms", "detail",
}


@dataclass(frozen=True)
class JobResult:
    contract: int
    job: str
    trade_date: str
    status: str
    tier: int
    source: str | None
    dataset: str
    version: str | None
    row_count: int | None
    coverage: float | None
    error_code: str | None
    retryable: bool
    attempts: int
    elapsed_ms: float
    detail: Any

    def __post_init__(self) -> None:
        if type(self.contract) is not int or self.contract != 1:
            raise ValueError("job_contract_must_be_1")
        if (self.status, self.retryable) not in _EXIT_CODES:
            raise ValueError("job_status_retryable_combination_invalid")

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[(self.status, self.retryable)]


def _failure(job: str = "unknown", trade_date: str = "", *, error_code: str,
             retryable: bool, values: Mapping[str, Any] | None = None) -> JobResult:
    values = values or {}
    return JobResult(
        contract=1,
        job=values.get("job") if isinstance(values.get("job"), str) else job,
        trade_date=(values.get("trade_date") if isinstance(values.get("trade_date"), str)
                    else trade_date),
        status="failed", tier=values.get("tier") if type(values.get("tier")) is int else 0,
        source=values.get("source") if isinstance(values.get("source"), str) else None,
        dataset=values.get("dataset") if isinstance(values.get("dataset"), str) else "",
        version=values.get("version") if isinstance(values.get("version"), str) else None,
        row_count=values.get("row_count") if type(values.get("row_count")) is int else None,
        coverage=(values.get("coverage") if isinstance(values.get("coverage"), (int, float))
                  and not isinstance(values.get("coverage"), bool) else None),
        error_code=error_code, retryable=retryable,
        attempts=values.get("attempts") if type(values.get("attempts")) is int else 0,
        elapsed_ms=(float(values["elapsed_ms"]) if isinstance(values.get("elapsed_ms"), (int, float))
                    and not isinstance(values.get("elapsed_ms"), bool) else 0.0),
        detail=values.get("detail"),
    )


def emit(result: JobResult) -> int:
    """Write a single JSON contract line and return its corresponding process code."""
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")))
    return result.exit_code


def parse(stdout_text: str, returncode: int) -> JobResult:
    """Parse the final stdout JSON line, translating legacy exits safely."""
    if returncode not in {0, 10, 20, 30, 40}:
        return _failure(error_code=f"LEGACY_EXIT_{returncode}", retryable=True)
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        return _failure(error_code="CONTRACT_VIOLATION", retryable=False)
    try:
        value = json.loads(lines[-1])
    except (TypeError, ValueError):
        return _failure(error_code="CONTRACT_VIOLATION", retryable=False)
    if not isinstance(value, dict) or not _FIELDS.issubset(value):
        return _failure(error_code="CONTRACT_VIOLATION", retryable=False)
    if (type(value.get("contract")) is not int or value.get("contract") != 1
            or not isinstance(value.get("job"), str)
            or not isinstance(value.get("trade_date"), str)
            or value.get("status") not in _VALID_STATUSES
            or type(value.get("tier")) is not int or value["tier"] < 0
            or (value.get("source") is not None and not isinstance(value.get("source"), str))
            or not isinstance(value.get("dataset"), str)
            or (value.get("version") is not None and not isinstance(value.get("version"), str))
            or (value.get("row_count") is not None and type(value.get("row_count")) is not int)
            or (value.get("coverage") is not None and
                (not isinstance(value.get("coverage"), (int, float)) or
                 isinstance(value.get("coverage"), bool)))
            or (value.get("error_code") is not None and not isinstance(value.get("error_code"), str))
            or type(value.get("retryable")) is not bool
            or type(value.get("attempts")) is not int or value["attempts"] < 0
            or not isinstance(value.get("elapsed_ms"), (int, float))
            or isinstance(value.get("elapsed_ms"), bool)):
        return _failure(error_code="CONTRACT_VIOLATION", retryable=False, values=value)
    try:
        result = JobResult(**{key: value[key] for key in _FIELDS})
    except (TypeError, ValueError):
        return _failure(error_code="CONTRACT_VIOLATION", retryable=False, values=value)
    if result.status == "failed":
        if result.error_code is None or result.exit_code != returncode:
            return _failure(error_code="CONTRACT_VIOLATION", retryable=False, values=value)
    elif (result.retryable or result.exit_code != returncode
          or (result.status in {"ok", "not_applicable"} and result.error_code is not None)):
        return _failure(error_code="CONTRACT_VIOLATION", retryable=False, values=value)
    return result


def _exception_code(exc: Exception, source: str) -> tuple[str, bool]:
    message = str(exc).strip()
    lowered = message.lower()
    if isinstance(exc, TimeoutError) or "timeout" in lowered or "timed out" in lowered:
        return ("BAOSTOCK_TIMEOUT" if "baostock" in source.lower() else "FETCH_TIMEOUT"), True
    for prefix, retryable in (("quality_invalid:", False), ("schema_invalid:", False)):
        if lowered.startswith(prefix):
            suffix = re.sub(r"[^A-Za-z0-9]+", "_", message[len(prefix):]).strip("_").upper()
            return suffix or prefix[:-1].upper(), retryable
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", message):
        return re.sub(r"[^A-Za-z0-9]+", "_", message).strip("_").upper(), True
    return type(exc).__name__.upper(), True


def run_with_degrade(job: str, trade_date: str,
                     tiers: Iterable[tuple[int, str, Callable[[str], Any]]], *,
                     dataset: str,
                     quality_check: Callable[[Any], bool] | None = None
                     ) -> tuple[JobResult, Any | None]:
    """Run ordered fetch tiers, returning one normalized result and the winning payload."""
    started = time.monotonic()
    failures: list[dict[str, Any]] = []
    last_tier, last_source = 0, None
    quality_check = quality_check or _has_usable_rows
    attempts = 0
    for tier, source, fetch in tiers:
        attempts += 1
        last_tier, last_source = tier, source
        try:
            payload = fetch(trade_date)
            if not quality_check(payload):
                raise _QualityFailure("QUALITY_INVALID")
        except Exception as exc:
            if isinstance(exc, _QualityFailure):
                error_code, retryable = "QUALITY_INVALID", False
            else:
                error_code, retryable = _exception_code(exc, source)
            failures.append({"tier": tier, "source": source, "error_code": error_code,
                             "retryable": retryable})
            continue
        degraded = tier > 0
        result = JobResult(
            contract=1, job=job, trade_date=trade_date,
            status="degraded" if degraded else "ok", tier=tier, source=source,
            dataset=dataset, version=None, row_count=_payload_row_count(payload),
            coverage=None, error_code=failures[0]["error_code"] if failures else None,
            retryable=False, attempts=attempts,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            detail={"failures": failures} if failures else None,
        )
        return result, payload
    if failures:
        final_failure = next((failure for failure in failures if failure["retryable"]),
                             failures[-1])
        result = JobResult(
            contract=1, job=job, trade_date=trade_date, status="failed",
            tier=last_tier, source=last_source, dataset=dataset, version=None,
            row_count=None, coverage=None, error_code=final_failure["error_code"],
            retryable=final_failure["retryable"], attempts=attempts,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            detail={"failures": failures},
        )
        return result, None
    result = JobResult(
        contract=1, job=job, trade_date=trade_date, status="not_applicable",
        tier=0, source=None, dataset=dataset, version=None, row_count=None,
        coverage=None, error_code=None, retryable=False, attempts=0,
        elapsed_ms=round((time.monotonic() - started) * 1000, 3), detail=None,
    )
    return result, None


class _QualityFailure(RuntimeError):
    pass


def _has_usable_rows(payload: Any) -> bool:
    if isinstance(payload, Mapping):
        rows = payload.get("rows")
        return isinstance(rows, (list, tuple)) and bool(rows)
    return isinstance(payload, (list, tuple)) and bool(payload)


def _payload_row_count(payload: Any) -> int | None:
    if isinstance(payload, Mapping):
        rows = payload.get("rows")
        return len(rows) if isinstance(rows, (list, tuple)) else None
    return len(payload) if isinstance(payload, (list, tuple)) else None
