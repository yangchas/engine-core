# 测试规范(真实数据优先)— C507,2026-10-23

**原则:解析/计算的测试输入必须是真实上游数据,不得手写"像样的假数据"后宣称通过。** 沙箱里手写数据通过、上线遇真实数据崩,是本项目反复出现的事故(见 fw/KB/04-lessons)。

## 三类测试
| 类别 | 输入 | 何时跑 | 用途 |
|---|---|---|---|
| 离线回放测试(默认) | `tests/fixtures/real/` 里**上游原始响应**(未加工) | 每次 pytest | 解析/规整/计算的回归 |
| 在线契约测试 `@pytest.mark.live` | 当场调用真实上游(只读) | `pytest --live`;real_gate 周期跑 | 发现上游形态漂移、接口不可用;上游不可达标 `UPSTREAM_UNREACHABLE` 并跳过,不冒充代码错 |
| 真实数据验收(real_gate/对账) | 生产 `~/core-data` 与真实 TD/Redis | 每个阶段步骤的验收 | 端到端与老结果逐项对比 |

## 夹具规则
1. **只能由 `examples/capture_real_fixture.py` 抓取**(或复制已提交的 Core 版本 manifest/行),文件放 `tests/fixtures/real/`,同时写入 `PROVENANCE.json`:来源、接口/调用、参数、抓取时刻、交易日、字节数、sha256、命令。
2. 存**原始响应**(上游返回什么就存什么),解析逻辑在被测代码里;不得存加工后的行。
3. `tests/test_real_fixtures.py` 自动校验:文件与 PROVENANCE 一一对应、sha256 一致、含来源/命令、不含凭据类字样(token/password/secret/cookie/authorization)。
4. 负样本也要真实:例如"当日涨停池次日才有"的 `errcode 1020` 响应已抓成夹具 `kaipan_pool_2026-10-23_L1_*.json`。
5. 替身(fake client)只允许**回放真实响应**去驱动真实解析器(如 `ReplayKaipan`),不得返回手写数据。
6. 凭据不入夹具/卡片/报告;抓取工具只保存响应体。

## 新增一个数据源/解析器的步骤
1. 先 `capture_real_fixture.py` 抓真实响应(必要时给工具加一个 kind)。
2. 写离线测试(断言行数与源响应一致、关键字段、排序/编号、错误形态)。
3. 写一个 `live` 契约测试(新响应 schema 与夹具一致)。
4. 在卡片 EVIDENCE 里贴出夹具名与 PROVENANCE 条目。

## 卡片验收口径
- 验收看:全量 pytest(离线)+ `pytest --live`(相关 live 用例)+ real_gate(10-22 或最近有 TD 数据的交易日)+ 对账脚本。
- 禁止:用手写行造"通过";用沙箱假时钟代替真实触发后宣称上线可用;只写临时根的结果当作落库验证。
- 真实性冲突时以真实数据为准,先修解析/假设,不改夹具迁就代码。

## 抓取命令示例
    python examples/capture_real_fixture.py ths_hot_rank
    python examples/capture_real_fixture.py kaipan_hot_plates --date 2026-10-23
    python examples/capture_real_fixture.py kaipan_pool --date 2026-10-22 --level 1
## 已观测的真实事实(来自本次抓取)
- THS 热榜接口不稳:2026-10-23 14:40 三次请求两次 SSL 握手超时 25s(provider 超时上限 10s,易失败)。
- 开盘啦 `getHisBans` 对"当日"返回 errcode 1020(历史接口当日不可用,次日起可用)。
