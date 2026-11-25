# `native/tickd` — C++17 实时行情与盘口计算引擎

> 原名 `t1_v2`，已于 `v1.0.0` 并入 `engine_core` Monorepo 并拍平为单层目录 `native/tickd/`。

## 1. 核心职责

1. **接入层**：从 RabbitMQ 消费实时压缩 Protobuf（`schema.proto`）五档 tick 批次。
2. **内存计算层**（`engine_core.cpp` / `quote_calculator.cpp` / `auction_calculator.cpp`）：
   - 实时计算涨跌停状态 `ls`（10%/20%/30% 分板块）、大单净额 `ln`、滚动动量（`spd1m`、`amt2m`、`amt5m`、`vec3m`、`vec5m`）；
   - 实时计算一档盘口挂单金额（整数元，防溢出 `int64_t`）：
     - `b1a = (bp_milli[0] * bv[0] * 100) / 1000`
     - `a1a = (ap_milli[0] * av[0] * 100) / 1000`
     - 盘口金额 `b1a`/`a1a` 变化即置位 `DIRTY_QUOTE_CORE`，确保涨停无成交期间撤单或卖一漏单也能立即刷 Redis；
   - 集合竞价 3 个定点截点（`09:20:03` / `09:24:10` / `09:25:06`）冻结全量竞价快照。
3. **双写输出层**：
   - **Redis 热层**（全天实时）：`q2:<symbol>`（26 字段覆盖写）、`core:auction:full:<date>:<0920|0924|0925>`、`a2:<date>:<tag>`、`m2:runtime:<date>`。
   - **TDengine 时序层**：`market_data1.stock_tick_v2`（完整五档逐笔，`09:45:59` 自动截停以释放盘中写库压力）。

## 2. Redis `q2:<symbol>` 关键字段速查

| 字段 | 含义 | 单位 | 说明 |
|---|---|---|---|
| `px` / `pc` | 现价 / 昨收 | 厘 (`milli_yuan`) | 除以 `1000` 为元 |
| `amt` / `vol` | 累计成交额 / 成交量 | 元 / 手 | — |
| `ls` | 涨跌停状态 | 枚举 (`1 / 0 / -1`) | `1`=涨停，`-1`=跌停 |
| `b1a` / `a1a` | **买一 / 卖一挂单金额** | **整数元** | `ls=1 && a1a=0` 为真封死（封单=`b1a`）；`ls=1 && a1a>0` 为开板风险 |
| `br` / `ar` | 竞价未匹配买/卖额 | 整数元 | **仅竞价口径**，绝不可当作盘中封单 |
| `a20`/`a24`/`a25`/`am` | 竞价锚点价与匹配额 | 厘 / 元 | — |
| `spd1m`/`vec3m`/`amt2m`/`ln` | 涨速 / 向量 / 2分钟额 / 大单净额 | bp / 元 | — |

## 3. 编译与自检

依赖：`g++ (C++17)`、`protoc (libprotoc 3.21.12)`、`zlib`、`protobuf`、`taos`、`hiredis`、`librabbitmq`、`openssl`。

```bash
cd native/tickd
CORE_DATA_ROOT=/home/exedev/core-data bash make.sh --self-test --out=build/tickd
```

- 禁止不传 `--out` 覆盖默认 `/tmp/t1_v2_all_live`；
- 构建临时生成的 `schema.pb.cc` / `schema.pb.h` 与 `build/` 已列入 `.gitignore`。

## 4. 线上部署与回滚

- 线上 systemd 服务：`t1-v2-live.service`
- 发布软链：`/home/exedev/services/t1-v2/current` → `/home/exedev/services/t1-v2/releases/<YYYYMMDD_tag_commit>/`
- 回滚：将 `current` 软链切回上一版本目录并执行 `sudo systemctl restart t1-v2-live.service`。
