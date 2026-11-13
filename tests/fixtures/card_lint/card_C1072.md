STAFF: builder-1
ID: C1072
STATUS: DONE(已合并975e98b;wait-card记录缓存占比;depth1 模式 test_c589 单跑过联跑败待查;11-14)
LANE: A
ROLE: builder
EXECUTOR: codex
MODEL: gpt-6.1-terra
EFFORT: medium
COMMIT: yes
TITLE: pm wait-card 记录缓存占比与未缓存 token(读 rollout)
CONTEXT: fw/context/C1072.md(先读)
SERVES: 用户2026-11-14 防止每次全部历史请求;C1071 实测 usage.tsv=未缓存+输出
PLAN: P0 固定职员制(会话预算)
APPROVAL: 用户2026-11-14 继续推进;审核审批自行决定
SCOPE_WRITE: /home/exedev/repos/engine_core-wt-C1072/examples/pm.py, /home/exedev/repos/engine_core-wt-C1072/tests/test_pm.py, /home/exedev/repos/engine_core-wt-C1072/tests/fixtures/pm_tokens, /home/exedev/fw/reports/C1072.md, /home/exedev/fw/evidence/C1072
REAL_DATA: C1071 证据 fw/evidence/C1071/rollout_stats.txt 里的真实数值(30 次调用 input 1399645 cached 1356544 output 9889);真实 rollout 文件的 token_count 事件摘录
ORACLE: 对该真实 rollout 摘录:SUM_INPUT 1399645、SUM_CACHED 1356544、缓存占比 0.969206、未缓存+输出=52990
ACCEPT_BY: fail-first-real-fixture
TEST_BUDGET: 新增测试 ≤3 个;必须包含危险对照;验收清单: tests/test_pm.py tests/test_real_fixtures.py tests/test_trade_day_context.py tests/test_registry_storage.py tests/test_guardian_contract_static.py tests/test_dataset_registry.py tests/test_baseline_window_declaration.py
STUBS_ALLOWED: 无需新增 kind,不使用采集工具;夹具为真实 rollout 文件的 token_count 行摘录并附 PROVENANCE(约束库 td_stops_0945 告警是关键词误报)
