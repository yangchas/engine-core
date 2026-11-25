C1055 的修复已提交，提交号 `034c647`，工作树干净。新增了派发状态生命周期和 `pm metrics`，并修复了 newcard、testlist 和 wait-card 的四个问题。

验收测试结果为 **79 passed, 1 skipped**；跳过的是需 `--live` 的契约测试。三个新增测试在修改前均失败，失败记录和真实归档夹具来源见 [C1055 报告](/home/exedev/fw/reports/C1055.md)。

真实归档上的 metrics 输出包括 `C1047:7`、`C1054:2`，测试夹具也验证了 `C1049:1`。当前真实数据中陈旧 ready 卡为 153 张。

本卡状态为 **PARTIAL**：尚待 runner 取得真实 GATE PASS。报告也记录了卡片计划步骤与 PLAN-MASTER 归属待 PM 判定的问题。