"""Preflight checks for task cards; card contents are never executed."""
import argparse
import json
import re
from pathlib import Path

GLOBAL_INVARIANT_TESTS = (
    'tests/test_real_fixtures.py', 'tests/test_trade_day_context.py',
    'tests/test_registry_storage.py', 'tests/test_guardian_contract_static.py',
    'tests/test_dataset_registry.py', 'tests/test_baseline_window_declaration.py',
)
FIELD = re.compile(r'^([A-Z][A-Z0-9_]*):\s*(.*)$', re.MULTILINE)
PATH = re.compile(r'(?:/home/exedev/|~/)[^\s,，;；。！？、（）() `"\'<>]+')
TRAILING = '.,:;!?，。；：！？、\'"）)]}】》'


def exact_test_list(files, repo):
    """Apply release.py's exact-mode selection to repository-relative paths."""
    repo = Path(repo)
    files = [str(f) for f in files]
    candidates = [f'tests/test_{Path(f).stem}.py' for f in files
                  if f.startswith(('src/', 'examples/')) and f.endswith('.py')]
    candidates += [f for f in files if f.startswith('tests/')
                   and Path(f).name.startswith('test_') and f.endswith('.py')]
    return list(dict.fromkeys(f for f in [*candidates, *GLOBAL_INVARIANT_TESTS]
                              if (repo / f).is_file()))


def lint_card(card, repo):
    repo = Path(repo)
    matches = list(FIELD.finditer(card))
    fields = {m.group(1): card[m.start(2):matches[i + 1].start() if i + 1 < len(matches) else len(card)].strip()
              for i, m in enumerate(matches)}
    findings = []
    def add(level, rule, message):
        findings.append(f'{level} {rule}: {message}')

    ident = re.match(r'C(\d+)', fields.get('ID', ''))
    card_id = ident.group(0) if ident else ''
    role = fields.get('ROLE', '').lower().split()[:1]
    role = role[0] if role else ''
    if ident and int(ident.group(1)) >= 1051 and role in ('builder', 'runner'):
        required = ['REAL_DATA', 'ACCEPT_BY']
        if role == 'builder':
            required += ['ORACLE', 'TEST_BUDGET', 'STUBS_ALLOWED']
        missing = [key for key in required if not fields.get(key)]
        accept = fields.get('ACCEPT_BY', '')
        if accept and not re.match(r'^(real-run|fail-first-real-fixture|function-run|observe)\b', accept):
            missing.append('ACCEPT_BY 取值')
        if accept.startswith('observe') and not (re.search(r'design/29', card, re.I) and re.search(r'\b[ABC]\b', card)):
            missing.append('observe 的 design/29 类别 A/B/C')
        if missing:
            add('FAIL', 'required_fields', '补充或修正 ' + ', '.join(missing))

    without_scope = re.sub(r'^SCOPE_WRITE:.*(?:\n(?![A-Z][A-Z0-9_]*:).*)*', '', card, flags=re.MULTILINE)
    # Dangerous-command counterexamples are intentionally nonexistent targets.
    without_scope = '\n'.join(line for line in without_scope.splitlines()
                              if '危险对照' not in line)
    missing_paths = []
    for match in PATH.finditer(without_scope):
        raw = match.group().rstrip(TRAILING)
        if any(c in raw for c in '*?[]'):
            continue
        path = Path(raw).expanduser()
        own_report = Path('/home/exedev/fw/reports') / f'{card_id}.md'
        own_evidence = Path('/home/exedev/fw/evidence') / card_id
        if card_id and (path == own_report or path == own_evidence or own_evidence in path.parents):
            continue
        if not path.exists() and str(path) not in missing_paths:
            missing_paths.append(str(path))
    if missing_paths:
        add('FAIL', 'missing_path', '路径不存在，请核对：' + ', '.join(missing_paths))

    scope = fields.get('SCOPE_WRITE', '')
    if (role == 'builder' and re.search(r'夹具|fixture|PROVENANCE|回放', card, re.I)
            and 'capture_real_fixture' not in scope
            and not re.search(r'无需新增\s*kind|已有\s*kind\s*覆盖', card, re.I)):
        add('WARN', 'fixture_tool_not_in_scope', '把 examples/capture_real_fixture.py 与 tests/test_real_fixtures.py 放进 SCOPE_WRITE 并写明新增 kind')

    scoped = re.findall(r'(?:src|examples|tests)/[\w./-]+\.py', scope)
    expected = exact_test_list(scoped, repo)
    budget = fields.get('TEST_BUDGET', '')
    declared = re.findall(r'tests/test_[\w./-]+\.py', budget)
    if declared and set(declared) != set(expected):
        add('WARN', 'test_list_mismatch', '精确清单：' + ' '.join(expected))
    if re.search(r'(?:新增测试|测试(?:数量|数))\s*[≤<=>:：约]*\s*([4-9]|[1-9]\d+)', budget) and not re.search(r'理由|因为|由于|原因', budget):
        add('WARN', 'test_budget_high', '新增测试超过 3 个，请说明理由')
    goal = fields.get('GOAL', '')
    if len(re.findall(r'^\s*(?:\d+|[a-z])[.、]', goal, re.MULTILINE)) > 12 or len(card) > 6000:
        add('WARN', 'card_too_large', '建议拆卡，每张卡不超过 3 组规则')
    constraints = json.loads(Path(__file__).with_name('card_constraints.json').read_text(encoding='utf-8'))
    for constraint in constraints:
        hits = [bool(re.search(p, card, re.I)) for p in constraint['patterns']]
        if (all(hits) if constraint['all'] else any(hits)):
            add('WARN', 'constraint_' + constraint['id'], constraint['message'])
    if (fields.get('EXECUTOR', '').lower().split()[:1] == ['claude']
            and re.search(r'taos|redis|TD_CONFIG|REDIS_URL', fields.get('AUTHORIZATION', ''), re.I)
            and '/services/engine-next/shared/venv/bin/python' not in card):
        add('FAIL', 'interpreter_missing', '指定 /services/engine-next/shared/venv/bin/python')
    # Keep failures visible even when a card triggers many warnings.
    ordered = [f for f in findings if f.startswith('FAIL ')] + [f for f in findings if not f.startswith('FAIL ')]
    return ordered[:15]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('card', type=Path)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    findings = lint_card(args.card.read_text(encoding='utf-8'), args.repo)
    for finding in findings:
        print(finding)
    return int(any(f.startswith('FAIL ') for f in findings))


if __name__ == '__main__':
    raise SystemExit(main())
