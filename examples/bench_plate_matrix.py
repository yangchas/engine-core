#!/usr/bin/env python3
"""Synthetic and retained-relation benchmark for PlateMatrix."""

from __future__ import annotations

import csv
import resource
import time
from pathlib import Path

import numpy as np

from engine_core.plate_matrix import PlateMatrix


N_STOCKS = 5200
N_PLATES = 677
N_EDGES = 300_000
N_FEATURES = 8
N_SLICES = 37
RELATION = Path("/home/exedev/validation/plate-registry-pit-20261013/relation.csv")


def elapsed(callable_):
    start = time.perf_counter()
    result = callable_()
    return result, time.perf_counter() - start


def peak_rss_mb() -> float:
    # Linux reports ru_maxrss in KiB.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def sparse_reduce(matrix: PlateMatrix, x: np.ndarray) -> np.ndarray:
    """Edge-list reference using weighted bincount reductions."""
    edge_plate, edge_stock = np.nonzero(matrix._membership)
    result = np.zeros((matrix.n_plates, x.shape[1], x.shape[2]), dtype=np.float64)
    for feature in range(x.shape[1]):
        for slice_index in range(x.shape[2]):
            valid = np.isfinite(x[edge_stock, feature, slice_index])
            result[:, feature, slice_index] = np.bincount(
                edge_plate[valid], weights=x[edge_stock[valid], feature, slice_index],
                minlength=matrix.n_plates,
            )
    return result


def naive_one_slice(matrix: PlateMatrix, x: np.ndarray) -> np.ndarray:
    result = np.zeros((matrix.n_plates, x.shape[1]), dtype=np.float64)
    for plate_index in range(matrix.n_plates):
        members = np.flatnonzero(matrix._membership[plate_index])
        for feature in range(x.shape[1]):
            values = x[members, feature, 0]
            values = values[np.isfinite(values)]
            result[plate_index, feature] = values.sum(dtype=np.float64)
    return result


def main() -> None:
    rng = np.random.default_rng(159)
    plates = [f"P{i:04d}" for i in range(N_PLATES)]
    stocks = [f"S{i:05d}" for i in range(N_STOCKS)]
    pairs = rng.choice(N_PLATES * N_STOCKS, size=N_EDGES, replace=False)
    edges = [(plates[int(pair // N_STOCKS)], stocks[int(pair % N_STOCKS)]) for pair in pairs]
    matrix, build_seconds = elapsed(lambda: PlateMatrix(edges, plates, stocks))
    x32 = rng.normal(size=(N_STOCKS, N_FEATURES, N_SLICES)).astype(np.float32)

    result, dense_seconds = elapsed(lambda: matrix.aggregate(x32))
    sparse_sum, sparse_seconds = elapsed(lambda: sparse_reduce(matrix, x32))
    max_rel = np.max(np.abs(sparse_sum - result["sum"]) / np.maximum(1.0, np.abs(result["sum"])))

    one_slice, naive_seconds = elapsed(lambda: naive_one_slice(matrix, x32[:, :, :1]))
    naive_full_estimate = naive_seconds * N_SLICES
    assert np.allclose(one_slice, result["sum"][:, :, 0], rtol=2e-6, atol=2e-5)

    x64 = x32.astype(np.float64)
    ref = matrix.aggregate(x64)["sum"]
    float32_membership = matrix._membership.astype(np.float32)
    float32_reduced = (float32_membership @ x32.reshape(N_STOCKS, -1)).reshape(
        N_PLATES, N_FEATURES, N_SLICES
    )
    float32_rel = np.max(np.abs(float32_reduced - ref) / np.maximum(1.0, np.abs(ref)))
    print(f"synthetic_shape stocks={N_STOCKS} plates={N_PLATES} edges={matrix.n_edges} slices={N_SLICES} features={N_FEATURES}")
    print(f"construct_seconds={build_seconds:.6f}")
    print(f"aggregate_all_seconds={dense_seconds:.6f}")
    print(f"sparse_bincount_all_seconds={sparse_seconds:.6f} sparse_vs_dense={sparse_seconds / dense_seconds:.2f}x max_rel_error={max_rel:.3g}")
    print(f"naive_one_slice_seconds={naive_seconds:.6f} naive_37_slice_estimate={naive_full_estimate:.6f} dense_speedup={naive_full_estimate / dense_seconds:.2f}x")
    print(f"float32_vs_float64_max_relative_error={float32_rel:.3g}")
    print(f"peak_rss_mb={peak_rss_mb():.1f}")

    if RELATION.exists():
        with RELATION.open(newline="", encoding="utf-8-sig") as handle:
            rows = csv.DictReader(handle)
            relation_edges = [(row["plateid"], row["stockid"]) for row in rows]
        real_plates = sorted({plate for plate, _ in relation_edges})
        real_stocks = sorted({stock for _, stock in relation_edges})
        real_matrix, real_seconds = elapsed(
            lambda: PlateMatrix(relation_edges, real_plates, real_stocks)
        )
        print(f"real_relation_plates={real_matrix.n_plates} stocks={real_matrix.n_stocks} edges={real_matrix.n_edges} construct_seconds={real_seconds:.6f}")
    else:
        print(f"real_relation_missing={RELATION}")


if __name__ == "__main__":
    main()
