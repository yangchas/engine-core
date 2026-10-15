"""Vectorized stock-to-plate aggregation using a dense NumPy incidence matrix.

The incidence matrix is kept in float64 so every dot product accumulates in
float64. This is small at the supported market dimensions and avoids a SciPy
dependency; a benchmark compares it with the edge-list reduction alternative.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np


class PlateMatrix:
    """Aggregate stock features over a deduplicated plate membership graph.

    ``edges`` contains ``(plate_code, stock_code)`` pairs. Unknown plate edges
    are ignored; edges for stocks absent from ``stock_codes`` are counted in
    ``unknown_stock_edges``. By default feature 1 supplies weights (commonly
    amount); pass ``weight_index`` to ``aggregate`` to select another feature.
    """

    def __init__(
        self,
        edges: Iterable[tuple[str, str]],
        plate_codes: Sequence[str],
        stock_codes: Sequence[str],
    ) -> None:
        self.plate_codes = tuple(str(code) for code in plate_codes)
        self.stock_codes = tuple(str(code) for code in stock_codes)
        if len(set(self.plate_codes)) != len(self.plate_codes):
            raise ValueError("plate_codes must be unique")
        if len(set(self.stock_codes)) != len(self.stock_codes):
            raise ValueError("stock_codes must be unique")
        self._plate_index = {code: index for index, code in enumerate(self.plate_codes)}
        self._stock_index = {code: index for index, code in enumerate(self.stock_codes)}
        accepted: set[tuple[int, int]] = set()
        unknown = 0
        for raw_plate, raw_stock in edges:
            plate, stock = str(raw_plate), str(raw_stock)
            if plate not in self._plate_index:
                continue
            stock_index = self._stock_index.get(stock)
            if stock_index is None:
                unknown += 1
                continue
            accepted.add((self._plate_index[plate], stock_index))
        ordered = sorted(accepted)
        self.n_plates = len(self.plate_codes)
        self.n_stocks = len(self.stock_codes)
        self.n_edges = len(ordered)
        self.unknown_stock_edges = unknown
        self._membership = np.zeros((self.n_plates, self.n_stocks), dtype=np.float64)
        if ordered:
            plate_idx, stock_idx = np.asarray(ordered, dtype=np.int32).T
            self._membership[plate_idx, stock_idx] = 1.0
        self.member_counts = self._membership.sum(axis=1, dtype=np.float64).astype(np.int64)

    def aggregate(
        self,
        X: np.ndarray,
        mask: np.ndarray | None = None,
        *,
        weight_index: int = 1,
    ) -> dict[str, np.ndarray]:
        """Return per-plate sum, count, mean, weighted mean and market share.

        X is ``(stocks, features)`` or ``(stocks, features, slices)``. Results
        always have shape ``(plates, features, slices)``. NaN values are
        excluded feature by feature; a supplied price/validity mask is applied
        to every feature. Empty denominators produce NaN, while counts remain
        zero. ``weighted_mean`` uses the selected feature as a weight.
        """
        values = np.asarray(X)
        if values.ndim == 2:
            values = values[:, :, None]
        if values.ndim != 3 or values.shape[0] != self.n_stocks:
            raise ValueError("X must have shape (n_stocks, n_features[, n_slices])")
        stocks, features, slices = values.shape
        if not 0 <= weight_index < features:
            raise ValueError("weight_index is outside the feature axis")
        x = np.asarray(values, dtype=np.float64)
        if mask is None:
            valid_mask = np.ones((stocks, slices), dtype=bool)
        else:
            valid_mask = np.asarray(mask, dtype=bool)
            if valid_mask.ndim == 1 and valid_mask.shape == (stocks,):
                valid_mask = valid_mask[:, None]
            if valid_mask.ndim != 2 or valid_mask.shape[0] != stocks:
                raise ValueError("mask must have shape (n_stocks[, n_slices])")
            if valid_mask.shape[1] == 1 and slices != 1:
                valid_mask = np.broadcast_to(valid_mask, (stocks, slices))
            elif valid_mask.shape[1] != slices:
                raise ValueError("mask slice dimension must be 1 or match X")
        valid = np.isfinite(x) & valid_mask[:, None, :]
        clean = np.where(valid, x, 0.0)
        flat = clean.reshape(stocks, features * slices)
        summed = (self._membership @ flat).reshape(self.n_plates, features, slices)
        count = (
            self._membership @ valid.astype(np.float64).reshape(stocks, features * slices)
        ).reshape(self.n_plates, features, slices)
        summed[count == 0] = np.nan
        mean = summed / count

        weights = x[:, weight_index, :]
        weight_valid = valid[:, weight_index, :]
        weight_values = np.where(weight_valid, weights, 0.0)
        weighted_weights = np.where(valid, weight_values[:, None, :], 0.0)
        weighted_denominator = (self._membership @ weighted_weights.reshape(stocks, features * slices))
        weighted_denominator = weighted_denominator.reshape(self.n_plates, features, slices)
        weighted_numerator = self._membership @ (
            clean * weight_values[:, None, :]
        ).reshape(stocks, features * slices)
        weighted_numerator = weighted_numerator.reshape(self.n_plates, features, slices)
        weighted_mean = np.full_like(summed, np.nan)
        np.divide(
            weighted_numerator,
            weighted_denominator,
            out=weighted_mean,
            where=(weighted_denominator != 0) & np.isfinite(weighted_denominator),
        )

        market_sum = np.where(valid, x, 0.0).sum(axis=0, dtype=np.float64)
        share = np.full_like(summed, np.nan)
        np.divide(summed, market_sum[None, :, :], out=share, where=market_sum[None, :, :] != 0)
        return {
            "sum": summed,
            "count": count.astype(np.int64),
            "mean": mean,
            "weighted_mean": weighted_mean,
            "share": share,
        }

    def top_k(self, values: Sequence[float] | np.ndarray, k: int, descending: bool = True) -> list[str]:
        """Return up to k plate codes, with ties broken by plate code."""
        scores = np.asarray(values)
        if scores.ndim != 1 or scores.size != self.n_plates:
            raise ValueError("values must have one score per plate")
        if k < 0:
            raise ValueError("k must be non-negative")
        order = sorted(
            range(self.n_plates),
            key=lambda i: (
                np.isnan(scores[i]),
                -float(scores[i]) if descending and not np.isnan(scores[i]) else
                float(scores[i]) if not np.isnan(scores[i]) else 0.0,
                self.plate_codes[i],
            ),
        )
        return [self.plate_codes[i] for i in order[:k]]
