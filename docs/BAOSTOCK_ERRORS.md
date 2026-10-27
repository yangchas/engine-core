# Baostock error handling

The production logs described by C561 contained `Bad file descriptor`,
`接收数据异常`, and login failures. Two independent A/B implementations also
printed `logout failed!` during real Baostock runs. These are observations from
those runs; they do not establish that every Baostock session fails this way.

| Error observation | Evidence | Handling |
| --- | --- | --- |
| `Bad file descriptor` | Production logs; retry path also covered by a fake client | Treat as a connection/session failure. Boundedly log out, create a fresh client, log in again, and retry the current symbol up to the configured limit. Exhaustion preserves the checkpoint and returns retryable failure (30 for Baostock-only jobs). |
| `接收数据异常` | Production logs; marker matching covered by a fake client | Same bounded reconnect and retry path as other connection failures. |
| Login failure | Production logs; bounded login retry covered by a fake client | Retry login up to `retries + 1` calls. Exhaustion preserves progress and returns retryable failure. |
| `logout failed!` | Printed during both independent real A/B Baostock runs; cleanup behavior also covered by a fake client | Check a returned non-zero logout code and record cleanup failure. Keep rows already fetched; cleanup failure does not invalidate them. |
| Per-call timeout | Fake-client regression test only | Bound each blocking call independently. If a worker is still blocked when its caller times out, do not reuse or log out that session; preserve the checkpoint and return retryable failure. |
| Total capture budget | Fake-client regression test only; C561 real interrupted/resume run provides separate end-to-end evidence | Save completed symbols and successful empty responses at the deadline. Return within budget and resume by skipping symbols in the hash-bound checkpoint. |

The fake-client cases verify bounded behavior and checkpoint semantics. They do
not claim those timeout conditions occurred in the live Baostock runs.

A completed `--symbols-file` capture exits 10 with status `degraded`: the
Baostock sample was fetched successfully, but its sample partition is
provisional and is not a formal daily-kline artifact.
