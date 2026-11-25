# Redis view fixture provenance

- Captured: 2026-11-10T15:04:59.976400+08:00
- Redis endpoint: 127.0.0.1:6380, database /0
- Captured by: builder
- Method: read-only TYPE, TTL, HLEN and HGETALL; select first 501 fields by bytewise lexicographic order. JSON maps each field to its unmodified Redis string value.

- `cache:stock_extra:2026-11-09`: TYPE hash; TTL -1; HLEN / original field count 5201; captured 501; file `redis_view_stock_extra_2026-11-09.json`; bytes 423809; SHA-256 `d11cea22b2bb47fb64cd15980d9b3b3538cde208dff1ace716225a35883da41b`.
- `cache:chip_peaks:2026-11-09`: TYPE hash; TTL -1; HLEN / original field count 5214; captured 501; file `redis_view_chip_peaks_2026-11-09.json`; bytes 121529; SHA-256 `0af6e952c82126d6642811c5472aff1f1f00483fc3af0c30ea18ee00ae336f68`.
- `cache:kline_ready:2026-11-09`: TYPE hash; TTL -1; HLEN / original field count 5201; captured 501; file `redis_view_kline_ready_2026-11-09.json`; bytes 91031; SHA-256 `f5bfed9baaf403e3f765ed6708c007e4c5de54d56d2cf46bda994b1af5ba8b37`.
- `cache:dde_ready:2026-11-09`: TYPE hash; TTL -1; HLEN / original field count 5217; captured 501; file `redis_view_dde_ready_2026-11-09.json`; bytes 51886; SHA-256 `f88e6b3d3d3373429175fb022e874b446571bd25d3836b411a33cb8822810860`.

## 性质声明
本目录夹具是从 Redis 派生视图的真实抓取,不是上游原始响应,因此不登记在 tests/fixtures/real/PROVENANCE.json 中。
