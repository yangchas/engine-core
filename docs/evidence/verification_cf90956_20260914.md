# engine_core `cf90956` 跨环境验证证据

## 身份

- Git commit: `cf90956` (`docs(evidence): record t1 runtime lag risk`)
- Local archive: `tmp/engine-core-cf90956.tar`
- Local archive SHA-256: `9eb57034d83e1f52f17bb90ac6879bde2b9576d1f06863cb55c89c85fbaacdd8`
- Cobra archive: `/home/exedev/validation/engine-core-cf90956.tar`
- Cobra archive SHA-256: `9eb57034d83e1f52f17bb90ac6879bde2b9576d1f06863cb55c89c85fbaacdd8`

归档由本地 `git archive HEAD` 生成，远端使用同一归档解压到新的隔离验证目录；未覆盖历史证据、生产代码或服务目录。

## 结果

| Check | Local Windows | Cobra-ion |
|---|---:|---:|
| pytest (`-p no:cacheprovider`) | 393 passed | 393 passed |
| Python | local edit environment | 3.12.3 |
| compileall (`src tests`) | PASS | PASS |
| `git diff --check` | PASS | N/A（归档目录不含 `.git`，本地提交前已 PASS） |

Cobra 执行目录：`/home/exedev/validation/engine-core-cf90956`。

## 边界

- 这次只验证固定提交的代码/文档一致性，不执行生产服务重启。
- 未新增 Rabbit consumer，未改变 ACK，未写 Redis/TD，未发送通知或 effect。
- 远端生产 `engine-next` 与 `t1-v2-live` 保持 active；下一交易日的 in-session shadow 仍按既定隔离命令等待执行。
- 该证据不提高 `SOURCE_INGESTION`、批归属、AuctionState freeze 或 Core replacement 的接受等级。
