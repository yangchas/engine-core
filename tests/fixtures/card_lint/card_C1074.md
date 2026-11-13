STAFF: builder-1
ID: C1074
STATUS: DONE(已合并2698066;合并时在合并树跑104测试通过;修重复计数/缺基线/pilot口径;11-14)
LANE: A
ROLE: builder
EXECUTOR: codex
MODEL: gpt-6.1-terra
EFFORT: medium
COMMIT: yes
TITLE: 修 C1072 评审三问题:重复计数/缺基线/pilot 口径
CONTEXT: fw/context/C1074.md(先读)
SERVES: 用户2026-11-14 防止全部历史请求;C1073 评审发现
PLAN: P0 固定职员制(会话预算)
APPROVAL: 用户2026-11-14 继续推进;审核审批自行决定
SCOPE_WRITE: /home/exedev/repos/engine_core-wt-C1074/examples/pm.py, /home/exedev/repos/engine_core-wt-C1074/tests/test_pm.py, /home/exedev/repos/engine_core-wt-C1074/tests/fixtures/pm_tokens, /home/exedev/fw/reports/C1074.md, /home/exedev/fw/evidence/C1074
REAL_DATA: C1073 报告与 evidence/C1073/check_rollout.txt;真实 rollout 夹具 tests/fixtures/pm_tokens
ORACLE: 对夹具里一条 token_count 事件重复回放:input/cached 总和不变;缺基线时缓存占比与未缓存显示未知而不是 0;pilot 续用与新会话同用未缓存输入+输出口径
ACCEPT_BY: fail-first-real-fixture
TEST_BUDGET: 新增测试 ≤2 个;真实夹具 fail-first;验收清单: tests/test_pm.py tests/test_real_fixtures.py tests/test_trade_day_context.py tests/test_registry_storage.py tests/test_guardian_contract_static.py tests/test_dataset_registry.py tests/test_baseline_window_declaration.py
STUBS_ALLOWED: 无需新增 kind,不使用采集工具;夹具为真实 rollout 的 token_count 行(已有 tests/fixtures/pm_tokens,带 PROVENANCE);重复事件由测试对真实行复制一条构造(约束库 td_stops_0945 告警是关键词误报)
