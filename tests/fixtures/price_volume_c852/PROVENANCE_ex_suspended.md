# C859: 剔除停牌行的参考计算

PLAN: D8（PLAN-MASTER §11.2；GOAL-datalayer G5）。

`expected_ex_suspended.jsonl` 是 C852 独立参考实现在剔除 `suspended: true` 行的序列上的输出，不是上游数据。源文件为同级 `c852/daily_kline.jsonl`、`factor_events.json`、`export.py`、`expected.jsonl`；未连接服务。字段、格式与 C852 expected 一致。`offline_verify.py` 直接提取 C852 export.py 的计算体执行，前复权因子选取不晚于当日的最近事件的 `foreAdjustFactor`；参考算法保留原有缺口重置规则。

由于授权不含原交易日历，使用八股日K日期的并集构造相邻日期；全部未过滤输入的重算结果与 C852 原 expected 847 行逐值一致。剔除的停牌日期也在日期并集之中，因此跨越停牌的相邻保留行按该参考算法仍是缺口并重置。该数据集剔除停牌后 823 行，全部与原 expected 对应日期逐值相同。此文件不能验证停牌期间状态和窗口保留的语义。002623 的停牌持续至样本末尾，无复牌日；301139 复牌后的结果也相同。详见 EVIDENCE.txt。

C852 原始数据和因子快照的来源及其历史可见性限制见同级 `c852/PROVENANCE.md`。此结果不证明历史 as-of 可见性，也不改变原参考实现的缺口处理。
