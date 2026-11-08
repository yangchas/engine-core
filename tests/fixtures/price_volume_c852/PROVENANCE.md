# C852 PROVENANCE

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。
采集开始 UTC: 2026-11-08T11:48:48.053390+00:00；耗时 5.703 秒。

## 来源与查询

daily_kline.jsonl: TD 只读，列名和类型见 schema.json，逐股无日期限制 SELECT，原始值为 TD 驱动返回值，时间序列化为字符串。查询原文：
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '600519' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '600519' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '000815' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '000815' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '300750' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '300750' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '688796' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '688796' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '001381' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '001381' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '002623' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '002623' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '301139' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '301139' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '000001' ORDER BY ts`
- `SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '000001' ORDER BY ts DESC LIMIT 1`
- `SELECT * FROM core_data.adjust_factor_days`

factor_snapshots.json: 生产 TD 原始快照，含原始 events_json；factor_days.json: 生产日标记原样读回。baostock_responses.json: 从下表所列原始响应逐股提取（含 fields/rows 或源 JSON 对象），新请求为 query_adjust_factor(code, start_date="1990-01-01", end_date="2026-11-08")，请求结果与时间见 request_log.json。factor_events.json 是上述响应或快照的字段提取，所有因子字符串保留。空列表表示成功响应无事件；null 表示未知。

| 股票 | 行数 | 日期范围 | 停牌行 | 事件数 | 事件来源 | 选股理由 |
|---|---:|---|---:|---:|---|---|
| 600519 | 122 | 2026-05-12..2026-11-06 | 0 | 31 | /home/exedev/validation/acceptance/3380b2e/c846/baostock_raw.json | 除权及长历史 |
| 000815 | 122 | 2026-05-12..2026-11-06 | 0 | 9 | /home/exedev/validation/acceptance/289f271/c851/baostock_000815.json | 2026-11-06 除权 |
| 300750 | 122 | 2026-05-12..2026-11-06 | 0 | 12 | /home/exedev/validation/acceptance/3380b2e/c846/baostock_raw.json | 创业板 |
| 688796 | 120 | 2026-05-13..2026-11-05 | 0 | 1 | C852 Baostock query_adjust_factor 1990-01-01..2026-11-08 | 科创板补数 |
| 001381 | 4 | 2026-11-03..2026-11-06 | 0 | 1 | /home/exedev/validation/acceptance/3380b2e/c846/baostock_extra_raw.json | 新上市 |
| 002623 | 121 | 2026-05-12..2026-11-05 | 3 | 12 | C852 Baostock query_adjust_factor 1990-01-01..2026-11-08 | 停牌 |
| 301139 | 114 | 2026-05-12..2026-10-27 | 21 | 4 | C852 Baostock query_adjust_factor 1990-01-01..2026-11-08 | 退市 |
| 000001 | 122 | 2026-05-12..2026-11-06 | 0 | 42 | /home/exedev/validation/acceptance/3380b2e/c846/baostock_raw.json | 替补：内部缺口未找到 |

calendar 来源 `/home/exedev/core-data/trade_calendar/calendar.json`，sha256 `c4b55f4fc7b3a4ead53a7f9c62a6be7148d939ae6196df70d44d110cafe5cc1f`。全表 `SELECT ts,symbol,suspended FROM core_data.daily_kline_v1`，按该日历扫描每股首末日期内部缺行：0 股。缺行本身的停牌属性无法从缺失行确定。

## 独立参考结果

expected.jsonl 为本卡 export.py 的独立 Python 参考计算输出，不是上游数据；每行与日K一一对应。按 §3 无中间舍入；按事件日期选择最近不晚于交易日的因子，停牌、缺因子与日历内缺失交易日中断状态。日历可用范围之外的连续性无法验证；数据及因子快照并未证明历史 as-of 可见性。使用前请由 builder 核实来源合规及采集器要求。

核对：
- 000815 2026-11-05: 13.95 × 0.997849 = 13.91999355
- 000815 2026-11-06: 13.49 × 1.000000 = 13.49000000
- 600519 2025-06-25 / 2025-06-26：UNKNOWN；授权 TD 表仅从 2026-05-12 起有该股行，无法取得这两日原价。已有原始因子分别 0.942063、0.960527。

## 文件散列与行数

MANIFEST.json 给出每个数据/脚本文件的字节数、行数与 SHA256；SHA256SUMS 包含本包文件散列（自身及会被规格检查覆盖的 spec-lint.txt 除外）。
