# C861 第二批价量指标参考值

PLAN: D8（PLAN-MASTER §11.2；GOAL-datalayer G5）。

`expected_batch2.jsonl` 是参考实现的输出，不是上游数据。输入为 C852 `daily_kline.jsonl` 和 `factor_events.json`；`generate.py` 为独立纯 Python 实现，使用 Decimal 对源价格乘最近不晚于当日的前复权因子，再用 float 按 KLINE_CLUSTER §3 计算，不作中间舍入。源百分比用于波动率和涨停计数；量和换手不复权。缺字段、零分母按契约输出 null。

停牌日剔除，同时从该股日历去掉，跨停牌保留状态；日历内非停牌缺口重置。与 C860 一样以八股日K日期并集替代日历，无法发现八股同时缺失的交易日；因子快照历史 as-of 可见性未证明。六只无停牌股票与 C852 expected 的日期集合一致，详见 generate_output.txt。原始输入限制见 C852 和 C860 PROVENANCE。

最小预热实测：七只样本足够的股票均与 §3 一致：rsi6/12/24 分别 7/13/25；atr14 为 14；pct_chg_volatility20 为 20；volume_ma5/10/20 为 5/10/20；volume_ratio 为 6；bias_20 为 20；change_pct_5d 为 6；avg_turnover_5d 与 limit_up_days_5 为 5；vol_ratio 为 20。001381 仅 4 行，全部未预热，无法实测一致性；逐股结果见 generate_output.txt。

`manual_check.py` 不调用参考计算函数，使用 600519 的真实输入分别手算每列首次有值的窗口，并断言文件值；输入、复权价、中间量及结果见 manual_output.txt。数值独立校验覆盖初始种子，未独立手算递推后续值。

待明确：§3 的“缺 C 或前 C 中断”和 ATR“必需价格缺失中断”，未明确缺失后第一根有效行如何重启 ATR 首 TR；本参考将有 H/L/C 且无前 C 的行作为新段首根。§3 声称“滚动窗口必须每根所需字段非 NULL”，change_pct_5d 同行写“任一端点缺失 NULL”；本参考按端点判断。实际样本是否覆盖这些缺失情形不能据此推为已验证。§2 的因子“覆盖、版本未证明”缺少可在离线事件文件中执行的判据，本参考按事件日期取值，不证明时点覆盖。
