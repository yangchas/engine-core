import numpy as np

from engine_core.plate_matrix import PlateMatrix


def _naive(edges, plates, stocks, values, mask=None, weight_index=1):
    x = np.asarray(values, dtype=float)
    if x.ndim == 2:
        x = x[:, :, None]
    if mask is None:
        valid = np.ones((len(stocks), 1, x.shape[2]), dtype=bool)
    else:
        valid = np.asarray(mask, dtype=bool)
        if valid.ndim == 1:
            valid = valid[:, None]
        if valid.shape[1] == 1 and x.shape[2] > 1:
            valid = np.broadcast_to(valid, (len(stocks), x.shape[2]))
        valid = valid[:, None, :]
    valid = valid & np.isfinite(x)
    by_plate = {p: [] for p in plates}
    for p, s in set(edges):
        if p in by_plate and s in stocks:
            by_plate[p].append(stocks.index(s))
    out_sum = np.zeros((len(plates), x.shape[1], x.shape[2]))
    out_sum[:] = np.nan
    out_count = np.zeros_like(out_sum)
    for pi, p in enumerate(plates):
        for t in range(x.shape[2]):
            for f in range(x.shape[1]):
                ids = [i for i in by_plate[p] if valid[i, f, t]]
                out_count[pi, f, t] = len(ids)
                if ids:
                    out_sum[pi, f, t] = np.sum(x[ids, f, t])
    mean = out_sum / out_count
    weights = x[:, weight_index, :]
    weighted = np.full_like(out_sum, np.nan)
    for pi, p in enumerate(plates):
        for t in range(x.shape[2]):
            ids = [i for i in by_plate[p] if valid[i, weight_index, t] and np.isfinite(weights[i, t])]
            den = np.sum(weights[ids, t]) if ids else 0
            if den:
                weighted[pi, :, t] = np.sum(x[ids, :, t] * weights[ids, t, None], axis=0) / den
    total = np.where(valid, x, 0).sum(axis=0)
    share = out_sum / total[None, :, :]
    return out_sum, out_count, mean, weighted, share


def test_aggregate_matches_naive_with_nan_and_zero_denominator():
    plates, stocks = ["p1", "p2", "empty"], ["a", "b", "c"]
    edges = [("p1", "a"), ("p1", "b"), ("p2", "b"), ("p2", "c")]
    x = np.array([[[2, 0], [3, 0]], [[4, 5], [0, 2]], [[np.nan, 1], [2, 4]]], dtype=float)
    mask = np.array([[1, 1], [1, 1], [0, 1]], dtype=bool)
    got = PlateMatrix(edges, plates, stocks).aggregate(x, mask=mask)
    want = _naive(edges, plates, stocks, x, mask)
    for key, expected in zip(("sum", "count", "mean", "weighted_mean", "share"), want):
        np.testing.assert_allclose(got[key], expected, equal_nan=True)


def test_hand_calculated_aggregate():
    matrix = PlateMatrix([("p", "a"), ("p", "b")], ["p"], ["a", "b"])
    got = matrix.aggregate(np.array([[2., 1.], [4., 3.]]))
    np.testing.assert_allclose(got["sum"].squeeze(), [6, 4])
    assert np.all(got["count"] == 2)
    np.testing.assert_allclose(got["mean"].squeeze(), [3, 2])
    np.testing.assert_allclose(got["weighted_mean"].squeeze(), [3.5, 2.5])


def test_valid_member_count_follows_mask():
    matrix = PlateMatrix([("p", "a"), ("p", "b")], ["p"], ["a", "b"])
    result = matrix.aggregate(np.ones((2, 2, 2)), mask=np.array([[1, 0], [0, 1]]))
    np.testing.assert_array_equal(result["count"][:, 0], [[1, 1]])


def test_top_k_is_stable_by_plate_code_for_ties():
    matrix = PlateMatrix([], ["z", "a", "m"], [])
    assert matrix.top_k([2, 2, 1], 2) == ["a", "z"]
    assert matrix.top_k([2, 2, 1], 2, descending=False) == ["m", "a"]


def test_empty_plate_and_duplicate_edges_are_handled():
    matrix = PlateMatrix([("p", "a"), ("p", "a")], ["p", "empty"], ["a"])
    assert matrix.n_edges == 1
    result = matrix.aggregate(np.array([[3., 2.]]))
    assert result["count"][0, 0, 0] == 1
    assert np.isnan(result["mean"][1]).all()


def test_unknown_stock_edges_are_ignored_and_counted():
    matrix = PlateMatrix([("p", "a"), ("p", "missing")], ["p"], ["a"])
    assert matrix.n_edges == 1
    assert matrix.unknown_stock_edges == 1
