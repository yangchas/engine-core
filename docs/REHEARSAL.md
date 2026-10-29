# Guardian 真实触发验收

`examples/rehearse_event.py` 从 Git commit 归档 Guardian，以 live mode 在隔离数据目录中提前触发真实 schedule 事件。

```bash
python examples/rehearse_event.py --event limit_pool_d1
python examples/rehearse_event.py --event limit_pool_d1 --refresh-output
python examples/rehearse_event.py --event limit_reasons_d1 --with limit_pool_d1 --minutes 3
python examples/rehearse_event.py --event daily_kline_backfill --timeout-minutes 45
```

`--minutes` 默认 3；`--timeout-minutes` 默认 45；`--commit` 默认 `HEAD`；`--with` 可重复指定依赖事件；`--td-database` 默认读取 `CORE_TD_DATABASE`，否则使用既有库 `core_dev_c590_632551160254`。数据库必须已存在且名称以 `core_dev_` 开头，工具不会建库或连接 `core_data`。`--keep` 为兼容参数；所有运行目录都会保留，不提供删除操作。

默认保留现有 catalog 状态；事件若发现目标分区已满足，会按真实语义跳过。需要验收新的上游写入路径时可加 `--refresh-output`：工具从真实 schedule/profile 解析主事件的数据集与目标日期，只从临时 catalog 隐藏该日期，并修正临时 latest 指针；原分区文件与线上 catalog 均不修改。此开关只影响主事件，不清除 `--with` 依赖事件的数据。

运行前要求根分区至少有 5 GB 可用空间。目录位于 `/tmp/rh*`，state-root 不超过 40 个字符；Core 数据根只复制小于 5 MB 的 JSON 元数据、calendar 元数据和每个数据集的 `latest.json`，复制 catalog 与 manifests，分区文件用符号链接指向原始不可变分区。暂存数据根必须小于 50 MB，整次运行目录最多 200 MB。catalog、latest 与 manifest 都是独立副本，因此临时写入不会改写线上索引。

Guardian 由指定 commit 的 `git archive` 构建，配置从该归档中的 `schedule.json` 经 `guardiand migrate --expand` 和 `make_config.py` 生成。工具只保留目标事件和显式 `--with` 事件，改写触发时间并延后 deadline、清除 phases，保留 gate、依赖、handler 和 tier 语义。进程使用独立 state-root；Redis 经过本地只读代理；告警 handler 固定 `--dry-run`，并关闭 self-email。任务数据写入暂存 Core 根；TD 版本表差异读取指定的既有 `core_dev_*` 库，事件可按其真实 handler 语义向该临时库写入。

事件终态、相关账本行、catalog 分区差异、TD 版本表差异、安全检查、stderr 尾部和目录占用写入 `/tmp/rh*/result.json`、`result.md`。结束时会停止 Guardian 及其子进程，并检查线上当日账本仍为 append-only、没有临时实例 ID，以及线上 catalog 未被本次临时实例修改。若 hash 变化，只有新 catalog 版本的 committed_at 与线上 Guardian 成功账本行在 120 秒内对应时才标记为 `EXPLAINED_ONLINE_GUARDIAN`；其它变化为 FAIL，并在 JSON 中列出差异与匹配事件。

依赖链较长的事件必须通过 `--with` 显式传入所需前置事件；缺依赖时工具会拒绝启动。原 profile 的 tier timeout 和退避保持不变，实际耗时可能超过触发提前量。当前没有 fake-clock 路径；测试只覆盖脚本纯函数并用仓库真实 `schedule.json`，live 验收需直接运行工具。
