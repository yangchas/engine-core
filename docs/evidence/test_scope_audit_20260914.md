# engine_core 测试范围与真实数据边界审计

## 当前可复核数字

- 测试模块：46 个 `tests/test_*.py` 文件。
- 静态 `def test_*`：319 个；pytest 参数化后收集用例：393 个。
- 本地完整执行：`393 passed`。
- 远端 Cobra-ion Python 3.12.3 同一提交归档：`393 passed`。
- `src`/`tests` `compileall`：本地与 Cobra-ion 均 PASS。

## 结论

当前测试套件主要证明 Core 轮子的确定性、边界和错误处理；不能把 393 个通过用例解释成 393 次真实数据连接。

静态盘点中 26 个测试模块引用真实源相关名称，10 个模块包含 `FakeRedis`/mock 形式的隔离客户端或替身。真实 Redis、TDengine、Baostock、Kaipanla、问财、同花顺连接验证由 `examples/run_*` 只读命令在 Cobra-ion 上单独执行，并以 `docs/evidence/*` 保存产物；这些命令不是默认 pytest 收集项，避免测试套件隐式触发生产 I/O。

因此当前证据应分开解释：

```text
Offline unit/property boundary     = 393 passed
Real provider probes               = separate Cobra evidence
In-session production shadow       = pending next trading day
Core replacement acceptance        = not achieved
```

## 已覆盖的主要轮子

- Contract：冻结、canonical 序列化/hash、整数除法、语义不可变性。
- Q2：字段解析、单位、source-time/freshness、coverage/completeness、未来时间拒绝。
- Calendar/session/timer：交易日、上一交易日、时区、半开区间、恢复来源和时间单调性。
- Window/facts：累计、source range、PARTIAL/MISSING、相邻段比较、P/M/RB/RA 与 lineage。
- Data/Engine：DataResult/Bundled identity、DATA_READY ownership、同刻因果顺序、VirtualClock、Q2Frame/TD replay。
- Read-only adapters：Redis Q2、TD auction/daily、旧 loader Guard、外部 reference probe 的输入边界。

## 尚未被 393 项离线测试证明的事项

- 真实交易日 `09:20/09:24/09:25` 的同批归属、最终 AuctionState 与 freeze 顺序。
- Redis/TD writer 是否消费同一上游状态，以及 engine-next 完整 loader/report trace。
- 真实运行时持续 freshness、t1-v2 backlog/capacity 和多交易日稳定性。
- Core 对 engine-next 启动、自检、报告、通知及状态生命周期的完全替代。

## 执行纪律

- pytest 不默认联网或改生产状态；真实数据验证必须显式运行 Cobra-ion 只读命令。
- 真实探针必须复用旧系统已验证连接方式，并经过 side-effect guard。
- 不能用 fixture hash、测试数量或当前 Redis 可读性替代历史可见性、批边界或生产链等价性证据。
- 下一有效证据仍是已安排的交易日 in-session Shadow；在此之前不增加通用框架、不切换生产主链。
