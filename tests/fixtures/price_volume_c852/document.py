import json,hashlib,datetime
from pathlib import Path
p=Path('/home/exedev/validation/acceptance/289f271/c852'); meta=json.loads((p/'capture_metadata.json').read_text()); checks=json.loads((p/'checks.json').read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
files=sorted(x for x in p.iterdir() if x.is_file() and x.name not in {'MANIFEST.json','SHA256SUMS','PROVENANCE.md','spec-lint.txt'})
manifest={'id':'C852','plan':'D8','capture_started_at_utc':meta['started_at_utc'],'files':{x.name:{'sha256':sha(x),'bytes':x.stat().st_size,'lines':sum(1 for _ in x.open())} for x in files},'samples':meta['summary'],'limits':['No 2025 daily kline available for 600519 in authorized TD table.','No internal missing trading date found in scanned TD symbols; 000001 is a substitute, not a gap example.','No snapshot as-of historical trade dates is established.']}
(p/'MANIFEST.json').open('x').write(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
text=['# C852 PROVENANCE','', 'PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。',f"采集开始 UTC: {meta['started_at_utc']}；耗时 {meta['elapsed_seconds']} 秒。",'','## 来源与查询','', 'daily_kline.jsonl: TD 只读，列名和类型见 schema.json，逐股无日期限制 SELECT，原始值为 TD 驱动返回值，时间序列化为字符串。查询原文：']
text += ['- `'+x+'`' for x in meta['queries']]
text += ['', 'factor_snapshots.json: 生产 TD 原始快照，含原始 events_json；factor_days.json: 生产日标记原样读回。baostock_responses.json: 从下表所列原始响应逐股提取（含 fields/rows 或源 JSON 对象），新请求为 query_adjust_factor(code, start_date="1990-01-01", end_date="2026-11-08")，请求结果与时间见 request_log.json。factor_events.json 是上述响应或快照的字段提取，所有因子字符串保留。空列表表示成功响应无事件；null 表示未知。', '', '| 股票 | 行数 | 日期范围 | 停牌行 | 事件数 | 事件来源 | 选股理由 |','|---|---:|---|---:|---:|---|---|']
reasons=['除权及长历史','2026-11-06 除权','创业板','科创板补数','新上市','停牌','退市','替补：内部缺口未找到']
for (s,v),reason in zip(meta['summary'].items(),reasons):text.append(f"| {s} | {v['rows']} | {v['start']}..{v['end']} | {v['suspended']} | {v['event_count']} | {v['factor_source']} | {reason} |")
text+=['','calendar 来源 `/home/exedev/core-data/trade_calendar/calendar.json`，sha256 `'+sha(Path('/home/exedev/core-data/trade_calendar/calendar.json'))+'`。全表 `SELECT ts,symbol,suspended FROM core_data.daily_kline_v1`，按该日历扫描每股首末日期内部缺行：0 股。缺行本身的停牌属性无法从缺失行确定。','', '## 独立参考结果','', 'expected.jsonl 为本卡 export.py 的独立 Python 参考计算输出，不是上游数据；每行与日K一一对应。按 §3 无中间舍入；按事件日期选择最近不晚于交易日的因子，停牌、缺因子与日历内缺失交易日中断状态。日历可用范围之外的连续性无法验证；数据及因子快照并未证明历史 as-of 可见性。使用前请由 builder 核实来源合规及采集器要求。','', '核对：']
for x in checks:text.append(f"- {x['symbol']} {x['date']}: {x['raw_close']} × {x['factor']} = {x['product']}")
text+=['- 600519 2025-06-25 / 2025-06-26：UNKNOWN；授权 TD 表仅从 2026-05-12 起有该股行，无法取得这两日原价。已有原始因子分别 0.942063、0.960527。','', '## 文件散列与行数','', 'MANIFEST.json 给出每个数据/脚本文件的字节数、行数与 SHA256；SHA256SUMS 包含本包文件散列（自身除外）。', '']
(p/'PROVENANCE.md').open('x').write('\n'.join(text))
