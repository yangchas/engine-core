# M1 Live Morning Shadow — 2026-09-21

这是下一交易日的完整只读早盘旁路运行单，用于验证 Core 的启动 readiness、参考数据预取、
`AUCTION_0926` 和 `OPENING_0932` 节点。它不替代 `engine-next`，不接管 Rabbit，不写 Redis/TD，
不发送邮件/通知，也不执行 effect。

## 固定边界

- 生产 owner：`engine-next`、`t1-v2-live`
- Core 代码副本：`/home/exedev/validation/engine_core-ecc-9aaf8b4d1c562d31869c2812841fe6216a64a918`
- Core commit：`9aaf8b4d1c562d31869c2812841fe6216a64a918`（归档 SHA-256：
  `e6e048a7c3718bac7ba06c6d34109ea0b95379645affe5dd09cb69a7da53b192`）
- Python：`/home/exedev/services/engine-next/shared/venv/bin/python`
- Legacy release（只读 loader 证据）：`/home/exedev/services/engine-next/releases/20260903_e272842`
- 交易日：`2026-09-21`
- 样本：`000338,600519`
- 输出目录：`/home/exedev/validation/live-morning-shadow-20260921-0915`

## 09:15 前预检

```bash
date -Is
systemctl is-active engine-next t1-v2-live
test -r /home/exedev/validation/cc-m0-calendar-20260911-v3.json
test ! -e /home/exedev/validation/live-morning-shadow-20260921-0915
```

TD 写入健康也必须单独检查；仅 `systemctl active` 不足以放行：

```bash
df -P / /home/exedev
if journalctl -u t1-v2-live --since '2026-09-18 00:00:00' --no-pager \
  | grep -q 'stage=commit.tdengine.*No enough disk space'; then
  echo 'TD_WRITE_HEALTH=BLOCKED'
else
  echo 'TD_WRITE_HEALTH=NO_KNOWN_DISK_ERROR'
fi
echo 'LAST_T1V2_PROGRESS:'
journalctl -u t1-v2-live --since '09:15:00' --no-pager \
  | grep 't1_v2 progress' | tail -1
```

如果仍有 `No enough disk space`，或在受控运行窗口内没有新的 progress/heartbeat，本次不得把 TD 当完整 ground truth；可以继续
Redis-only 只读 Shadow，但结论必须标记 `WARN/BLOCKED`，不能宣称 TD/跨源 parity
通过。不得在运行单中自行删除数据、prune volume 或修改 keep 策略。

目标交易日必须在快照中，且输出目录必须不存在：

```bash
/home/exedev/services/engine-next/shared/venv/bin/python - <<'PY'
import json

path = "/home/exedev/validation/cc-m0-calendar-20260911-v3.json"
payload = json.load(open(path, encoding="utf-8"))
if "2026-09-21" not in payload.get("trading_dates", ()):
    raise SystemExit("target trade date is not in calendar snapshot")
print("calendar_target_present=2026-09-21")
PY
```

若服务不为 `active`、目标交易日缺失、磁盘空间不足、TD 写入健康为 BLOCKED、输出目录已存在，停止并记录
`BLOCKED`，不得使用 recovery/fallback 代替本次正常来源验证。

## 运行命令

必须在 09:15 前启动；观察时间由脚本实际读取，不能手填历史时间：

```bash
cd /home/exedev/validation/engine_core-ecc-9aaf8b4d1c562d31869c2812841fe6216a64a918
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/run_live_morning_shadow.py \
  --trade-date 2026-09-21 \
  --calendar-file /home/exedev/validation/cc-m0-calendar-20260911-v3.json \
  --output-dir /home/exedev/validation/live-morning-shadow-20260921-0915 \
  --symbols 000338,600519 \
  --stale-after-ms 10000 \
  --poll-ms 250 \
  --start-at 09:15:00 \
  --stop-at 09:33:00 \
  --legacy-root /home/exedev/services/engine-next/releases/20260903_e272842 \
  --prefetch-auction-references
```

## 验收要点

必须分别记录：

```text
startup readiness / reference prefetch
AUCTION_0926 observation and fact status
OPENING_0932 observation and Q2 status
source-time range
semantic/evidence/submission hashes
read/write/effect safety counters
```

`OPENING_0932` 的业务锚点仍为 `09:32:00`，但正式评估/采样不得早于
`09:32:10`。这是源行情 settling barrier，不改变通用 Timer；仅在
`09:32:00` 采样不能作为正式 opening 证据。

允许的结论：

```text
PASS       节点按 NORMAL 时点消费且输入质量满足节点合同
WARN       节点可运行但 Q2/reference 为 PARTIAL/STALE/UNAVAILABLE
BLOCKED    服务、日历、依赖或输入边界不满足
```

不得把 `RECOVERY_CATCHUP` 改标为 `NORMAL`，不得因为 coverage=1.0 把 stale 数据升级为
`READY`。TD 行不能证明 Rabbit batch membership；旧 loader 只能作为只读证据，不能调用
`recover_auction_anchor()` 或任何写入/修复路径。

## 运行后

保存 manifest、各节点 JSON、SHA-256、commit、Python/dependency/TZ 信息；当天只提交证据
和文档。即使本次通过，也只关闭 M1 早盘 shadow，不代表 Core 已替代 `engine-next`。
