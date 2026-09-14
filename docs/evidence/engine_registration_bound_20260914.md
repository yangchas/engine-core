# Engine evaluation registration bound — 2026-09-14

## Scope

本次只修复 `DeterministicEngine` 单个 session 内 evaluation identity ledger
无界增长的问题。没有新增持久化、Checkpoint、Rabbit 接管、watermark、workflow
或 effect；生产 `engine-next`/`t1-v2-live` 未停止、未重启、未修改。

## Contract

```text
默认 evaluation_registration_limit = 65536
同一 session 的 evaluation_id 不驱逐
达到上限 → fail closed
终态 tombstone 仍为有界近期分类
新的 Engine instance = 新 session 生命周期边界
跨重启的 durable identity 仍延期到 persistence/checkpoint 阶段
```

不驱逐注册身份是为了避免终态 tombstone 淘汰后，同一个 evaluation_id 被重新
注册并再次执行策略。容量耗尽时拒绝新注册，优先保证 once-only 语义，不把安全性
换成无限内存。

## Implementation identity

```text
commit: 3d870ee fix(engine): bound session evaluation registration
archive: /home/exedev/validation/engine-core-3d870ee.tar
archive_sha256: d6cc0aad33b88b610c68bdf1f1aa469fe9a8dca812230f7480cfb99a17390ba2
```

## Tests

Local Windows:

```text
375 passed
compileall: PASS
```

Cobra-ion:

```text
Python: 3.12.3
375 passed in 1.86s
compileall: PASS
```

覆盖内容：

```text
默认容量为有限值
自定义容量达到上限时 fail closed
拒绝发生在 pending 状态变更之前
终态 tombstone 淘汰后仍不会重新注册旧 evaluation_id
```

## Remaining boundary

该修复只解决单个 Engine session 的内存上界和 once-only 注册合同。它不证明
进程跨重启后可以恢复同一 evaluation identity；正式替代 `engine-next` 仍需后续
持久化/恢复设计和多交易日验证。当前 Core 继续 shadow-only。
