# engine_core 服务器 Codex 开发交接文档

更新时间：2026-09-20（Asia/Shanghai）

## 1. 项目定位

engine_core 是独立、可测试、可回放的计算内核，目标是逐步替代 engine-next 的计算路径，但当前仍处于只读 Shadow/迁移阶段。

硬边界：

- 不接管 Rabbit 消费，不改变 ACK。
- 不写生产 Redis/TD。
- 不发送邮件、通知、下单或其他 effect。
- 不把 Core 代码复制进生产服务目录。
- 未经明确批准不重启生产服务。

## 2. 三方仓库位置

本地 Windows 工作区：

`D:\work\Go\engine_core`

服务器独立 Git 仓库：

`/home/exedev/repos/engine_core`

GitHub：

`https://github.com/yangchas/engine-core.git`

当前开发分支：

`codex/feature-session-engine-integration`

三方校验：

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/main refs/heads/codex/feature-session-engine-integration
ssh cobra-ion 'git -C /home/exedev/repos/engine_core rev-parse HEAD'
```

服务器 Core 仓库 origin 已指向 GitHub。

## 3. 生产目录与服务

生产仓库/服务：

```text
/home/exedev/repos/stock-situation-runtime
/home/exedev/services/engine-next
/home/exedev/services/t1-v2
```

systemd 服务：

```text
engine-next
t1-v2-live
```

只读检查：

```bash
systemctl is-active engine-next t1-v2-live
df -h / /home/exedev
```

禁止：

```text
新增 Rabbit consumer
改变 ACK
写 Redis 或 TD
重启 engine-next/t1-v2-live
删除、prune 或改变 TD retention
触发邮件、通知、下单或其他 effect
```

## 4. 服务器测试

服务器 Python：

```text
/home/exedev/services/engine-next/shared/venv/bin/python
```

执行：

```bash
cd /home/exedev/repos/engine_core
/home/exedev/services/engine-next/shared/venv/bin/python -m pytest -q -p no:cacheprovider
/home/exedev/services/engine-next/shared/venv/bin/python -m compileall -q src tests examples
git status --short
```

本地执行：

```powershell
python -m pytest -q -p no:cacheprovider
python -m compileall -q src tests examples
git diff --check
```

最近一次代码验证：610 passed + compileall。该结果代表代码/fixture 验证，不代表生产链已经通过。

## 5. 验证快照

`/home/exedev/validation/engine_core-*` 是历史验证快照，不是当前开发仓库。

重点旧快照：

```text
/home/exedev/validation/engine_core-05e8901
```

当前开发和运行应使用：

```text
/home/exedev/repos/engine_core
```

不要删除历史验证证据，也不要把旧快照当作最新代码。

## 6. 最近修复

- Redis 竞价 projection 和 Q2 在各自读取完成后记录 observed_at。
- 所有源读取完成后才记录 evaluation 时间。
- 09:20 前的 NORMAL 调用在首次 Redis 读取前阻断。
- timer 未到点时不占用 write-once 输出文件。
- TD 健康门禁要求清理后基线、当日受控窗口 progress，以及 last_ts_ms、batches、td_sql 实际前进。

相关文件：

```text
examples/run_m3_0920_shadow.py
docs/runbooks/m3_0920_next_session_20260921.md
```

## 7. 当前生产阻塞项

最近只读检查：

```text
engine-next：active
t1-v2-live：active
根分区：约 95% 使用率，剩余约 1.1GB
t1-v2 曾出现 stage=commit.tdengine / No enough disk space
```

没有真实清理完成时间和后续交易日健康 progress 前，不得把 M3-1 标为 PASS。

运行单要求：

```bash
export CLEANUP_BASELINE='实际清理完成时间'
```

没有当日受控窗口的新 progress，或 TD 提交计数未前进：

```text
TD_WRITE_HEALTH=UNPROVEN
M3_1_NORMAL=BLOCKED
```

## 8. 下一步路线

```text
1. 确认本地、GitHub、服务器 SHA 相同且工作树干净。
2. 下一个交易日完成真实 TD/磁盘健康门禁。
3. 仅在 09:20–09:21 执行 M3-1 NORMAL 只读旁路。
4. 记录 Q2、auction projection、timer、engine trace。
5. M3-1 通过后再进入 09:24/09:25。
6. 再做 Gate B 和第一条 Auction Shadow 规则。
```

继续延期：

```text
Rabbit direct ingestion
REPLAY_RECORDED
watermark/late correction
Checkpoint
通用 fallback engine
Workflow/DAG
Outbox/fencing/effect
```

## 9. 服务器开发工作流

```bash
cd /home/exedev/repos/engine_core
git fetch origin
git checkout codex/feature-session-engine-integration
git pull --ff-only origin codex/feature-session-engine-integration
```

修改后必须先测试，再提交，再推送 GitHub，最后让服务器仓库 fast-forward 到同一 SHA。

推荐提交格式：

```text
fix(q2): ...
test(m3): ...
docs(runbook): ...
```

任何不确定的架构、生产操作或数据语义，先保持 UNKNOWN/UNPROVEN，不要猜测，不要在盘中修改 producer。
