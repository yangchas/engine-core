# 价量簇细则

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。状态：待实施的设计合同。本文扩展 `docs/INDICATOR_FRAMEWORK.md`（以下简称框架），所有新增字段、DDL、命令及编排均为提案，并非已上线；U2 等前置缺口未解决不得部署。时间为 Asia/Shanghai。事实来源：C784 `/home/exedev/fw/reports/C784.md` 与 `/home/exedev/validation/realdata/indicator_samples_20261107/REPORT.md`；C786 `/home/exedev/fw/reports/C786.md` 与 `/home/exedev/validation/realdata/adjust_factor_20261107/REPORT.md`；C788 `/home/exedev/fw/reports/C788.md` 与 `/home/exedev/validation/realdata/chip_dde_20261107/REPORT.md`；现状来源 `docs/DAILY_INDICATORS.md`、`src/engine_core/data_pipeline/derived_daily.py`、`src/engine_core/daily_history.py`。

## §1 范围与成员

同股同版本日K与因子在一次按日期升序遍历中计算。现有 `daily_indicators` 为 `derived_v1`，从不复权价计算，输出 `ma5/ma10/ma20/bias_20/rsi_6/change_pct_5d/avg_turnover_5d/limit_up_days_5/vol_ratio`；窗口默认最多120交易日，前述浮点结果保留两位（现状来源 daily_history.py、derived_daily.py、DAILY_INDICATORS.md）。目标在兼容列之外增加 `ma60,ema12,ema26,dif,dea,macd,k,d,j,rsi12,rsi24,boll_upper,boll_middle,boll_lower,boll_bandwidth,atr14,pct_chg_volatility20,volume_ratio,volume_ma5,volume_ma10,volume_ma20`（拟新增；框架 §3.1 和本细则）。`rsi_6` 保留兼容，目标标准名 `rsi6`（框架 §3.1）；切换需映射。`vol_ratio` 的旧换手率公式与新成交量 `volume_ratio` 不同，两者并存直至消费方明确切换，不得同名替换。其他现有列继续提供，不能因聚类而丢失。

## §2 输入

只读已发布 L0 `daily_kline` 与 `adjust_factor`（框架 §2.1–2.2、§4）；不直接读 market_data1 或网络。日K字段（C784）：`open/high/low/close/pre_close` 元，`pct_chg` 百分点，`volume_shares` 股 BIGINT，`amount_yuan` 元 DOUBLE，`turnover_pct` 百分比，`suspended` BOOL；`ts` 交易日、`symbol` tag、`version`。因子事件原始 `dividOperateDate/foreAdjustFactor` 字符串（C786），目标表每股一行 `ts/version/event_count/sha256/events_json`，ts 为处理时刻，JSON 保存全部事件及原始因子字符串（框架 §2.9）；TD KEEP 与事件日范围限制促成单行原子替换，U2 对该事实不再成立，部署前 DDL 已由大脑审批。`pct_chg` 使用源百分比而非重新混用复权价；volume 不复权。金额、换手只在对应指标使用，不因无成交补零。

对每股取 as-of 可见完整因子快照；事件按日排序，对日 d 找 `event_date <= d` 最近事件，以 Decimal 解析 `foreAdjustFactor`，前复权 OHLC = 原 OHLC × 该因子，事件日使用新因子（C786；五股实测范围，不推为普遍市场规则）。无此前事件或覆盖、版本未证明则价格相关结果 NULL/PARTIAL，不填 1；跨除权计算前日收盘使用该日自身复权 close，ATR 不使用原始 `pre_close`。C786 的样本不证明新事件前后重发行为。停牌日不产出行，递推状态与窗口保留：停牌日无成交，不是交易 bar，与行情软件一致。上市首日的源语义仍为 UNKNOWN-K1；除权日因子选择已在样本验证，其他事件类型 UNKNOWN-K2。缺失日与已确认非交易日不能等同。

默认请求最近120个交易日分区与窗口起点之前最近事件；不是120根可交易K的保证。120日回填起点由交易日历从目标日含当日向前数第120个交易日，实际日期依目标日和日历确定；个股不足时如实 PARTIAL。递推要从最早合格历史开始或使用血缘匹配的前一检查点，120根不是收敛保证；历史缺失不能伪称全历史稳定。FORMAL 覆盖、时点与白名单按框架 §2、§4，因子是事件快照而非逐日要求有行。

## §3 逐指标细则表

约定：第一批与第二批指标计算已实现（C854/C862），已有列另标现状；数值为 DOUBLE，不做中间四舍五入；目标指标价格采用 §2 前复权，变量 C/H/L 为复权 close/high/low，V 为 volume_shares，T 为 turnover_pct，P 为源 pct_chg。`n` 为连续有效根数；滚动窗口必须每根所需字段非 NULL；除零/非有限值返回 NULL。递推在未知缺口处中断并重新预热，不跨空补零；缺值后首个 H/L/C 均有效的 bar 是新段首根，ATR 首 TR=H-L，RSI 从下一根的 Δ 开始；停牌日不是 bar，跨停牌继续，非停牌缺口重置；有效窗口和递推状态均需同一输入版本。状态名是本合同拟新增；`—` 表示无需递推状态，增量仍须读历史窗口。预热为首次定义所需最少有效根数，不保证收敛精度。现状两位小数、未复权；以下精度与价格口径变化均为差异。

| 名称 | 输出列（来源） | 类型 | 公式及边界 | 默认参数 | 类型[递推型/窗口型] | 共用中间量 | 需保存的状态列 | 最小预热根数及依据 | 缺失处理(NULL) | 与现有 daily_indicators 差异 |
|---|---|---|---|---|---|---|---|---|---|
| MA | ma5/ma10/ma20/ma60（框架§3.1） | DOUBLE | sum(C, N)/N | 5/10/20/60 | 窗口型 | 滚动和 | — | N，窗口长度 | 任一 C 缺失 NULL | 前三列现有；ma60 新增，口径和精度变化 |
| EMA | ema12/ema26（框架§3.1） | DOUBLE | 首个有效 C 初始化 E=C；后续 E=2C/(N+1)+(1-2/(N+1))Eprev | 12/26 | 递推型 | E12/E26 | state_ema12/state_ema26 | 1，首值初始化；warm 不代表收敛 | 缺 C 中断 | 新增 |
| DIF | dif（框架§3.1） | DOUBLE | E12-E26，EMA 均以上述首价初始化 | 12/26 | 递推型 | E12/E26 | state_ema12/state_ema26 | 1，两 EMA 均有首值 | 任一 EMA NULL 则 NULL | 新增 |
| DEA | dea（框架§3.1） | DOUBLE | 首个 DIF 初始化 DEA=DIF；后续 DEA=0.2×DIF+0.8×前 DEA | 9 | 递推型 | DIF | state_dea | 1，首 DIF 初始化 | DIF 缺失中断 | 新增 |
| MACD 柱 | macd（框架§3.1） | DOUBLE | 2×(DIF-DEA)，中国常用双倍柱 | 12/26/9 | 递推型 | DIF/DEA | state_ema12/state_ema26/state_dea | 1，首 DIF/DEA 均可定义 | 任一输入 NULL 则 NULL | 新增 |
| KDJ | k/d/j（框架§3.1） | DOUBLE | RSV=100(C-LL9)/(HH9-LL9)；HH=LL 时 RSV=NULL、中断；首个有效 RSV 的前 K=D=50，K=(2Kprev+RSV)/3，D=(2Dprev+K)/3，J=3K-2D | 9/3/3 | 递推型 | HH9/LL9 | state_k/state_d | 9，完整高低窗口 | 缺 H/L/C 或零价差则 NULL，中断 | 新增 |
| RSI | rsi6/rsi12/rsi24（框架§3.1） | DOUBLE | Δ=C-Cprev，G=max(Δ,0)，L=max(-Δ,0)；首 N 次 Δ 的 G/L 算术均值初始化，之后各自 Wilder=(前值×(N-1)+本次)/N；RSI=100Gavg/(Gavg+Lavg)，两者均0时取0，Lavg=0 且 Gavg>0 取100 | 6/12/24 | 递推型 | Δ/G/L | state_rsi6_gain/state_rsi6_loss、state_rsi12_gain/state_rsi12_loss、state_rsi24_gain/state_rsi24_loss | N+1，需 N 个变化 | 缺 C 或前 C 中断 | rsi_6 现有 Wilder 同初始化及平盘取0；新增长周期、前复权和不舍入 |
| BOLL | boll_upper/boll_middle/boll_lower/boll_bandwidth（框架§3.1） | DOUBLE | M=mean(C,20)，σ=sqrt(sum((C-M)²)/20)（总体标准差）；upper=M+2σ，middle=M，lower=M-2σ，bandwidth=(upper-lower)/M；M=0 时带宽 NULL | 20/2 | 窗口型 | MA20/滚动方差 | — | 20，完整窗口 | C 缺失 NULL | 新增；带宽无量纲 |
| ATR | atr14（框架§3.1） | DOUBLE | TR=max(H-L,abs(H-Cprev),abs(L-Cprev))；首根无 Cprev 时 TR=H-L；首14 TR 算术均值，之后 ATR=(13×ATRprev+TR)/14（Wilder） | 14 | 递推型 | TR/前 C | state_atr14 | 14，含首根 TR | 必需价格缺失中断 | 新增 |
| 涨跌幅波动率 | pct_chg_volatility20（框架§3.1） | DOUBLE | sqrt(sum((P-mean(P,20))²)/20)，总体标准差，单位百分点，非年化 | 20 | 窗口型 | P 滚动和/平方和 | — | 20，有效 P 数 | 任一 P 缺失 NULL | 新增 |
| 均量 | volume_ma5/volume_ma10/volume_ma20（本细则提案） | DOUBLE | sum(V,N)/N，单位股 | 5/10/20 | 窗口型 | V 滚动和 | — | N，窗口长度 | 任一 V 缺失 NULL | 新增 |
| 日量比 | volume_ratio（本细则提案） | DOUBLE | Vtoday/mean(V前5根)，不含今日；前5均量为0则 NULL；收盘日量比，不冒充盘中量比 | 5 | 窗口型 | V 滚动和 | — | 6，今日及之前5根 | 任一 V 缺失 NULL | 新增，与旧 vol_ratio 不同 |
| 乖离率 | bias_20（daily_history.py 现有） | DOUBLE | 100(C-MA20)/MA20，MA20=0 则 NULL | 20 | 窗口型 | MA20 | — | 20，MA20 窗口 | C 或 MA20 NULL | 旧值使用先四舍五入到2位的 MA20；目标不舍入且前复权 |
| 5日涨幅 | change_pct_5d（daily_history.py 现有） | DOUBLE | 100(C/C[-5]-1)，基准0则 NULL | 5 | 窗口型 | C 环形缓冲 | — | 6，两个端点间5根 | 仅检查两个端点，中间根缺失不影响；任一端点缺失 NULL | 前复权且不舍入 |
| 5日平均换手 | avg_turnover_5d（daily_history.py 现有） | DOUBLE | mean(T,5) | 5 | 窗口型 | T 滚动和 | — | 5 | 任一 T 缺失 NULL | 不舍入 |
| 5日涨停计数 | limit_up_days_5（daily_history.py 现有） | SMALLINT | 最近5根 P 大于阈值计数；300/301/688/689 为19.8，8/92 为29.8，其余9.8；该调用无名称，ST 规则未启用 | 5 | 窗口型 | P 窗口 | — | 5 | 任一 P 缺失 NULL | 保留现状启发式，非真实涨停事实 |
| 旧换手比 | vol_ratio（daily_history.py 现有） | DOUBLE | mean(T[-5:])/(mean(T[-20:-5])+0.1)，分母0则 NULL | 5/15 | 窗口型 | T 滚动和 | — | 20 | 任一所需 T 缺失 NULL | 保留旧公式但不舍入，与新 volume_ratio 分列 |

递推接续还须保存上一有效复权收盘 `state_prev_close`（拟新增）、连续计数 `state_count`（拟新增）；RSI、ATR 初始化期间需要种子累计和与计数，未 warm 的检查点不作为可恢复检查点，增量从该段起点重算。KDJ 初始化后状态足够；窗口型通过读取已发布 L0 最近60根（含前5量）重建缓冲，不能只凭状态列。框架示例 `state_rsi6/12/24` 是指标值而不是足够状态，本表的 gain/loss 对才足够；不使用它们冒充完整检查点。

## §4 一次遍历的计算顺序与共用量

先批量按版本读取各股历史一次，排序去重并检查连续性；每个股在单次升序遍历更新固定窗口环形缓冲、每种长度的和及平方和、单调队列高低值，所有消费者复用。以下省略边界处理，均执行 §3 的 NULL/中断规则：

```text
for symbol in symbols:
  state = load_valid_checkpoint_or_empty(symbol)
  for bar in ascending_bars_after_checkpoint(symbol):
    factor = latest_covered_event_at_or_before(bar.date)
    O,H,L,C = adjust_once(bar.ohlc, factor)
    close_windows.update_once(C, lengths=[5,10,20,60])
    volume_windows.update_once(bar.volume_shares, lengths=[5,10,20], retain_previous_5=true)
    turnover_windows.update_once(bar.turnover_pct, lengths=[5,20])
    change_windows.update_once(bar.pct_chg, lengths=[5,20])
    extrema9.update_once(H,L)
    delta, tr = difference_and_true_range_once(C,H,L,state.prev_close)
    ema12,ema26 = update_ema_once(C,state)
    dif,dea,macd = update_macd_once(ema12,ema26,state)
    k,d,j = update_kdj_once(extrema9,C,state)
    rsi6,rsi12,rsi24 = update_wilder_rsi_once(delta,state)
    atr14 = update_wilder_atr_once(tr,state)
    ma5,ma10,ma20,ma60 = means(close_windows)
    boll = bands_once(ma20,close_windows.variance20)
    remaining = derive_window_outputs(close_windows,volume_windows,turnover_windows,change_windows)
    state.prev_close = C
    emit_result_and_checkpoint(bar.date, all_outputs, state, input_refs)
```

窗口长度固定时摊销 O(5514×120)=661680 根更新，O(5514×60) 缓冲（若逐股流式则 O(60)），不含排序、存储和 I/O；C784 每 DOUBLE 列约 5293440 字节，实际内存、耗时和数据库压缩 UNKNOWN-K3。方差需稳定算法，微小负数只允许按明确数值误差阈值归零，阈值 UNKNOWN-K4。

## §5 存储

冷层唯一真相为 TD `core_data`，3650 日；以下 DDL 在框架 §3.1 上补充兼容列和足够递推状态，全部是拟新增合同，需单独 DDL 审批。`ts` 为交易日上海零点，`symbol` tag（框架 §2、§3.1）；`input_refs` 是完整版本范围及快照引用 JSON，不得截断。`quality` 为质量状态，`warm` 表示所有要求指标均已达各自最小预热且依赖完整，不代表收敛。

```sql
CREATE STABLE core_data.price_volume_indicators_v1 (
 ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), input_refs NCHAR(16384),
 available_at VARCHAR(40), quality VARCHAR(16), warm BOOL,
 ma5 DOUBLE, ma10 DOUBLE, ma20 DOUBLE, ma60 DOUBLE, ema12 DOUBLE, ema26 DOUBLE,
 dif DOUBLE, dea DOUBLE, macd DOUBLE, k DOUBLE, d DOUBLE, j DOUBLE,
 rsi6 DOUBLE, rsi12 DOUBLE, rsi24 DOUBLE, boll_upper DOUBLE, boll_middle DOUBLE,
 boll_lower DOUBLE, boll_bandwidth DOUBLE, atr14 DOUBLE, pct_chg_volatility20 DOUBLE,
 volume_ma5 DOUBLE, volume_ma10 DOUBLE, volume_ma20 DOUBLE, volume_ratio DOUBLE,
 bias_20 DOUBLE, rsi_6 DOUBLE, change_pct_5d DOUBLE, avg_turnover_5d DOUBLE,
 limit_up_days_5 SMALLINT, vol_ratio DOUBLE,
 state_ema12 DOUBLE, state_ema26 DOUBLE, state_dea DOUBLE, state_k DOUBLE, state_d DOUBLE,
 state_rsi6_gain DOUBLE, state_rsi6_loss DOUBLE, state_rsi12_gain DOUBLE, state_rsi12_loss DOUBLE,
 state_rsi24_gain DOUBLE, state_rsi24_loss DOUBLE, state_atr14 DOUBLE,
 state_prev_close DOUBLE, state_count INT
) TAGS (symbol VARCHAR(8));
```

单表同 tag 同 ts 不能仅凭 version 列实现多版本隔离；UNKNOWN-U2（TD 负责人）决定物理分表/版本隔离及原子标记，解决前 DDL 不可投入生产。每簇独立日标记应含框架 §2.9 `trade_date/version/business_status/row_count/sha256/available_at/quality/missing_whitelist`，数据和状态校验后最后发布；不把半成品标完整。`version` 标识完整输入引用、算法及参数；`available_at` 为实际发布可见时刻，历史查询不得用当前时刻伪造。

Redis `core:ind:kline:{YYYY-MM-DD}` 是 hash，field=symbol，value 为下列完整目标 JSON 格式；所有值是 `<示例>` 占位，不是观测值；实际实现须输出数字、布尔或 null，而非占位字符串。状态仅存冷层，不放热视图。symbol/date 来自键/field；`rsi_6` 为迁移兼容列。

```json
{"version":"<示例>","algo_version":"<示例>","input_refs":[{"dataset":"daily_kline","start_date":"<示例>","end_date":"<示例>","version":"<示例>"},{"dataset":"adjust_factor","start_date":"<示例>","end_date":"<示例>","version":"<示例>"}],"available_at":"<示例>","quality":"<示例>","warm":"<示例>","ma5":"<示例>","ma10":"<示例>","ma20":"<示例>","ma60":"<示例>","ema12":"<示例>","ema26":"<示例>","dif":"<示例>","dea":"<示例>","macd":"<示例>","k":"<示例>","d":"<示例>","j":"<示例>","rsi6":"<示例>","rsi12":"<示例>","rsi24":"<示例>","boll_upper":"<示例>","boll_middle":"<示例>","boll_lower":"<示例>","boll_bandwidth":"<示例>","atr14":"<示例>","pct_chg_volatility20":"<示例>","volume_ma5":"<示例>","volume_ma10":"<示例>","volume_ma20":"<示例>","volume_ratio":"<示例>","bias_20":"<示例>","rsi_6":"<示例>","change_pct_5d":"<示例>","avg_turnover_5d":"<示例>","limit_up_days_5":"<示例>","vol_ratio":"<示例>"}
```

缓存最近5个交易日，TTL 604800 秒（框架 DECISION-5）。重建：按交易日历选择最近5日，按 as-of 从 TD 读取已完成标记及合格版本，校验行数、摘要、时间和血缘，完整生成 hash 后原子切换并设置 TTL；无合格结果返回缺依赖，不触发上游。旧键与替换原子性 UNKNOWN-K5（实施卡验证）；不能将缺项键当完整。

## §6 增量、失效与回填

日常：检查 D 的两项 FORMAL 依赖和 as-of，核对前一个有效检查点的算法/参数、全部祖先血缘及连续性；读需要的滚动历史一次，处理 D 并写该股一行及同版本状态；逐股汇总校验再发布日标记。无合格检查点从可证明的最早输入顺序重算，不用无血缘状态。未 warm 检查点不可续接（§3）。

| 事件 | 影响范围 | 动作 |
|---|---|---|
| D 日K修订 | 该股 D 起所有递推结果；窗口型包括 D 的各窗口 | 从 D 前有效检查点恢复并至最新，窗口范围单独校验；发布新版本 |
| 新除权事件或因子快照变化 | 该股全部历史递推及受影响窗口 | 旧结果失效，从最早可证明因子覆盖处全历史重算；之前 UNKNOWN 保持 PARTIAL |
| 算法版本升级 | 使用该版本的全历史 | 新算法版本全量回填 |
| 窗口参数变化 | 相关算法的全历史 | 参数写入算法版本或其哈希，新版本全量回填 |
| 热键丢失 | 缓存 | 从 TD 合格版本重建，不拉上游 |

首次回填按 §2 日历起点及最早可证明历史，时间升序；120日仅默认请求，并不保证所有股有120根或递推稳定。单行 `warm=true` 要所有 §3 指标达到各自最小根数且必需值可得；否则 `warm=false, quality=PARTIAL`，可已定义的单列保留值，不能把整行 partial 当作 FORMAL 完整标记。精度容忍度 UNKNOWN-K4。相同输入引用、算法、参数且完整提交时返回 20 跳过；失败或缺引用不能跳过。`--force` 可重算但不得绕过依赖与租约；产物相同可复用已发布版本。算法改变发布新版本、旧版本按 as-of 保留，不覆盖旧可见版本；物理保留与原子发布依赖 U2，未解决前禁止宣称支持替换。

## §7 函数契约与单独运行

建议入口 `kline_cluster.run(date, window=120, pull_upstream=False, force=False, dry_run=False, available_at=None)`（拟新增，框架 §8）。`available_at` 是带时区 as-of 截止；默认值及发布时钟语义 UNKNOWN-K6。声明（拟新增，框架 §4.1；日期通过交易日历解析）：

```json
{"id":"price_volume_indicators","requires":[{"dataset":"daily_kline","window_days":120,"min_quality":"FORMAL"},{"dataset":"adjust_factor","window_days":120,"min_quality":"FORMAL"}],"produces":["price_volume_indicators"]}
```

逐日期检查日K完整标记、覆盖、白名单及可见版本；因子检查整股快照及窗口起点以前事件。默认缺依赖返回 30 和 `NOT_YET_AVAILABLE`，列出缺失交易日/股及原因；不拉源。仅 `--pull-upstream` 可在 `core:lease:fact:{fact}:{date}` 300秒、token 续租/释放之下调用唯一获取方，丢租停止发布，完成后重新读标记；`--dry-run` 不写不拉。获取方和租约规则见框架 §4。

| exit | status / error_code | 含义 |
|---|---|---|
| 0 | ok / 空字符串 | 成功 |
| 10 | degraded / STALE_VERSION | 受控旧版本，非重试；新增代码需登记 |
| 20 | not_applicable / ALREADY_SATISFIED | 同版本已完成 |
| 30 | failed / NOT_YET_AVAILABLE | 缺依赖，可重试；新增代码需登记 |
| 40 | failed / CONTRACT_ERROR | 合同错误，不重试 |
| 64 | failed / CONFIG_ERROR | 配置错误，不重试 |
| 75 | failed / BAOSTOCK_CONNECTION_ERROR | 上游或执行暂时故障，可重试；仅显式拉取可能触及上游 |

回复为框架 contract=1；示例日期只是格式，不宣称缺失：
```json
{"contract":1,"status":"failed","error_code":"NOT_YET_AVAILABLE","retryable":true,"detail":{"missing":[{"dataset":"daily_kline","trade_date":"2026-11-06"},{"dataset":"adjust_factor","trade_date":"2026-11-06","symbol":"600519","reason":"factor_coverage_unknown"}]}}
```

拟议 CLI（实现后才可调用，命令模块名和参数待实施卡接线）：
```sh
python -m engine_core.kline_cluster --date 2026-11-06 --window 120 --available-at 2026-11-06T23:30:00+08:00
python -m engine_core.kline_cluster --date 2026-11-06 --window 120 --pull-upstream
```

## §8 编排

盘后由 `baostock_probe_1730` 完成后依次触发 `adjust_factor_postclose` → `price_volume_postclose` → `chip_peak_postclose`；夜间由 `daily_kline_backfill` 完成后依次触发 `adjust_factor_0115` → `price_volume_0130` → `chip_peak_0145`。各依赖接受成功或降级成功，失败则下游跳过；窗口仅限定最早开始和最晚截止。目标日日K缺失或明显不全时降级且不写日标记或状态。首次部署须手动全市场 rebuild；空状态增量返回 NEEDS_REBUILD。

## §9 验证

实现卡先写 fail-first；解析/计算仅使用 `examples/capture_real_fixture.py` 采集的真实原始响应并登记 `tests/fixtures/real/PROVENANCE.json`，不手写假行冒充真实夹具。先扩展该采集器支持从 TD 采 N 只股票 × 所需历史窗口及对应因子快照，记录查询范围、版本、时点、哈希和脱敏来源；捕获器能力、N 和覆盖 UNKNOWN-K7（runner/实现卡），不得宣称现有脚本已支持 TD。上游调整事件历史在采集时需证明 as-of 版本；C784/C786 已存原始响应可作为研究线索，不自动等同合规测试夹具。

- 真实完整日K加因子：断言 MA/BOLL 共用前复权窗口，旧代码用不复权且无 BOLL，修复前失败。
- 跨真实除权日：断言事件日选当日因子，旧实现忽略因子，修复前失败。
- 真实窗口：逐列对照独立标准算法计算 EMA、MACD、KDJ、RSI、ATR、波动率及成交量比，旧代码缺字段，修复前失败。
- 真实不足预热或缺失输入窗口：断言对应 NULL、warm=false/PARTIAL，旧路径无簇质量合同，修复前失败。
- 真实连续历史在中间断点切分：断言状态接续等于同一初始点整段重算，检查 RSI gain/loss，旧代码无检查点，修复前失败。
- 真实已发布 TD 结果清空隔离测试热键：断言重建值、版本和 TTL 且无上游调用，旧路径无该簇重建，修复前失败；删除只限 core: 测试键。
- 缺 FORMAL 日K或因子：断言 exit 30、缺失日期和默认零上游调用，旧路径无该依赖合同，修复前失败。
- 同输入重试及修订：断言幂等、旧版 as-of 可读、新版结果及标记原子可见，旧路径不具该 TD 多版本合同，修复前失败。

对照可在测试环境用 pandas/ta 等独立标准实现，只作测试期望、不得成为运行时依赖；须显式匹配本合同首值、总体标准差、KDJ 平滑、ATR 首 TR、平盘 RSI 和双倍 MACD，库不支持的变体用真实原始窗口逐步手算校验点。按绝对/相对容忍度比较，容忍度 UNKNOWN-K4；不以旧输出为标准答案。保留每次对照的输入版本、断点、公式及误差。

## §10 迁移

1. 冻结本文口径，解决因子覆盖、版本隔离与 DDL 审批；先采真实 fixture/evidence，部署唯一因子获取方和完整标记。
2. 回填日K与因子，验证时间可见、覆盖和真实除权窗口；建设价量簇、状态、版本发布和热层重建，旧 `daily_indicators` 继续提供。
3. 以最短真实窗口双读；逐列解释前复权、不舍入、RSI 命名和新旧量比差异，不要求数值相等；保留 `bias_20/change_pct_5d/avg_turnover_5d/limit_up_days_5/vol_ratio` 直到消费者迁完。
4. 改 `src/engine_core/slices/__init__.py` 中读取 `daily_indicators` 的日期选择、PIT 校验与 `indicator_fields` 映射；`rsi6` 映射至旧 `rsi_6`，旧 `vol_ratio` 不映射成 `volume_ratio`；不得用旧日期指标冒充最新日。检查报告及 `daily_report_features` 和独立 CSV 路径的消费者。
5. FORMAL sweep、夜间回补、历史 as-of、热层重建及回滚期均验证后，按字段和消费清单删除已迁完的旧构建路径；未迁字段、报告特征及独立 CSV 保留（框架 §10.1/U14）。

## §11 与框架文档的差异/缺口

- 框架 §3.1 的 `state_rsi6/state_rsi12/state_rsi24` 仅单值不能恢复 Wilder；本文用各 gain/loss 两列及前收盘扩展。版本隔离和部分初始化检查点仍依赖 U2；框架需在实施前同步修订。其他扩展列是兼容与均量、量比所需提案，非已存在字段。
- 框架要求缺依赖默认不计算；本文的 PARTIAL 指依赖已满足但个股历史短、因子覆盖或字段不完整时的可定义部分，因子快照未达整体 FORMAL 仍应先返回 30，不能用部分结果绕过门槛。
- 框架 U12 未冻结，本文选择明确算法变体和最小定义根数，并非已证明数值收敛；生产精度容忍度、除权因子覆盖及版本物理隔离仍阻塞实施验收。

## §12 UNKNOWN 与待 runner 查证项

- UNKNOWN-K1：上市首日源行、首日 pre_close 与缺失原因；runner 真实样本卡按日K与交易日历核对。
- UNKNOWN-K2：其他除权事件类型、首事件前因子和新事件前后快照重发；复权 runner/实现卡以事件前后多次采集查证。
- UNKNOWN-K3：5514股×120日的实测耗时、峰值内存和存储；实现后 runner 真实数据性能卡测量。
- UNKNOWN-K4：稳定方差舍入阈值、逐指标绝对/相对容差和递推收敛要求；指标实现/研究卡对照真实数据及高精度参考确定。
- UNKNOWN-K5：Redis 完整 hash 原子替换和 TTL 恢复实现；实现卡在隔离 core 测试键核验。
- UNKNOWN-K6：函数未传 available_at 时的时钟、发布可见时刻和 CLI 模块路径；Guardian/实现卡确定。
- UNKNOWN-K7：真实夹具采集器 TD/因子能力、样本数量与版本可见性；runner 采集卡核查并登记来源。
- UNKNOWN-U1/U2/U3/U12/U13/U14：依次为非日K完整性阈值、TD 多版本隔离、日K唯一获取边界、公式容忍度、最终降级运行时、迁移消费清单；责任及验收见框架附录 B。U12 的算法变体在本文提议，容忍度尚未解决。
