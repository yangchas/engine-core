# engine-next runtime owner audit — 2026-09-18

## Scope

只读检查 `cobra-ion` 上正在运行的 `engine-next@20260903_e272842`，并读取
当天 systemd runtime log。没有重启服务，没有消费 Rabbit，没有写 Redis/TD，
没有触发通知或 effect。

## Runtime identity

```text
service: engine-next
unit: /etc/systemd/system/engine-next.service
working directory: /home/exedev/services/engine-next/current
resolved process cwd: /home/exedev/services/engine-next/releases/20260903_e272842
entrypoint: engine_next.app_main --environment server
timezone: Asia/Shanghai
```

生产服务 `engine-next`、`t1-v2-live` 均为 `active`。

## Observed same-day runtime events

来自 `/home/exedev/services/engine-next/logs/engine-next-systemd.log`：

```text
08:30:35 mapping ready; record_count=5955
08:32:51 runtime prime; phase=premarket; symbols=5247; quotes=0; native=0
09:00:32 runtime prime; phase=premarket; symbols=5248; quotes=5223; native=5223
09:25:15.537 scheduled event execute; name=auction_finalize_0925
09:25:58 runtime prime; phase=auction; symbols=5223; quotes=5223; native=5223
09:26:37.576 scheduled event execute; name=auction_followup_0926
09:32:52.716 shape eval; phase=open_confirm; selected=283; total=5223
09:33:45.748 strategy feed audit; phase=intraday
```

这些记录证明旧 runtime 在 09:25/09:26/09:32 有实际消费与报告编排；它们不证明
09:20/09:24 的上游快照产生、batch 归属或 writer freeze。

## Owner conclusion

| Capability | Observed owner | Core status |
|---|---|---|
| 08:30/09:00 mapping/runtime preflight | `engine-next` startup/runtime | 未迁移 |
| 09:20/09:24 source snapshot/freeze | t1-v2/外部生产链 | Core 只读消费，未接管 |
| 09:25 finalization event | `engine-next` runtime controller | Core 未接管 |
| 09:26 follow-up | `engine-next` runtime controller | Core 仅有受限 Shadow |
| 09:32 open-confirm | `engine-next` reporting/runtime path | Core 仅有受限 Shadow |
| report/notification lifecycle | `engine-next` | Core deny-all |

## Migration implication

Core 目前可以继续做真实只读 Shadow，但不能声称已经替代 `engine-next` 的启动、
竞价源冻结、报告编排或通知链。下一交易日首先验证 Core 的单一 `AUCTION_0920`
正常来源路径；通过后再按 09:24、09:25 顺序补节点，不把 09:25 report event
误认为 source freeze owner。

## Evidence status

```text
ENGINE_NEXT_RUNTIME_LOG = OBSERVED
0920_SOURCE_FREEZE_OWNER = UNKNOWN/EXTERNAL
0924_SOURCE_FREEZE_OWNER = UNKNOWN/EXTERNAL
0925_REPORT_EVENT         = OBSERVED
CORE_REPLACEMENT         = NOT_READY
```
