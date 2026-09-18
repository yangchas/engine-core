# AnchorDeltaShadowStrategy 真实 TD 只读验证（2026-09-18）

## 范围

本次验证只运行 `AnchorDeltaShadowStrategy`，读取 Cobra-ion 本机 TDengine 的
`market_data1.auction_snapshot_v2` 三条竞价投影行（symbol `000338`，交易日
`2026-09-18`）。没有新增 Rabbit consumer，没有 ACK 改动，没有 Redis/TD 写入，
没有 recovery、通知或 effect；`engine-next`/`t1-v2-live` 仍是生产 owner。

访问路径复用既有 TD client/query 方式，验证副本为：

```text
/home/exedev/validation/engine-core-m3-followup-20260918
```

## 真实结果

```text
rows = 3
0920: PENDING, missing=(AUCTION_0924, AUCTION_0925)
0924: PENDING, missing=(AUCTION_0925,)
0925: OBSERVED, missing=()
```

最终 `0925` 结果：

```text
content_hash = 06b44d77a6b7ae9196a7ed6085f5caaf2bab590cd65969bb196f6f04673c9a5c
evidence_hash = 75f3558ba23be7dcfbfa8930a0cc6e27d6cc99c4ca6aa98163f6eed197232dcf
```

`content_hash` 是事实语义身份；提交上下文单独在 trace 中以
`submission_hash` 保存。`evidence_hash` 只表示真实 TD 行的 lineage/ref，不被当成
业务事实 hash。

## 验证边界

- 证明真实 TD projection rows 可以进入 Core 的事实型策略切片。
- 证明相邻 0920→0924、0924→0925 的状态推进和缺失锚点语义是确定的。
- 不证明 TD 行拥有 Rabbit arrival/batch 顺序。
- 不证明 TD projection 与 Redis/`AuctionState` 同源。
- 不证明 0925 source-freeze owner 或可替代 `engine-next`。
- 不因为盘后真实 TD 可读而宣称当天正常节点 live acceptance。

