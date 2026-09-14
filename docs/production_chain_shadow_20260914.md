# 2026-09-14 生产链只读旁路验收

## 结论

本次只读验证没有停止 `engine-next` 或 `t1-v2-live`，没有新增 Rabbit consumer，没有改变 ACK，没有 Redis/TD 写入，也没有发送通知或 effect。

结论为：

```text
SOURCE_INGESTION_ACCEPTANCE        UNKNOWN
AUCTION_STATE_ACCEPTANCE           PASS（0920/0925 有真实投影）
STORAGE_PROJECTION_ACCEPTANCE      PASS（受限于可比字段）
ENGINE_NEXT_CONSUMPTION_ACCEPTANCE UNKNOWN（未捕获只读 loader trace）
ENGINE_CORE_SHADOW_ACCEPTANCE      PASS
JOINT_TRADING_DAY_ACCEPTANCE       WARN
```

`WARN` 的原因不是生产服务失败，而是：

```text
0924 捕获时 Redis key 为空；
Rabbit/runtime batch membership 未观测；
effective_config_sha256 缺失；
```

不得用之后出现的 0924 Redis/TD 值回填此前的空捕获槽位。

## 真实运行状态

在 cobra-ion：

```text
engine-next  active，PID 4022407，NRestarts=0
t1-v2-live   active，PID 2878024，NRestarts=0
```

远端根分区约 76% 使用率，可用约 4.2 GiB；本次验证使用独立目录：

```text
/home/exedev/validation/engine-core-live-20260914
```

## 捕获证据

远端原始目录：

```text
/home/exedev/audit/production_ground_truth/20260914
```

本地只读副本：

```text
tmp/capture-20260914/
```

关键 SHA-256：

```text
auction_0920.json              249857be8414175dff179ba9343e117009590bd35a608f0b09d03f71645b8710
auction_0925.json              eb5a3bb707f099b68402fc5dff21601fea7a51ff2c18b774c239e0a0eda9ce75
auction_anchor.json            73919ac88242709577fd92790968408187313993d3d8da0ac90ce7d16ab050a0
capture_manifest.partial.json  be8afaccca55eef6db694eb062d76d70e369f05f04f079690d6cb521df6ed415
```

Manifest 重新计算结果：

```text
declared formal_ground_truth = true
recomputed ground_truth      = PARTIAL
failed required slot         = auction_0924
runtime identity fields      = STABLE_OBSERVED
config provenance            = PARTIAL
write isolation              = Redis 0 / TD 0 / notification 0 / repair 0
```

这里的 `formal_ground_truth=true` 与失败槽位矛盾，属于 capture 工具元数据缺陷，不能作为本次证据的最终结论。

## 真实 Q2 Core Shadow

使用 `q2_093210.jsonl`：

```text
symbol count                  5220
quote count                   5220
coverage                      1.0
status                        STALE
consistency                   BEST_EFFORT_STALE
stale symbols                 5220
oldest source time            2026-09-14 00:00:00+08:00
newest source time            2026-09-14 09:31:14+08:00
```

这证明：

```text
coverage=1.0 不会自动升级 freshness/completeness；
source_time_range 被保留；
同一捕获输入重复运行 Core，Probe hash 一致；
```

本地 captured-file Core shadow：

```text
projection_hash      42a554a56463dd1daa8f1eb54f506a7fd63c41ca729555129e2ad9b5bca2ebe2
engine_probe_hash    087159abc41a1c92f0b39afdf8c0ba9fa3deb4adb3b848a3318cc7e8c77d012b
repeat_hash_equal    true
```

远端直接 Redis Q2 只读探针也完成，但在约 09:49 观察时全部 5220 个 symbol 超过 10 秒 freshness budget，结果为 `STALE/BEST_EFFORT_STALE`；这是真实数据质量结果，不是测试失败。

## 真实竞价与 TD 证据

捕获的 0920 投影：

```text
total_stocks=4914
valid_stock_count=4914
top_amount_count=200
```

捕获的 0925 投影：

```text
total_stocks=5207
valid_stock_count=5207
top_amount_count=200
anchor present=true
```

0924 在捕获时：

```text
market:auction:20260914:0924 = empty
```

之后远端 Redis/TD 查询发现 0924 数据已经存在，但它只能作为“后续观察”，不能改变本次捕获结论。

TD 只读取得 600519 的 0920/0924/0925 竞价投影；事实 shadow 为：

```text
status          PARTIAL
decision_status FACT_ONLY
state           OBSERVE
price           UNKNOWN（0920/0925 projection price 缺失）
amount          VOLUME_EXPANDING
pressure        PRESSURE_WEAKENING
```

TD `stock_tick_v2` 只读取得 09:24:50–09:30:00 的 11 条选定股票样本。保存了五档可见性、价格/量额存在性和 `source_record_time`，但没有 source sequence，因此同毫秒因果顺序仍为 `UNKNOWN`，没有把 hash 或 TD 返回顺序解释成生产到达顺序。

## 输出产物

由 `examples/run_production_chain_shadow.py` 从真实捕获文件生成：

```text
production_chain_matrix.csv
tick_shape_samples.jsonl
tick_shape_transition.csv
tick_shape_statistics.csv
tick_shape_audit.md
audit_summary.json
```

工具只做：文件读取、规范化、Core 内存计算和证据输出；不连接生产 Redis/TD，不写任何外部存储。

## 最终跨环境验证身份

```text
commit: e106e124573a2e49cde5cd43e722268746a69fc6
Python: 3.12.3 (local / cobra-ion)
pytest: 318 passed (local / cobra-ion)
compileall: PASS (local / cobra-ion)
```

同一真实 capture、同一 TD tick 样本下，以下产物的字节级 SHA-256 在 Windows 与 cobra-ion Linux 完全一致：

```text
audit_summary.json           d914bce9fbdfd9f3d4d68712d96b13ec5f5bfa8f36b6a115774ee4c6791cd727
production_chain_matrix.csv  7fa4fa8c664548eed333fc5aa84fa71c88cf277d2a4fc8c9618f34e928f34c86
tick_shape_samples.jsonl     69bcec164573f6bea8ba3c32cac1b7709784a2baeae2753bee510d7ae28fd4ee
tick_shape_transition.csv    f35d513009d69bab2d3ec2de9526909a1d75120c2c1e6aa74408a7a30093a2f2
tick_shape_statistics.csv    8871b8b85540db9375f3e05cf21aa3182130b29098bf5f8db6c53b0b91782a54
tick_shape_audit.md          21b417b9fcba2943873ed5e1a35d25ab8f54d5f8a30b8cc90338e2bf90409181
```

证据文本统一使用 UTF-8 + LF，避免 Windows/Linux 换行差异伪造 hash 漂移；capture 目录名不参与语义身份。

## 验证命令

本地：

```bash
python -m pytest -q -p no:cacheprovider
python -m compileall -q src tests
python examples/run_production_chain_shadow.py \
  --capture-dir tmp/capture-20260914 \
  --output-dir tmp/production-chain-20260914 \
  --trade-date 2026-09-14 \
  --stale-after-ms 10000
```

cobra-ion 使用同一 commit 的临时验证目录和 Python 3.12.3，完整套件为 `318 passed`；本地与远端审计产物 SHA-256 完全一致。

## 后续边界

本次不进入：

```text
Rabbit direct ingestion
producer 修改
Checkpoint
watermark/late correction
正式策略或正式邮件
```

下一阶段仅做 M0：对照 `engine-next` 启动自检、日期锁定、Q2 readiness、参考数据预取和 09:20/09:24/09:25 节点动作；继续保持 `engine-next` 为生产主系统。
