# engine_next 报告与通知边界审计（2026-09-14）

## 目的

本记录只审计旧 `engine_next` 的报告构建与外部通知边界，作为后续 Core 替代路线的输入。它不修改旧项目，不把当前报告路径宣称为 Core 已迁移，也不改变生产通知配置。

## 已观察到的职责

### 报告构建：`runtime/auction_email_report.py`

`build_auction_email_report(...)` 接收已形成的 `PlateAuctionShadowV1`、竞价证据、市场上下文和开盘确认数据，执行以下工作：

- 校验 `format`、交易日和 `data_origin`；
- 从已提供的 payload 中组装市场概览、板块行、涨跌停行、锚点变化和 provenance；
- 通过固定模板生成 HTML，并生成纯文本/Markdown 表达；
- 对事实视图、观察列表、HTML 和文本生成 SHA-256 元数据。

该函数可以被视为“报告表示构建边界”，但目前仍直接读取模板文件，并依赖旧项目 payload 结构。它不是事实计算内核，也不是发送器。报告构建本身未观察到 Redis/TD 写入、Rabbit ACK 或 SMTP/HTTP 发送。

### 文本/HTML 渲染

`_render_html()` 与 `_render_text()` 只负责将已组装的报告映射成展示格式；`_canonical_json()` 仅用于报告元数据哈希。展示字段的缺失值会渲染为 `unavailable`，这属于旧报告的表示规则，不能自动提升为 Core 的业务 readiness 语义。

### 通知与外部副作用：`runtime/notification_service.py`

`RuntimeNotificationService` 是外部副作用所有者，当前职责包括：

- 读取通知配置和环境变量；
- 通过 Redis 读取/记录通知去重摘要；
- 通过 SMTP 发送邮件；
- 通过 HTTP webhook 发送通知；
- 在 09:26 条件满足时加载已生成的竞价报告并发送一次。

`_send_email()` 使用 SMTP/SMTP_SSL，`_send_webhooks()` 使用 HTTP POST；这些都必须继续留在 Core 外部。Replay、Shadow 和只读验收不得调用它们。

### 运行时编排

`app_main.py` 与 runtime controller 负责阶段循环、请求构造、报告/通知调用和其他运行时副作用。它们不是可直接复制到 Core 的事实层。未来替代路线应只提取“输入已冻结事实 → build-only report artifact”的窄边界，SMTP、Webhook、Redis 去重及发送策略仍由外部 owner 控制。

## Core 迁移边界

```text
CurrentMarketState / Facts / FrozenDataBundle
                ↓
        build-only report artifact
                ↓
       外部 notification owner
          ├─ SMTP
          └─ Webhook
```

允许迁移的内容：

- 已验证事实的字段投影；
- 明确的报告输入契约和 provenance 展示；
- 无网络、无 Redis/TD 写入、无策略副作用的文本/HTML 构建；
- 可重复的 semantic/evidence hash。

当前不得迁移或接管的内容：

- SMTP、Webhook、通知去重写入；
- 报告构建过程中的隐式数据补齐、recover/backfill 或联网查询；
- 旧 runtime controller 的阶段循环和状态生命周期；
- 任何正式交易 effect。

## 证据等级

| 结论 | 状态 | 说明 |
|---|---|---|
| 旧报告存在独立 build-only 构建函数 | OBSERVED | 已审计函数签名、输入校验、渲染与哈希路径 |
| 报告构建函数本身不发送邮件 | OBSERVED | SMTP 调用位于 `RuntimeNotificationService` |
| 通知服务拥有 SMTP/Webhook 副作用 | VERIFIED | 代码路径明确调用 SMTP 与 HTTP POST |
| 旧报告字段与 Core Fact 一一等价 | UNKNOWN | 尚未完成完整字段 authority/parity 矩阵 |
| Core 可替代旧报告 owner | UNKNOWN | 仍缺实时节点、报告输入和多日 differential 证据 |

## 下一步约束

1. 不在当前 Foundation/Engine integration 分支引入报告发送器或新的 effect 框架。
2. 第一条 Auction Shadow 只输出 trace/build-only artifact；报告构建必须使用冻结输入。
3. 任何报告字段在进入 Core 前都要有 Fact/Field lineage；旧模板中的展示默认值不能替代 `Missing/Partial/Unavailable`。
4. 只有完成旧报告字段的逐项 authority/parity 审计后，才可判断哪些字段迁移为 Fact、哪些仍由外部 presentation 保留。

