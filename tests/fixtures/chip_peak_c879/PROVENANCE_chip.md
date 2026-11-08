# C879 筹码峰参考值来源

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。

来源为 C852 的 daily_kline.jsonl 与 factor_events.json，详见 /home/exedev/validation/acceptance/289f271/c852/PROVENANCE.md；C860/C861 来源说明记录停牌与价量簇口径。它们是既有采集快照，非本卡实时请求。原始输入、因子与旧函数散列见本目录 MANIFEST.json。前复权价以 Decimal 原价乘最近 event_date 不晚于该行日期的 foreAdjustFactor，再转 binary64；换手原值百分数。没有事件、价格或换手缺失则结果为 NULL，不默认因子。按股日期升序，去掉 suspended=true 后取截至当日最近最多 120 根已有 K 线；停牌行不输出。跨停牌保留窗口。停牌非 bar 是本卡采用的假设，待设计确认。

规范原文：“请求截至目标日含当日最近 120 个交易日分区，按日期升序每股取最多 120 根”“不是向前无限补齐 120 根”；本卡问题要求“截至 d 的最近 120 根”，本卡按该要求执行，若窗口超过最近 120 个交易日分区则与规范不同。授权数据无完整交易日历，八股日期并集亦不能证明没有所有股票同时缺失的日期，无法完整判定这个差异或缺日及分区质量。因子快照历史 as-of 可见性未证明。参考结果是离线数值对拍，不证明 FORMAL 发布资格或生产输入覆盖；quality 仅表示窗口算法局部条件。

规范原文：“停牌行能否作为有效 K 的源语义 UNKNOWN-C1（runner 取证卡）；未证明时不把停牌零行情当有效 K。” “少于 5 根十列 NULL、warm=false、PARTIAL；5–119 根可算但 PARTIAL、warm=false，120 根且输入完整有效方 warm=true、可 FORMAL”。本卡遵从此 warm 定义，非“5 根即 warm”。非同价零权重、同价、非法/缺失输入分别依照 §3 降为 PARTIAL、warm=false。

expected_chip.jsonl 为 generate.py 的独立纯 Python §3 实现；expected_chip_legacy.jsonl 是直接导入只读旧 RuntimeChipRunner.calculate_chip_peak 的十字段映射，未用规范包装旧函数行为，缺项为 NULL；legacy 的 bars_used 是本卡窗口元信息。两文件各 823 行、8230 字段逐项对比，数值绝对误差阈值 1e-9、NULL 对 NULL，共 8230 一致；differences.jsonl 为不一致明细，本样本为空。此比较不能推及未出现的边界及全部市场。

boundary_cases.jsonl 为受控构造，非上游原始数据：从 600519 最新 20 条真实复权 close 和换手复制后，分别全部 close 改为首条价格、全部换手置零、首条换手置 NULL、首条换手置 -1，另截取前 4 条；输入和规范/旧值均保存在同一行。真实窗口中不足 5 根 32 行，001381 的四行全属此类；没有发现全同价、零总权重、换手 NULL 或换手为零窗口。负换手为额外非法输入覆盖。旧函数对 NULL 换手按零处理，对负换手不拒绝，所以这些构造期望可不同；应以规范为准。

manual_check.py 独立读取原始输入，不导入或调用参考实现及旧函数；manual_output.txt 包含首尾五行、前十箱权重、峰箱、四列计算与断言。脚本和完整对拍/手算日志只在证据根目录，不进入 fixture_pack。fixture_pack 仅含数据、来源、清单、哈希，可用于复制参考资料；这些计算结果和构造输入不是新采集的真实上游原始响应，是否纳入测试夹具须由 builder 按采集器和来源规范审核。
