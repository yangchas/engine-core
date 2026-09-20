"""Soft-deadline auction facts and versioned late-correction timeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .clock import local_datetime_ms
from .contracts import evidence_hash, semantic_hash
from .q2 import normalize_symbol


AUCTION_TIMING_POLICY_CONTRACT_VERSION = "AuctionTimingPolicyV1"
AUCTION_ANCHOR_REVISION_CONTRACT_VERSION = "AuctionAnchorRevisionV1"
AUCTION_TIMELINE_CONTRACT_VERSION = "AuctionTimelineV1"

OBSERVING = "OBSERVING"
READY = "READY"
PARTIAL = "PARTIAL"
MISSING = "MISSING"
FACT_ONLY = "FACT_ONLY"


@dataclass(frozen=True)
class AuctionTimingPolicyV1:
    """Versioned business-anchor timing policy with adaptive grace."""

    tag: str
    business_time: str
    first_observable_time: str
    preferred_finalize_time: str
    soft_deadline_time: str
    policy_version: str = AUCTION_TIMING_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.tag not in {"0920", "0924", "0925"}:
            raise ValueError("unsupported auction tag")
        for value in (
            self.business_time,
            self.first_observable_time,
            self.preferred_finalize_time,
            self.soft_deadline_time,
        ):
            if len(value) != 8 or value[2] != ":" or value[5] != ":":
                raise ValueError("auction timing values must be HH:MM:SS")
        if not (
            self.business_time <= self.first_observable_time
            <= self.preferred_finalize_time <= self.soft_deadline_time
        ):
            raise ValueError("auction timing must be monotonic")

    @classmethod
    def default(cls, tag: str) -> "AuctionTimingPolicyV1":
        values = {
            "0920": ("09:20:00", "09:20:03", "09:20:03", "09:20:30"),
            "0924": ("09:24:00", "09:24:10", "09:24:10", "09:24:30"),
            "0925": ("09:25:00", "09:25:06", "09:25:10", "09:25:30"),
        }
        try:
            business, first, preferred, soft = values[tag]
        except KeyError as exc:
            raise ValueError("unsupported auction tag") from exc
        return cls(tag, business, first, preferred, soft)

    @classmethod
    def for_tag(cls, tag: str) -> "AuctionTimingPolicyV1":
        return cls.default(tag)

    def at(self, trade_date: str) -> Mapping[str, int]:
        return {
            "business_anchor_ms": local_datetime_ms(trade_date, self.business_time),
            "first_observable_ms": local_datetime_ms(trade_date, self.first_observable_time),
            "preferred_finalize_ms": local_datetime_ms(trade_date, self.preferred_finalize_time),
            "soft_deadline_ms": local_datetime_ms(trade_date, self.soft_deadline_time),
        }

    def timestamps(self, trade_date: str) -> Mapping[str, int]:
        return self.at(trade_date)


@dataclass(frozen=True)
class AuctionAnchorRevisionV1:
    trade_date: str
    tag: str
    revision: int
    business_anchor_ms: int
    first_observable_ms: int
    preferred_finalize_ms: int
    soft_deadline_ms: int
    state: str
    expected_symbols: Tuple[str, ...]
    observed_symbols: Tuple[str, ...]
    missing_symbols: Tuple[str, ...]
    coverage: float
    source_layers: Tuple[str, ...]
    observed_at_ms: Optional[int]
    evaluation_time_ms: int
    freeze_time_ms: Optional[int]
    source_time_min_ms: Optional[int]
    source_time_max_ms: Optional[int]
    late_execution: bool
    supersedes_revision: Optional[int] = None
    recovery_state: str = "NOT_REQUESTED"
    observations_hash: str = ""
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.tag not in {"0920", "0924", "0925"}:
            raise ValueError("unsupported auction tag")
        if self.revision <= 0 or self.evaluation_time_ms <= 0:
            raise ValueError("revision and evaluation_time_ms must be positive")
        if self.state not in {OBSERVING, READY, PARTIAL, MISSING}:
            raise ValueError("unsupported auction revision state")
        expected = tuple(sorted({normalize_symbol(item) for item in self.expected_symbols}))
        observed = tuple(sorted({normalize_symbol(item) for item in self.observed_symbols}))
        missing = tuple(sorted({normalize_symbol(item) for item in self.missing_symbols}))
        if set(observed) - set(expected) or set(missing) - set(expected):
            raise ValueError("auction symbol sets must be subsets of expected_symbols")
        if set(observed) & set(missing) or set(observed) | set(missing) != set(expected):
            raise ValueError("observed/missing symbols must partition expected_symbols")
        if not 0.0 <= self.coverage <= 1.0:
            raise ValueError("coverage must be between zero and one")
        object.__setattr__(self, "expected_symbols", expected)
        object.__setattr__(self, "observed_symbols", observed)
        object.__setattr__(self, "missing_symbols", missing)
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        object.__setattr__(self, "content_hash", semantic_hash({
            "contract": AUCTION_ANCHOR_REVISION_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "tag": self.tag,
            "business_anchor_ms": self.business_anchor_ms,
            "expected_symbols": expected,
            "observed_symbols": observed,
            "missing_symbols": missing,
            "coverage": self.coverage,
            "observations_hash": self.observations_hash,
        }))
        object.__setattr__(self, "evidence_hash", evidence_hash({
            "revision": self.revision,
            "source_layers": self.source_layers,
            "observed_at_ms": self.observed_at_ms,
            "evaluation_time_ms": self.evaluation_time_ms,
            "freeze_time_ms": self.freeze_time_ms,
            "source_time_min_ms": self.source_time_min_ms,
            "source_time_max_ms": self.source_time_max_ms,
            "late_execution": self.late_execution,
            "supersedes_revision": self.supersedes_revision,
            "recovery_state": self.recovery_state,
        }))

    @property
    def business_anchor(self) -> int:
        return self.business_anchor_ms

    @property
    def source_time_ms(self) -> Optional[int]:
        return self.source_time_max_ms


def _rows_by_symbol(rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    if isinstance(rows, Mapping):
        return {normalize_symbol(symbol): dict(values) for symbol, values in rows.items()}
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("symbol"):
            raise ValueError("auction rows must contain symbol")
        result[normalize_symbol(row["symbol"])] = dict(row)
    return result


def build_auction_anchor_revision(
    trade_date: str,
    tag: str,
    rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    evaluation_time_ms: int,
    expected_symbols: Sequence[Any] = (),
    observed_at_ms: Optional[int] = None,
    source_layers: Sequence[str] = (),
    revision: int = 1,
    supersedes_revision: Optional[int] = None,
    recovery_state: str = "NOT_REQUESTED",
    policy: Optional[AuctionTimingPolicyV1] = None,
) -> AuctionAnchorRevisionV1:
    policy = policy or AuctionTimingPolicyV1.default(tag)
    times = policy.at(trade_date)
    by_symbol = _rows_by_symbol(rows)
    expected = tuple(sorted({normalize_symbol(item) for item in expected_symbols}))
    observed = tuple(sorted(symbol for symbol in by_symbol if not expected or symbol in set(expected)))
    if not expected:
        # Unknown denominator is represented as PARTIAL, never as READY.
        missing = ()
        coverage = 1.0 if observed else 0.0
    else:
        observed = tuple(symbol for symbol in observed if symbol in set(expected))
        missing = tuple(symbol for symbol in expected if symbol not in set(observed))
        coverage = len(observed) / float(len(expected))
    if evaluation_time_ms < times["first_observable_ms"]:
        state = OBSERVING
    elif not observed:
        state = MISSING if evaluation_time_ms >= times["soft_deadline_ms"] else PARTIAL
    elif expected and not missing:
        state = READY
    else:
        state = PARTIAL
    freeze = times["preferred_finalize_ms"] if evaluation_time_ms >= times["preferred_finalize_ms"] else None
    event_times = [
        value.get("source_time_ms", value.get("ts"))
        for value in by_symbol.values()
        if value.get("source_time_ms", value.get("ts")) is not None
    ]
    event_times = [int(item) for item in event_times]
    observations_hash = semantic_hash(by_symbol)
    return AuctionAnchorRevisionV1(
        trade_date=trade_date,
        tag=tag,
        revision=revision,
        business_anchor_ms=times["business_anchor_ms"],
        first_observable_ms=times["first_observable_ms"],
        preferred_finalize_ms=times["preferred_finalize_ms"],
        soft_deadline_ms=times["soft_deadline_ms"],
        state=state,
        expected_symbols=expected,
        observed_symbols=observed,
        missing_symbols=missing,
        coverage=coverage,
        source_layers=tuple(source_layers),
        observed_at_ms=observed_at_ms,
        evaluation_time_ms=evaluation_time_ms,
        freeze_time_ms=freeze,
        source_time_min_ms=min(event_times) if event_times else None,
        source_time_max_ms=max(event_times) if event_times else None,
        late_execution=evaluation_time_ms > times["soft_deadline_ms"],
        supersedes_revision=supersedes_revision,
        recovery_state=recovery_state,
        observations_hash=observations_hash,
    )


class AuctionTimeline:
    """In-memory version ledger for auction facts and late corrections."""

    def __init__(self, trade_date: str, policies: Optional[Mapping[str, AuctionTimingPolicyV1]] = None) -> None:
        self.trade_date = trade_date
        self.policies = dict(policies or {tag: AuctionTimingPolicyV1.default(tag) for tag in ("0920", "0924", "0925")})
        self._history: dict[str, list[AuctionAnchorRevisionV1]] = {tag: [] for tag in self.policies}

    def observe(
        self,
        tag: str,
        rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        *,
        evaluation_time_ms: int,
        expected_symbols: Sequence[Any] = (),
        observed_at_ms: Optional[int] = None,
        source_layers: Sequence[str] = (),
        recovery_state: str = "NOT_REQUESTED",
    ) -> AuctionAnchorRevisionV1:
        if tag not in self.policies:
            raise ValueError("unsupported auction tag")
        history = self._history.setdefault(tag, [])
        candidate = build_auction_anchor_revision(
            self.trade_date,
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected_symbols,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
            revision=(history[-1].revision + 1 if history else 1),
            supersedes_revision=(history[-1].revision if history else None),
            recovery_state=recovery_state,
            policy=self.policies[tag],
        )
        if history and history[-1].content_hash == candidate.content_hash:
            return history[-1]
        history.append(candidate)
        return candidate

    def latest(self, tag: str) -> Optional[AuctionAnchorRevisionV1]:
        history = self._history.get(tag, [])
        return history[-1] if history else None

    def apply_recovery(
        self,
        plan: Any,
        rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        *,
        evaluation_time_ms: int,
        observed_at_ms: Optional[int] = None,
        source: str = "wencai",
    ) -> AuctionAnchorRevisionV1:
        """Apply an already merged recovery cohort as a new fact revision.

        The external recovery owner is responsible for fetching and merging
        only missing fields.  Core records the source layer and remains
        fact-only; repeated cohorts with the same semantic content are
        idempotent because ``observe`` compares content hashes.
        """

        tag = getattr(plan, "tag", None)
        if not isinstance(tag, str):
            raise ValueError("recovery plan must expose tag")
        current = self.latest(tag)
        expected = current.expected_symbols if current is not None and current.expected_symbols else getattr(plan, "requested_symbols", ())
        return self.observe(
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected,
            observed_at_ms=observed_at_ms,
            source_layers=(source,),
            recovery_state="APPLIED",
        )

    def revisions(self, tag: str) -> Tuple[AuctionAnchorRevisionV1, ...]:
        return tuple(self._history.get(tag, ()))

    def build_analysis_bundle(self, tag: str = "0925") -> Mapping[str, Any]:
        current = self.latest(tag)
        if current is None:
            recovery_plan = None
            if tag == "0925":
                from .recovery import build_recovery_plan

                recovery_plan = build_recovery_plan(
                    self.trade_date,
                    tag,
                    requested_symbols=(),
                    missing_fields=("anchor",),
                    current_revision=0,
                    requested_at_ms=local_datetime_ms(self.trade_date, "09:25:06"),
                    reason="0925 anchor is unavailable; request an asynchronous recovery cohort",
                )
            return {
                "status": MISSING,
                "fact_status": FACT_ONLY,
                "anchor": None,
                "prior_deltas": {},
                "recovery_required": tag == "0925",
                "recovery_plan": recovery_plan,
            }
        prior_deltas = {}
        if tag == "0925":
            for prior in ("0920", "0924"):
                prior_deltas[prior] = "UNKNOWN" if self.latest(prior) is None else "AVAILABLE"
        recovery_plan = None
        if current.state in {PARTIAL, MISSING}:
            from .recovery import build_recovery_plan

            recovery_plan = build_recovery_plan(
                self.trade_date,
                tag,
                requested_symbols=current.missing_symbols,
                missing_fields=("anchor",),
                current_revision=current.revision,
                requested_at_ms=current.evaluation_time_ms,
                reason="partial auction cohort; fill missing symbols/fields asynchronously",
                soft_deadline_ms=current.soft_deadline_ms,
            )
        return {
            "status": current.state,
            "fact_status": FACT_ONLY,
            "anchor": current,
            "prior_deltas": prior_deltas,
            "recovery_required": current.state in {PARTIAL, MISSING},
            "source_layers": current.source_layers,
            "revision": current.revision,
            "content_hash": current.content_hash,
            "recovery_plan": recovery_plan,
        }
