#!/usr/bin/env python3
"""PM workflow commands."""
import argparse
import ast
from collections import Counter
import difflib
from datetime import datetime, timedelta
import json
import os
import re
import selectors
import shlex
import sys
import subprocess
from pathlib import Path
import time
from zoneinfo import ZoneInfo


TERMINAL = frozenset({'SUCCEEDED', 'SUCCEEDED_DEGRADED', 'FAILED_FINAL',
                      'MISSED', 'SKIPPED_NA', 'SKIPPED_PHASE'})


ROOT = Path('/home/exedev/repos/engine_core')
CARDS = Path('/home/exedev/fw/cards')
PYTHON = '/home/exedev/services/engine-next/shared/venv/bin/python'
CALLBACK = '/home/exedev/fw/bin/card_callback'


def worktree_from_card(text):
    match = re.search(r'^SCOPE_WRITE:[^\n]*', text, re.MULTILINE)
    if match:
        for path in match.group().split(':', 1)[1].split(','):
            root = re.match(r'\s*(/[^\s,]*?-wt-[^/\s,]+)(?:/|\s|$)', path)
            if root:
                return root.group(1)
    return None


def set_status(text, new_line):
    return re.sub(r'^STATUS:[^\r\n]*', lambda _: new_line, text, count=1, flags=re.MULTILINE)


def run_merge(command):
    """Forward lock notices promptly while retaining the merge output for its summary."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env={**os.environ, 'TMPDIR': '/tmp'}) as process:
        chunks = []
        pending = b''
        deadline = time.monotonic() + 3600
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise subprocess.TimeoutExpired(command, 3600)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    chunks.append(chunk)
                    pending += chunk
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if line == b'waiting for another pytest run':
                            print(line.decode(), flush=True)
        return subprocess.CompletedProcess(command, process.wait(), b''.join(chunks).decode(errors='replace'), '')


def accept(args):
    if not re.fullmatch(r'C\d+', args.card):
        print(f'ACCEPT {args.card} REFUSED setup: invalid card ID')
        return 2
    card_path = CARDS / f'{args.card}.md'
    try:
        card_text = card_path.read_text(encoding='utf-8')
        worktree = worktree_from_card(card_text)
        if not worktree:
            raise ValueError('no worktree in SCOPE_WRITE')
        def git(repo, *command):
            return subprocess.run(['git', '-C', str(repo), *command], check=True,
                                  capture_output=True, text=True).stdout.strip()
        branch = git(worktree, 'branch', '--show-current')
        if not branch:
            raise ValueError('worktree has detached HEAD')
        main_worktree = git(ROOT, 'worktree', 'list', '--porcelain').splitlines()[0].removeprefix('worktree ')
        main_branch = git(main_worktree, 'symbolic-ref', '--short', 'HEAD')
        base = git(ROOT, 'merge-base', main_branch, branch)
        if not git(ROOT, 'diff', '--name-only', f'{base}...{branch}'):
            raise ValueError(f'empty diff vs base {base}')
        if not re.search(r'^STATUS:', card_text, re.MULTILINE):
            raise ValueError('card has no STATUS line')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'ACCEPT {args.card} REFUSED setup: {exc}')
        return 2
    audit_cmd = [PYTHON, str(ROOT / 'examples/audit_card.py'), '--card', args.card,
                 '--branch', branch, '--base', base, '--repo', str(ROOT)]
    merge_cmd = [PYTHON, str(ROOT / 'examples/release.py'), 'merge', '--branch', branch, '--card', args.card]
    if args.deep:
        merge_cmd.append('--deep')
    if args.dry_run:
        print(f'worktree {worktree} branch {branch} base {base}')
        print('audit skipped' if args.no_audit else 'audit ' + ' '.join(audit_cmd))
        print('merge ' + ' '.join(merge_cmd))
        print(f'status {card_path} DONE(已合并 <sha>)')
        print(f'callback {CALLBACK} --ack {args.card}')
        return 0
    if not args.no_audit:
        try:
            result = subprocess.run(audit_cmd, capture_output=True, text=True, timeout=900)
            payload = json.loads(result.stdout)
            problems = payload.get('problems', [])
            if result.returncode or payload.get('verdict') != 'PASS':
                detail = '; '.join(map(str, problems)) or result.stderr.strip() or 'audit failed'
                print(f'ACCEPT {args.card} REFUSED audit: {detail[:120]}')
                return 3
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f'ACCEPT {args.card} REFUSED audit: {str(exc)[:120]}')
            return 3
    try:
        result = run_merge(merge_cmd)
        merge_lines = result.stdout.splitlines()
        if result.returncode:
            lines = (result.stdout + result.stderr).splitlines()
            detail = lines[-1] if lines else f'exit {result.returncode}'
            print(f'ACCEPT {args.card} REFUSED merge: {detail[:150]}')
            return 4
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'ACCEPT {args.card} REFUSED merge: {str(exc)[:150]}')
        return 4
    try:
        sha = git(ROOT, 'rev-parse', '--short', 'HEAD')
        current_text = card_path.read_text(encoding='utf-8')
        if not re.search(r'^STATUS:', current_text, re.MULTILINE):
            raise ValueError('card has no STATUS line')
        card_path.write_text(set_status(current_text, f'STATUS: DONE(已合并 {sha})'), encoding='utf-8')
        result = subprocess.run([CALLBACK, '--ack', args.card], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f'exit {result.returncode}')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'ACCEPT {args.card} MERGED; status/callback failed: {exc}')
        return 5
    print(f"ACCEPT {args.card} MERGED {sha} audit={'SKIPPED' if args.no_audit else 'PASS'}")
    summary = next((line for line in reversed(merge_lines) if line.startswith('选中 ')),
                   f'选中 UNKNOWN 个测试文件、共 UNKNOWN 个测试、耗时 UNKNOWN 秒; --deep={"yes" if args.deep else "no"}')
    print(summary)
    return 0


def latest_state(lines, event_id):
    latest = None
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        data = entry.get('data')
        if (entry.get('type') == 'INSTANCE_STATE' and isinstance(data, dict)
                and data.get('event_id') == event_id and isinstance(data.get('state'), dict)):
            latest = data['state']
    return latest



def location(name, default):
    return Path(os.environ.get(name, default))


def card_path(card):
    cards = location('PM_CARDS_DIR', str(CARDS))
    path = cards / (card + '.md')
    if not re.fullmatch(r'C[0-9]+', card) or not path.is_file() or path.resolve().parent != cards.resolve():
        choices = [p.stem for p in cards.glob('C*.md') if re.fullmatch(r'C[0-9]+', p.stem)]
        print(f"card {redact(card)} not found; closest: {', '.join(difflib.get_close_matches(card, choices, n=3, cutoff=0))}")
        return None
    return path


def field(text, name):
    match = re.search(r'^' + re.escape(name) + r':\s*([^\r\n]*)', text, re.MULTILINE)
    return match.group(1).strip() if match else ''


def card_processes(card):
    # Only retain elapsed seconds; never expose the command line or environment.
    found = []
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        ticks = os.sysconf('SC_CLK_TCK')
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                argv = [x.decode(errors='replace') for x in argv if x]
                matched = any(Path(x).name == 'runcard' and i + 1 < len(argv) and argv[i + 1] == card
                              for i, x in enumerate(argv))
                matched |= any(x == 'dispatch' and any(Path(y).name == card + '.md' for y in argv[i + 1:])
                               for i, x in enumerate(argv))
                if matched:
                    stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                    if stat[0] != 'Z':
                        found.append(max(0, int(uptime - int(stat[19]) / ticks)))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    return found


def redact(text):
    value = r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s&]+)"
    text = re.sub(r'(?i)\bset-cookie\s*:[^\r\n]*', '***', text)
    # Preserve the existing two-mask rendering for JWT authorization headers.
    text = re.sub(r'(?i)\bauthorization\s*[=:]\s*(?:bearer|basic)\s+eyJ[A-Za-z0-9_-]{17,}', '*** ***', text)
    text = re.sub(r'(?i)\bauthorization\s*[=:]\s*(?:bearer|basic)\s+' + value, '***', text)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+' + value, '***', text)
    text = re.sub(r'(?i)(--(?:token|password)(?:\s+|=))' + value, r'\1***', text)
    text = re.sub(r'(?i)(?<![\w-])(?:access_token|token)\s*=' + value, '***', text)
    text = re.sub(r'(?i)\b(password|passwd|secret|api[_-]?key|authorization|cookie)\s*[=:]\s*\S+', '***', text)
    text = re.sub(r'(?i)(rediss?://)[^\s@]*@', r'\1***@', text)
    return re.sub(r'\beyJ[A-Za-z0-9_-]{17,}', '***', text)


def denials(args):
    path = location('PM_GUARD_LOG', '/home/exedev/fw/retros/guard.log')
    counts = Counter()
    examples = []
    total = 0
    try:
        with path.open(encoding='utf-8', errors='replace') as lines:
            for line in lines:
                fields = line.rstrip('\n').split('\t', 4)
                if len(fields) != 5 or not fields[0].startswith(args.date) or fields[1] != 'DENY':
                    continue
                total += 1
                counts[fields[3].split(':', 1)[0]] += 1
                if len(examples) < 5:
                    examples.append(redact(fields[4])[:80])
    except OSError as exc:
        print(redact(f'guard log unavailable: {exc}'))
        return 1
    print(f'DENY {args.date}: {total}')
    for category, count in counts.most_common(6):
        print(redact(f'{category}: {count}'))
    for command in examples:
        print(f'示例: {command}')
    print(f"目标 <5/天;本日 {total} 次 {'达标' if total < 5 else '超标'}")
    return 0


def show_status(card, path):
    text = path.read_text(encoding='utf-8')
    for name in ('STATUS', 'ROLE', 'EXECUTOR', 'TITLE'):
        print(f'{name}: {redact(field(text, name))[:200]}')
    report = location('PM_REPORTS_DIR', '/home/exedev/fw/reports') / (card + '.md')
    if report.is_file():
        with report.open(encoding='utf-8', errors='replace') as f:
            first = redact(f.readline().strip())[:160]
        print(f'report: yes size={report.stat().st_size} first={first}')
    else:
        print('report: no')
    runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
    logs = [p for p in runs.glob(card + '*') if p.is_file()]
    if logs:
        log = max(logs, key=lambda p: p.stat().st_mtime)
        print('log modified: ' + datetime.fromtimestamp(log.stat().st_mtime).isoformat(timespec='seconds'))
        from collections import deque
        with log.open(encoding='utf-8', errors='replace') as f:
            for line in deque(f, maxlen=3):
                print(redact(line.rstrip())[:160])
    else:
        print('log: none')
    processes = card_processes(card)
    print(f'process: running {max(processes)}s' if processes else 'process: exited')
    worktree = worktree_from_card(text)
    if worktree:
        for label, command in [('git log', ['log', '-1', '--oneline']), ('git status', ['status', '--short'])]:
            result = subprocess.run(['git', '-C', worktree, *command], capture_output=True, text=True)
            lines = result.stdout.splitlines()[:1 if label == 'git log' else 5]
            print(label + ': ' + (' | '.join(redact(line)[:160] for line in lines) or '(empty or unavailable)'))
    evidence = Path('/home/exedev/fw/evidence') / card
    print(f'evidence files: {sum(1 for p in evidence.rglob("*") if p.is_file()) if evidence.is_dir() else 0}')
    if field(text, 'ROLE').lower() == 'builder' and worktree:
        try:
            base = os.environ.get('PM_BASE')
            if not base:
                integration = subprocess.run(['git', '-C', str(ROOT), 'symbolic-ref', '--short', 'HEAD'],
                                             check=True, capture_output=True, text=True).stdout.strip()
                base = subprocess.run(['git', '-C', worktree, 'merge-base', 'HEAD', integration],
                                      check=True, capture_output=True, text=True).stdout.strip()
            try:
                from examples.test_audit import audit
            except ModuleNotFoundError:
                from test_audit import audit
            findings = audit(worktree, base, text)
            actionable = [line for line in findings if not line.startswith('NOTE ')]
            for line in actionable[:4]:
                print(redact(line))
            if not actionable:
                print('audit: ok')
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f'audit: unavailable ({redact(str(exc))[:120]})')
    return 0



def lint_module():
    # Support both module import and direct execution of examples/pm.py.
    try:
        from examples import card_lint
    except ModuleNotFoundError:
        import card_lint
    return card_lint


def scoped_files(text, worktree):
    scope = field(text, 'SCOPE_WRITE')
    files = []
    for match in re.findall(r'(?:src|examples|tests)/[\w./-]+\.py', scope):
        files.append(match)
    return files


def testlist(args):
    repo = Path(__file__).resolve().parents[1]
    if args.card:
        path = card_path(args.card)
        if path is None:
            return 2
        text = path.read_text(encoding='utf-8')
        repo = Path(worktree_from_card(text) or repo)
        files = scoped_files(text, repo)
    else:
        files = [f.strip() for f in (args.files or '').split(',') if f.strip()]
    selected = lint_module().exact_test_list(files, repo)
    candidates = [f'tests/test_{Path(f).stem}.py' for f in files
                  if f.startswith(('src/', 'examples/')) and f.endswith('.py')]
    candidates += [f for f in files if f.startswith('tests/') and Path(f).name.startswith('test_') and f.endswith('.py')]
    selected = list(dict.fromkeys([*candidates, *selected]))
    for item in selected:
        print(item)
    print(f"cd {shlex.quote(str(repo))} && TMPDIR=/tmp PYTHONPATH=src:. env -u TD_CONFIG -u REDIS_URL {shlex.quote(PYTHON)} -m pytest -q --no-header -p no:cacheprovider {' '.join(map(shlex.quote, selected))}; echo exit=$?")
    return 0


def card_block(text, name):
    match = re.search(r'^' + re.escape(name) + r':[ \t]*(.*(?:\n(?![A-Z][A-Z0-9_]*:).*)*)', text, re.M)
    return match.group(1).strip() if match else ''


def title_keywords(title):
    words = set(re.findall(r'[a-zA-Z][a-zA-Z0-9_]+', title.lower()))
    for part in re.findall(r'[\u4e00-\u9fff]+', title):
        words.update(part[i:i + 2] for i in range(len(part) - 1))
    return words


def pack(args):
    path = card_path(args.card)
    if path is None:
        return 2
    text = path.read_text(encoding='utf-8')
    local_repo = Path(__file__).resolve().parents[1]
    worktree = Path(worktree_from_card(text) or local_repo)
    files = scoped_files(text, worktree)
    scope = field(text, 'SCOPE_WRITE')
    sections = []
    sections.append(('目标与不做什么', [f'TITLE: {field(text, "TITLE")}', f'SERVES: {field(text, "SERVES")}',
                     *('GOAL: ' + line for line in card_block(text, 'GOAL').splitlines()[:5]),
                     f'NOT_DO: {card_block(text, "NOT_DO")}']))
    constraints = json.loads(Path(__file__).with_name('card_constraints.json').read_text(encoding='utf-8'))
    matched = []
    for c in constraints:
        hits = [bool(re.search(p, text, re.I)) for p in c['patterns']]
        if c.get('always') or any(re.search(p, scope, re.I) for p in c.get('scope_patterns', [])) or (all(hits) if c['all'] else any(hits)):
            matched.append(f"- {c['id']}: {c['message']}")
    sections.append(('既定设计约束', matched))
    code, terms, owners = [], set(), {}
    for rel in dict.fromkeys(files):
        source = worktree / rel
        if not source.is_file():
            source = local_repo / rel
        if not source.is_file():
            continue
        try:
            content = source.read_text(encoding='utf-8')
            tree = ast.parse(content)
        except (OSError, UnicodeError, SyntaxError):
            continue
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        file_terms = {n.name for n in nodes}
        file_terms.update(re.findall(r'core:[a-z_:]+', content))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.fullmatch(r'[a-z][a-z0-9_]*_v[0-9]+', n.value):
                file_terms.add(n.value)
        for term in file_terms:
            owners.setdefault(term, set()).add(rel)
        terms.update(file_terms)
        code.append(f'{rel}:')
        for n in nodes[:15]:
            if isinstance(n, ast.ClassDef):
                signature = f'class {n.name}' + ('(' + ', '.join(ast.unparse(x) for x in n.bases) + ')' if n.bases else '')
            else:
                signature = ('async def ' if isinstance(n, ast.AsyncFunctionDef) else 'def ') + n.name + '(' + ast.unparse(n.args) + ')' + (' -> ' + ast.unparse(n.returns) if n.returns else '')
            code.append(f'  {signature} :{n.lineno}')
        if len(nodes) > 15:
            code.append('  …已截断')
    counts = Counter()
    if terms:
        repo = worktree if (worktree / '.git').exists() else local_repo
        command = ['git', '-C', str(repo), 'grep', '-n', '-F']
        for term in sorted(terms):
            command.extend(['-e', term])
        result = subprocess.run(command + ['--', 'src/', 'examples/', 'tests/'], capture_output=True, text=True, timeout=10)
        if result.returncode not in (0, 1):
            raise RuntimeError('git grep failed')
        for line in result.stdout.splitlines():
            name, _, snippet = line.split(':', 2)
            if any(term in snippet and (owners[term] - {name}) for term in terms):
                counts[name] += 1
    if len(code) > 37:
        code = code[:36] + ['…代码签名已截断']
    code.append('消费方清单:')
    code.extend(f'  {name}: {count}' for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:15])
    sections.append(('代码地图', code))
    keywords = title_keywords(field(text, 'TITLE'))
    precedents = []
    cards = location('PM_CARDS_DIR', str(CARDS))
    for candidate in cards.glob('C*.md'):
        if candidate == path:
            continue
        other = candidate.read_text(encoding='utf-8')
        if not field(other, 'STATUS').startswith('DONE') or field(other, 'ROLE') != field(text, 'ROLE'):
            continue
        other_words = title_keywords(field(other, 'TITLE'))
        score = len(keywords & other_words) / len(keywords | other_words) if keywords | other_words else 0
        report = location('PM_REPORTS_DIR', '/home/exedev/fw/reports') / candidate.name
        first = report.read_text(encoding='utf-8').splitlines()[:1] if report.is_file() else []
        precedents.append((score, candidate.stem, field(other, 'TITLE')[:40], first[0][:100] if first else '报告缺失'))
    prior = [f'{ident}|{title}|{first}' for _, ident, title, first in sorted(precedents, key=lambda x: (-x[0], x[1]))[:3]]
    retros = location('PM_RETROS_DIR', '/home/exedev/fw/retros')
    for item in sorted(retros.glob('*.md')):
        content = item.read_text(encoding='utf-8')
        if sum(word in content.lower() for word in keywords) >= 2:
            heading = next((line.lstrip('# ').strip() for line in content.splitlines() if line.startswith('#')), '')
            prior.append(f'{item.name}|{heading}')
            if len(prior) >= min(3, len(precedents)) + 3:
                break
    sections.append(('先例', prior))
    facts = Path(__file__).with_name('card_context_facts.md').read_text(encoding='utf-8').splitlines()
    capture = ast.parse((local_repo / 'examples/capture_real_fixture.py').read_text(encoding='utf-8'))
    for node in ast.walk(capture):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'kind':
            choices = next((ast.literal_eval(k.value) for k in node.keywords if k.arg == 'choices'), [])
            facts.append('fixture kinds: ' + ', '.join(choices))
            break
    facts.append('验收清单: ' + ' '.join(lint_module().exact_test_list(files, worktree if worktree.is_dir() else local_repo)))
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    try:
        try:
            from engine_core.trade_day_context import next_trade_date
        except ModuleNotFoundError:
            sys.path.insert(0, str(local_repo / 'src'))
            from engine_core.trade_day_context import next_trade_date
        following = next_trade_date(None, today)
    except (OSError, ValueError, ImportError) as exc:
        following = 'UNKNOWN (' + type(exc).__name__ + ')'
    facts.append(f'当前日期: {today}; 下一个交易日: {following}')
    facts.append(f'MODEL: {field(text, "MODEL")}; EFFORT: {field(text, "EFFORT")}')
    sections.append(('环境事实', facts))
    sections.append(('验收口径', [f'{key}: {card_block(text, key)}' for key in ('REAL_DATA', 'ORACLE', 'ACCEPT_BY', 'TEST_BUDGET', 'STUBS_ALLOWED')] + ['遇到规格内部矛盾时选与真实历史失败原因一致者并写入报告 OPEN,不要停下来等 PM。']))
    staff = location('PM_STAFF_DIR', '/home/exedev/fw/design/staff')
    culture_path = staff / 'CULTURE.md'
    culture = culture_path.read_text(encoding='utf-8').splitlines() if culture_path.is_file() else []
    if len(culture) > 12:
        culture = [line.split('。', 1)[0] for line in culture if line.startswith('#') or re.match(r'^\d+\.', line)][:11] + ['…守则已截断']
    role = field(text, 'ROLE')
    experience = staff / role / 'EXPERIENCE.md' if role in ('builder', 'runner', 'auditor', 'pm') else staff / '__missing__'
    lessons = [line for line in experience.read_text(encoding='utf-8').splitlines() if re.match(r'^\d{4}-\d{2}-\d{2}\|', line)] if experience.is_file() else []
    words = title_keywords(field(text, 'TITLE') + ' ' + card_block(text, 'GOAL'))
    ranked = sorted(((sum(word in line.lower().split('|', 2)[-1] for word in words), i, line) for i, line in enumerate(lessons)), key=lambda x: (-x[0], -x[1]))
    selected = [line for score, _, line in ranked if score > 0][:8]
    for line in lessons[-3:]:
        if line not in selected:
            selected.append(line)
    sections.append(('岗位记忆', culture + selected))
    epic = field(text, 'EPIC')
    epic_path = location('PM_EPIC_DIR', '/home/exedev/fw/design/epics') / (epic + '.md')
    if re.fullmatch(r'[a-zA-Z0-9_-]+', epic) and epic_path.is_file():
        brief = epic_path.read_text(encoding='utf-8').splitlines()
        if len(brief) > 60:
            brief = brief[:60] + ['…专题简报已截断']
    else:
        brief = ['无专题简报']
    sections.append(('专题简报', brief))
    handoffs = []
    dependencies = [x.strip() for x in field(text, 'DEPENDS').split(',') if x.strip()]
    for dependency in dependencies[:3]:
        if not re.fullmatch(r'C[0-9]+', dependency):
            handoffs.append(dependency + ': 无效卡号')
            continue
        report = location('PM_REPORTS_DIR', '/home/exedev/fw/reports') / (dependency + '.md')
        if not report.is_file():
            handoffs.append(dependency + ': 报告缺失')
            continue
        report_lines = report.read_text(encoding='utf-8').splitlines()
        start = next((i for i, line in enumerate(report_lines) if re.match(r'^#{1,6}\s*.*HANDOFF', line, re.I)), None)
        if start is None:
            handoffs.extend([dependency + ' (无 HANDOFF,取末 8 行)', *report_lines[-8:]])
        else:
            end = next((i for i in range(start + 1, len(report_lines)) if re.match(r'^#{1,6}\s', report_lines[i])), len(report_lines))
            handoffs.extend([dependency, *report_lines[start:min(end, start + 8)]])
    if len(dependencies) > 3:
        handoffs.append('…前驱已截断')
    sections.append(('交接单链', handoffs or ['无前驱']))
    quotas = (8, 12, 32, 7, 10, 15, 22, 18, 12)
    lines = []
    for (heading, content), quota in zip(sections, quotas):
        lines.append('## ' + heading)
        flattened = [part for item in content for part in item.splitlines()]
        if len(flattened) > quota:
            flattened = flattened[:quota - 1] + ['…本节已截断']
        lines.extend(flattened)
    out = Path(args.out_dir) if args.out_dir else location('PM_CONTEXT_DIR', '/home/exedev/fw/context')
    out.mkdir(parents=True, exist_ok=True)
    target = out / (args.card + '.md')
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if not re.search(r'^CONTEXT:', text, re.M):
        updated, count = re.subn(r'^(TITLE:[^\n]*\n)', r'\1CONTEXT: fw/context/' + args.card + r'.md(先读)\n', text, count=1, flags=re.M)
        if not count:
            raise ValueError('card has no TITLE')
        path.write_text(updated, encoding='utf-8')
    print(f'{target} {len(lines)} lines')
    return 0


def try_pack(card):
    try:
        result = pack(argparse.Namespace(card=card, out_dir=None))
        if result:
            print(f'warning: pack failed for {card}')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'warning: pack failed: {exc}')


def newcard(args):
    recipe_path = Path(__file__).with_name('card_recipes') / (args.recipe + '.json')
    if not re.fullmatch(r'[a-z0-9-]+', args.recipe) or not recipe_path.is_file():
        print('unknown recipe')
        return 2
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    tiers = json.loads(recipe_path.with_name('tiers.json').read_text(encoding='utf-8'))
    tier = args.tier or recipe['default_tier']
    if tier not in tiers:
        print('unknown tier')
        return 2
    values = vars(args).copy()
    if args.goal_file:
        values['goal'] = Path(args.goal_file).read_text(encoding='utf-8').strip()
    for item in args.param:
        if '=' not in item:
            print('invalid --param: expected k=v')
            return 2
        key, value = item.split('=', 1)
        values[key.replace('-', '_')] = value
    missing = [key for key in recipe['required'] if not values.get(key)]
    if missing:
        print('missing required: ' + ', '.join(missing))
        return 2
    cards = location('PM_CARDS_DIR', str(CARDS))
    repo = Path(__file__).resolve().parents[1]
    numbers = [int(p.stem[1:]) for p in cards.glob('C*.md') if re.fullmatch(r'C[0-9]+', p.stem)]
    number = max(numbers, default=0) + 1
    while True:
        ident = f'C{number}'
        path = cards / (ident + '.md')
        if args.dry_run:
            break
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            number += 1
    worktree = repo.parent / ('engine_core-wt-' + ident)
    scope = [x.strip() for x in (values.get('scope') or '').split(',') if x.strip()]
    files = [x for x in scope if x.startswith(('src/', 'examples/', 'tests/'))]
    selected = lint_module().exact_test_list(files, repo)
    context = {**values, 'id': ident, 'test_files': ' '.join(selected)}
    fixed = {k: v.format_map(context) for k, v in recipe['fixed'].items()}
    expanded = [str(worktree / x) if not Path(x).is_absolute() else x for x in scope]
    for extra in (location('PM_REPORTS_DIR', '/home/exedev/fw/reports') / (ident + '.md'),
                  location('PM_EVIDENCE_DIR', '/home/exedev/fw/evidence') / ident):
        if str(extra) not in expanded:
            expanded.append(str(extra))
    entries = [('ID', ident), ('STATUS', 'ready'), ('LANE', fixed['LANE']),
               ('ROLE', fixed['ROLE']), ('EXECUTOR', fixed['EXECUTOR']),
               ('MODEL', tiers[tier]['MODEL']), ('EFFORT', tiers[tier]['EFFORT']), ('COMMIT', fixed['COMMIT']),
               ('TITLE', values['title']), ('SERVES', values['serves']), ('PLAN', values['plan']),
               ('APPROVAL', values['approval']), ('SCOPE_WRITE', ', '.join(expanded)),
               ('REAL_DATA', values['real_data']), ('ORACLE', values['oracle']),
               ('ACCEPT_BY', values['accept_by'])]
    entries += [(key, values[key.lower()]) for key in ('EPIC', 'DEPENDS') if values.get(key.lower())]
    if fixed['ROLE'] == 'runner':
        entries += [('QUESTION', values['question']), ('NEED_DATA', fixed['NEED_DATA'])]
    entries += [(k, fixed[k]) for k in ('TEST_BUDGET', 'STUBS_ALLOWED', 'AUTHORIZATION') if k in fixed]
    understanding = "第 0 步:先写 ≤8 行'我的理解'(目标、不做什么、关键约束、怎么验收、不确定点)到 evidence 目录下 understanding.md,再开工。\n" if tier == 'hard' else ''
    goal = understanding + '先读 CONTEXT 文件。\n' + values['goal']
    if fixed.get('GOAL_SUFFIX'):
        goal += '\n' + fixed['GOAL_SUFFIX']
    entries += [('GOAL', goal)] + [(k, fixed[k]) for k in ('NOT_DO', 'REPORT', 'ACCEPT')]
    card = '\n'.join(f'{k}: {v}' for k, v in entries) + '\n'
    if args.dry_run:
        print(card, end='')
        return 0
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(card)
    if fixed['ROLE'] == 'builder' and scope and not (args.no_worktree or os.environ.get('PM_NO_WORKTREE') == '1'):
        result = subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', ident,
                                 str(worktree), os.environ.get('PM_BASE', 'HEAD')], capture_output=True, text=True)
        if result.returncode:
            print(f'{ident} {path}\nworktree failed: {result.stderr.strip()}')
            return 2
    if not args.no_pack:
        try_pack(ident)
        card = path.read_text(encoding='utf-8')
    findings = lint_module().lint_card(card, worktree if worktree.is_dir() else repo)
    print(f'{ident} {path}')
    for finding in findings:
        print(finding)
    if not findings:
        print('lint PASS')
    print(f'bin/pm dispatch {ident}')
    return 2 if any(f.startswith('FAIL ') for f in findings) else 0

def card_command(args):
    if args.command == 'cards':
        cards = location('PM_CARDS_DIR', str(CARDS))
        reports = location('PM_REPORTS_DIR', '/home/exedev/fw/reports')
        paths = sorted((p for p in cards.glob('C*.md') if re.fullmatch(r'C[0-9]+', p.stem)),
                       key=lambda p: int(p.stem[1:]), reverse=True)
        rows = []
        for path in paths:
            text = path.read_text(encoding='utf-8')
            status = field(text, 'STATUS')
            if args.open and not status.startswith(('ready', 'dispatched')):
                continue
            if len(rows) >= max(0, args.limit):
                break
            rows.append(f"{path.stem} | {status[:50]} | {field(text, 'ROLE')} | {field(text, 'EXECUTOR')} | {field(text, 'TITLE')[:40]} | {'有' if (reports / path.name).is_file() else '无'}")
        group = max(1, (len(rows) + 14) // 15)
        for start in range(0, len(rows), group):
            print('  ;  '.join(rows[start:start + group]))
        return 0
    path = card_path(args.card)
    if path is None:
        return 2
    if args.command == 'status':
        return show_status(args.card, path)
    if args.command == 'setstatus':
        if len(args.text) > 200 or '\n' in args.text or '\r' in args.text:
            print('invalid status text')
            return 2
        text = path.read_text(encoding='utf-8')
        if not re.search(r'^STATUS:', text, re.MULTILINE):
            print('missing STATUS')
            return 2
        print('before: STATUS: ' + field(text, 'STATUS'))
        path.write_text(set_status(text, 'STATUS: ' + args.text), encoding='utf-8')
        print('after: STATUS: ' + args.text)
        return 0
    if args.command == 'dispatch':
        if not args.no_pack:
            try_pack(args.card)
        try:
            if not args.no_lint:
                lint = os.environ.get('PM_CARD_LINT', str(Path(__file__).with_name('card_lint.py')))
                result = subprocess.run([PYTHON, lint, str(path)], capture_output=True, text=True)
                if result.stdout or result.stderr:
                    print((result.stdout + result.stderr).rstrip())
                if result.returncode:
                    return 2
            result = subprocess.run([os.environ.get('PM_PREFLIGHT', '/home/exedev/fw/bin/preflight_card'), str(path)],
                                    capture_output=True, text=True)
            if result.returncode:
                print('\n'.join((result.stdout + result.stderr).splitlines()[:8]))
                return 2
            if not field(path.read_text(encoding='utf-8'), 'STATUS').startswith('ready'):
                print('refused: STATUS is not ready')
                return 2
            if card_processes(args.card):
                print('refused: card already running')
                return 2
            env = os.environ.copy()
            if args.to:
                person = next((p for p in load_roster() if p['name'] == args.to), None)
                if not person or person.get('status') != 'active':
                    print('refused: staff unavailable')
                    return 2
                cards_dir = location('PM_CARDS_DIR', str(CARDS))
                for other in cards_dir.glob('C*.md'):
                    if other != path and field(other.read_text(encoding='utf-8'), 'STAFF') == args.to and card_processes(other.stem):
                        print('refused: staff already running')
                        return 2
                current = path.read_text(encoding='utf-8')
                if field(current, 'ROLE') == 'auditor':
                    references = re.findall(r'\bC[0-9]+\b', '\n'.join(line for line in current.splitlines() if line.startswith(('DEPENDS:', 'SERVES:', 'GOAL:', 'TITLE:'))))
                    for ref in references:
                        target = cards_dir / (ref + '.md')
                        if ref != args.card and target.is_file() and field(target.read_text(encoding='utf-8'), 'STAFF') == args.to:
                            print('refused: auditor is implementer')
                            return 2
                current = set_field(current, 'STAFF', args.to)
                for name in ('MODEL', 'EFFORT'):
                    if not field(current, name):
                        current = set_field(current, name, person[name.lower()])
                if person.get('ctx_tokens', 0) > int(os.environ.get('PM_STAFF_CTX_LIMIT', '60000')) or len(person.get('cards', [])) >= int(os.environ.get('PM_STAFF_MAX_CARDS', '3')):
                    rollover_staff(person['name'], '自动:ctx/卡数超限')
                    person = next(p for p in load_roster() if p['name'] == args.to)
                env['FW_COMPACT_LIMIT'] = os.environ.get('PM_STAFF_COMPACT', '40000')
                env.pop('RESUME_ID', None)
                env.pop('FW_PERSIST', None)
                if person.get('session_id'):
                    env['RESUME_ID'] = person['session_id']
                    current = sync_preamble(current, person)
                else:
                    env['FW_PERSIST'] = '1'
                path.write_text(current, encoding='utf-8')
            runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
            runs.mkdir(parents=True, exist_ok=True)
            if args.to:
                (runs / (args.card + '.staff')).write_text(json.dumps({'name': args.to, 'mode': 'resume' if env.get('RESUME_ID') else 'fresh', 'ts': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')}, ensure_ascii=False) + '\n', encoding='utf-8')
            (runs / (args.card + '.dispatch.timestamp')).write_text(str(time.time_ns()), encoding='utf-8')
            with (runs / (args.card + '.dispatch.out')).open('ab') as out:
                process = subprocess.Popen([os.environ.get('PM_RUNCARD', '/home/exedev/fw/bin/runcard'), args.card],
                                           stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env)
            current = path.read_text(encoding='utf-8')
            stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
            path.write_text(set_status(current, f'STATUS: dispatched({stamp})'), encoding='utf-8')
            print(f'dispatched {args.card} pid={process.pid};盯进度: bin/pm status {args.card};等待: bin/pm wait-card {args.card}')
            return 0
        except OSError as exc:
            print(f'dispatch failed: {exc.strerror}')
            return 2
    if args.timeout < 0 or args.interval <= 0:
        print('timeout must be nonnegative and interval must be positive')
        return 2
    deadline = time.monotonic() + args.timeout
    report = location('PM_REPORTS_DIR', '/home/exedev/fw/reports') / (args.card + '.md')
    runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
    marker = runs / (args.card + '.dispatch.timestamp')
    try:
        dispatch_ns = int(marker.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        try:
            dispatch_ns = (runs / (args.card + '.dispatch.out')).stat().st_mtime_ns
        except OSError:
            dispatch_ns = None
    announced = False
    while True:
        running = bool(card_processes(args.card))
        now = time.monotonic()
        if not running:
            current = path.read_text(encoding='utf-8')
            try:
                fresh = dispatch_ns is not None and report.stat().st_mtime_ns > dispatch_ns
            except OSError:
                fresh = False
            if fresh and field(current, 'STATUS').startswith('dispatched'):
                with report.open(encoding='utf-8', errors='replace') as handle:
                    first = handle.readline().strip()
                verdict = re.search(r'\bVERDICT\s*:\s*(DONE|PARTIAL|BLOCKED)\b', first, re.I)
                if verdict:
                    date = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
                    status = f'{verdict.group(1).upper()}(自动;待 PM 复核;{date})'
                else:
                    status = 'PARTIAL(自动;报告首行未含 VERDICT)'
                path.write_text(set_status(current, 'STATUS: ' + status), encoding='utf-8')
            if fresh and field(current, 'STAFF'):
                staff_writeback(args.card, current, runs)
            print('exited')
            return show_status(args.card, path)
        if not announced:
            print('running', flush=True)
            announced = True
        remaining = deadline - now
        if remaining <= 0:
            print('TIMEOUT')
            return 2
        time.sleep(min(args.interval, remaining))


def status_category(status):
    value = status.strip().upper()
    if value.startswith(('READY', 'PLANNED', 'DRAFT', 'PREPARED', 'APPROVED')):
        return 'ready'
    if value.startswith('DISPATCHED'):
        return 'dispatched'
    if value.startswith(('DONE', '已合并', 'MERGED')):
        return 'DONE'
    if value.startswith('PARTIAL'):
        return 'PARTIAL'
    if value.startswith('BLOCKED'):
        return 'BLOCKED'
    if value.startswith(('SUPERSEDED', 'REJECTED')):
        return 'SUPERSEDED'
    return '其他'


def reconcile(args):
    cards = location('PM_CARDS_DIR', str(CARDS))
    reports = location('PM_REPORTS_DIR', '/home/exedev/fw/reports')
    changes = []
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    for path in sorted(cards.glob('C*.md')):
        if not re.fullmatch(r'C[0-9]+', path.stem):
            continue
        content = path.read_text(encoding='utf-8')
        if not field(content, 'STATUS').lower().startswith(('ready', 'dispatched')):
            continue
        report = reports / path.name
        if not report.is_file() or not report.stat().st_size:
            continue
        first = next((line.strip() for line in report.read_text(encoding='utf-8').splitlines() if line.strip()), '')
        verdict = re.search(r'\b(DONE|PARTIAL|BLOCKED)\b', first, re.IGNORECASE)
        result = verdict.group(1).upper() if verdict else 'PARTIAL'
        detail = f'对账 {today};待复核' if verdict else f'对账 {today};待复核;报告首行未含 VERDICT'
        changes.append((path, content, result, f'STATUS: {result}({detail})'))
    counts = Counter(item[2] for item in changes)
    print(f'陈旧卡: {len(changes)}')
    print('将写入: ' + ' '.join(f'{key}={counts[key]}' for key in ('DONE', 'PARTIAL', 'BLOCKED')))
    for path, content, result, line in changes[:min(args.limit, 10) if args.limit is not None else 10]:
        print(f'{path.stem}: {line}')
    if args.apply:
        for path, content, result, line in changes[:args.limit] if args.limit is not None else changes:
            path.write_text(set_status(content, line), encoding='utf-8')
    return 0


def metrics(args):
    cards = location('PM_CARDS_DIR', str(CARDS))
    reports = location('PM_REPORTS_DIR', '/home/exedev/fw/reports')
    runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
    categories = ('ready', 'dispatched', 'DONE', 'PARTIAL', 'BLOCKED', 'SUPERSEDED')
    counts = {key: 0 for key in (*categories, '其他')}
    rows = []
    stale = 0
    total = 0
    for path in cards.glob('C*.md'):
        if not re.fullmatch(r'C[0-9]+', path.stem):
            continue
        report = reports / path.name
        archives = list((runs / 'archive').glob(path.stem + '.md.*'))
        marker = runs / (path.stem + '.dispatch.timestamp')
        log = runs / (path.stem + '.dispatch.out')
        status = field(path.read_text(encoding='utf-8'), 'STATUS')
        dispatched = bool(archives or report.is_file() or marker.is_file() or log.is_file() or
                          status.startswith(('dispatched', 'DONE', 'PARTIAL', 'BLOCKED')))
        if args.since:
            times = [p.stat().st_mtime for p in [*archives, report, marker, log] if p.is_file()]
            if not times or datetime.fromtimestamp(max(times), ZoneInfo('Asia/Shanghai')).date().isoformat() < args.since:
                continue
        total += 1
        category = status_category(status)
        counts[category] += 1
        stale += category == 'ready' and report.is_file()
        if dispatched:
            rows.append((path.stem, len(archives) + 1, status, report.is_file() and report.stat().st_size > 0))
    rows.sort(key=lambda row: (-row[1], int(row[0][1:])))
    average = sum(row[1] for row in rows) / len(rows) if rows else 0
    reported = [row for row in rows if row[3]]
    rate = sum(count == 1 and status_category(status) == 'DONE' for _, count, status, _ in reported) / len(reported) if reported else 0
    print(f'卡总数: {total} | 已派发: {len(rows)}')
    print('状态: ' + ' '.join(f'{key}={counts[key]}' for key in counts))
    print(f'陈旧 ready: {stale}')
    print('派发最多: ' + (' '.join(f'{card}:{count}' for card, count, _, _ in rows[:3]) or '无'))
    print(f'平均派发: {average:.2f} | 一次通过率: {rate:.1%}')
    log = location('PM_GUARD_LOG', '/home/exedev/fw/retros/guard.log')
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    try:
        with log.open(encoding='utf-8', errors='replace') as handle:
            deny = sum(line.startswith(today) and len(parts := line.split('\t')) > 1 and parts[1] == 'DENY' for line in handle)
        print(f'今日 DENY: {deny}')
    except OSError:
        print('今日 DENY: UNKNOWN')
    print('目标:一次通过率≥70%;平均派发≤1.5;陈旧 ready=0;DENY<5/天')
    print('一次通过率口径: 已派发且有非空报告的卡')
    return 0

def lesson_add(args):
    if (not re.fullmatch(r'C[0-9]+', args.card) or any(not value.strip() or len(value) > 200 or any(c in value for c in '\r\n|') for value in (args.mistake, args.rule))):
        print('无效字段')
        return 2
    path = location('PM_STAFF_DIR', '/home/exedev/fw/design/staff') / args.role / 'EXPERIENCE.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding='utf-8') if path.exists() else ''
    line = f"{datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()}|{args.card}|{args.mistake}|{args.rule}"
    if line in old.splitlines():
        print('已存在,跳过')
    else:
        with path.open('a', encoding='utf-8') as output:
            output.write(('\n' if old and not old.endswith('\n') else '') + line + '\n')
        print('已追加: ' + line)
    return 0


def scorecard(args):
    cards = location('PM_CARDS_DIR', str(CARDS))
    runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
    groups = {}
    card_groups = {}
    usage = runs / 'usage.tsv'
    usage_rows = [line.split('\t') for line in usage.read_text(encoding='utf-8').splitlines()] if usage.is_file() else []
    inferred = {row[0]: tuple(row[1:4]) for row in usage_rows if len(row) == 5}
    for path in cards.glob('C*.md'):
        if not re.fullmatch(r'C[0-9]+', path.stem):
            continue
        if args.since and datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo('Asia/Shanghai')).date().isoformat() < args.since:
            continue
        content = path.read_text(encoding='utf-8')
        values = tuple(field(content, name) for name in ('ROLE', 'MODEL', 'EFFORT'))
        fallback = inferred.get(path.stem, ('?', '?', '?'))
        key = tuple(value or fallback[i] for i, value in enumerate(values))
        group = groups.setdefault(key, {'cards': 0, 'dispatch': 0, 'done': 0, 'tokens': [], 'missing': 0, 'latest': 0})
        group['cards'] += 1
        group['latest'] = max(group['latest'], int(path.stem[1:]))
        group['dispatch'] += 1 + len(list((runs / 'archive').glob(path.stem + '.md.*')))
        group['done'] += field(content, 'STATUS').startswith('DONE')
        card_groups[path.stem] = key
    if usage.is_file():
        for row in usage_rows:
            if len(row) != 5 or card_groups.get(row[0]) != tuple(row[1:4]):
                continue
            group = groups[tuple(row[1:4])]
            if row[4].isdigit():
                group['tokens'].append(int(row[4]))
            else:
                group['missing'] += 1
    rows = []
    for key, group in sorted(groups.items(), key=lambda item: (-item[1]['latest'], item[0])):
        count = group['cards']
        average = f"{sum(group['tokens']) / len(group['tokens']):.0f}" if group['tokens'] else '?'
        rows.append(f"{'×'.join(key)} 卡数={count} 平均派发={group['dispatch']/count:.2f} DONE={group['done']/count:.1%} 平均 tokens={average} 缺数据={group['missing']}")
    for row in rows[:12]:
        print(row)
    if len(rows) > 12:
        print(f'…另有 {len(rows)-12} 组已截断')
    staff = location('PM_STAFF_DIR', '/home/exedev/fw/design/staff')
    totals = []
    for role in ('builder', 'runner', 'auditor', 'pm'):
        path = staff / role / 'EXPERIENCE.md'
        count = sum(bool(re.match(r'^\d{4}-\d{2}-\d{2}\|', line)) for line in path.read_text(encoding='utf-8').splitlines()) if path.is_file() else 0
        totals.append(f'{role} {count}')
    print('经验册条数:' + ' / '.join(totals))
    return 0


def staff_root():
    return location('PM_STAFF_PEOPLE_DIR', '/home/exedev/fw/staff')


def load_roster():
    path = staff_root() / 'roster.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def save_roster(roster):
    root = staff_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / 'roster.json.tmp'
    temporary.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(root / 'roster.json')


def hire(args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.name):
        print('invalid name')
        return 2
    roster = load_roster()
    directory = staff_root() / args.name
    if directory.exists() or any(p['name'] == args.name for p in roster):
        print('staff already exists')
        return 2
    date = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    files = {'PROFILE.md': f'岗位: {args.role}\n模型: {args.model}\n强度: {args.effort}\n入职日: {date}\n会话 ID: \n信任: L0 试用\n状态: active\n',
             'MEMORY.md': '# core 记忆\n≤1.5k token，由本人每张卡后更新。\n\n## 导师点评\n',
             'SKILLS.md': '# SKILLS\n', 'RULES.md': '# RULES\n'}
    if args.dry_run:
        for name, content in files.items():
            print(f'{directory / name}\n{content}', end='')
        return 0
    directory.mkdir(parents=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding='utf-8')
    roster.append(dict(name=args.name, role=args.role, model=args.model, effort=args.effort, session_id='', hired=date, trust='L0', status='active', ctx_tokens=0, cards=[]))
    save_roster(roster)
    print('hired ' + args.name)
    return 0


def rollover_staff(name, reason):
    roster = load_roster()
    person = next((p for p in roster if p['name'] == name), None)
    if person is None:
        print('staff unavailable')
        return 2
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    profile = staff_root() / name / 'PROFILE.md'
    with profile.open('a', encoding='utf-8') as handle:
        handle.write(f"换岗记录:{now.date().isoformat()}|{reason}|{person.get('session_id', '')[:8]}|{person.get('ctx_tokens', 0)}|{len(person.get('cards', []))}\n")
    person.update(session_id='', ctx_tokens=0, cards=[], generation=person.get('generation', 0) + 1, rolled_at=now.isoformat(timespec='seconds'))
    save_roster(roster)
    print(f"rollover {name} 代数={person['generation']} 原因={reason}")
    return 0


def rollout_usage(session_id):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', session_id or ''):
        return None
    paths = list((Path.home() / '.codex/sessions').rglob(f'rollout-*-{session_id}.jsonl'))
    if len(paths) != 1:
        return None
    total = dict(input=0, cached=0, output=0, calls=0, compactions=0)
    seen_totals = set()
    try:
        with paths[0].open(encoding='utf-8') as handle:
            for line in handle:
                event = json.loads(line)
                payload = event.get('payload', {})
                if event.get('type') == 'compacted' or payload.get('type') == 'compacted':
                    total['compactions'] += 1
                if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                    continue
                info = payload.get('info') or {}
                usage = info.get('last_token_usage')
                if not usage:
                    continue
                cumulative = info['total_token_usage']
                cumulative_values = tuple(cumulative[k] for k in ('input_tokens', 'cached_input_tokens', 'output_tokens'))
                if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in cumulative_values) or cumulative_values[1] > cumulative_values[0]:
                    return None
                if cumulative_values in seen_totals:
                    continue
                seen_totals.add(cumulative_values)
                values = [usage[k] for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')]
                if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values) or values[1] > values[0]:
                    return None
                for key, value in zip(('input', 'cached', 'output'), values):
                    total[key] += value
                total['calls'] += 1
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return total if total['calls'] else None


def roster_list():
    ctx_limit = int(os.environ.get('PM_STAFF_CTX_LIMIT', '60000'))
    max_cards = int(os.environ.get('PM_STAFF_MAX_CARDS', '3'))
    for person in load_roster()[:12]:
        cards = person.get('cards', [])
        last = cards[-1] if cards and isinstance(cards[-1], dict) else {}
        ratio = last.get('cached_ratio')
        cache = f'{ratio:.2%}' if ratio is not None else '?'
        print(f"缓存占比={cache} {person['name']} {person['role']} {person['model']}/{person['effort']} {person['trust']} 卡数={len(person['cards'])} 会话={person['session_id'][:8]} ctx_tokens={person['ctx_tokens']} 代数={person.get('generation', 0)} 预算=ctx {person['ctx_tokens']}/{ctx_limit} · 卡 {len(person['cards'])}/{max_cards}")
    return 0


def set_field(text, name, value):
    line = name + ': ' + value
    if re.search(r'^' + name + ':', text, re.M):
        return re.sub(r'^' + name + r':[^\n]*', lambda _: line, text, count=1, flags=re.M)
    return line + '\n' + text


def git_output(*args):
    result = subprocess.run(['git', '-C', str(location('PM_REPO', str(ROOT))), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


def sync_preamble(text, person):
    text = re.sub(r'(?ms)^## 续用会话同步前言\(PM 自动生成 [^\n]*\)\n.*?(?=^GOAL:)', '', text)
    cards = person.get('cards', [])
    last = cards[-1] if cards else ''
    last = last.get('id', '') if isinstance(last, dict) else last
    commit = git_output('log', '-1', '--format=%h', '--fixed-strings', '--grep=' + last) if last else ''
    changes = git_output('log', '--oneline', '-20', commit + '..HEAD') if commit else ''
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    block = (f'## 续用会话同步前言(PM 自动生成 {stamp})\nHEAD: {git_output("rev-parse", "--short", "HEAD")}\n'
             f'上一张卡: {last or "无"} 提交: {commit or "未知"}\n{changes}\n'
             '你的记忆里关于文件内容的印象可能已过时,先 `git status`、重新读要改的文件\n'
             '本卡的 CONTEXT 文件与卡正文为准,与你的旧印象冲突时以它们为准\n'
             "开工前在 evidence 目录写 ≤8 行'我的理解'\n\n")
    return re.sub(r'^GOAL:', lambda _: block + 'GOAL:', text, count=1, flags=re.M)


def staff_writeback(card, text, runs):
    name = field(text, 'STAFF')
    roster = load_roster()
    person = next ( (p for p in roster if p['name'] == name), None)
    if not person:
        return
    session = runs / (card + '.session')
    session_id = ''
    if session.is_file():
        ids = re.findall(r'session id:\s*([^\s]+)', session.read_text(encoding='utf-8'), re.I)
        if ids:
            session_id = person['session_id'] = ids[-1]
    usage = runs / 'usage.tsv'
    rows = [line.split('\t') for line in usage.read_text(encoding='utf-8').splitlines()] if usage.is_file() else []
    matches = [row for row in rows if len(row) == 5 and row[0] == card]
    if matches and matches[-1][4].isdigit():
        person['ctx_tokens'] = int(matches[-1][4])
    else:
        print('tokens 未记录')
    staff_file = runs / (card + '.staff')
    try:
        mode = json.loads(staff_file.read_text(encoding='utf-8')).get('mode', 'resume')
    except (OSError, ValueError):
        mode = 'resume'
    entry = {'id': card, 'mode': mode}
    if matches and matches[-1][4].isdigit():
        entry['tokens'] = int(matches[-1][4])
    totals = rollout_usage(session_id)
    if totals:
        earlier = [c for c in person['cards'] if (c.get('id') if isinstance(c, dict) else c) != card]
        previous = [c for c in earlier[-1:] if isinstance(c, dict) and c.get('session_id') == session_id
                    and isinstance(c.get('rollout_totals'), dict)
                    and all(isinstance(c['rollout_totals'].get(key), int) for key in totals)]
        entry.update(session_id=session_id, rollout_totals=totals)
        baseline = previous[-1]['rollout_totals'] if previous else dict.fromkeys(totals, 0)
        delta = {key: totals[key] - baseline.get(key, 0) for key in totals}
        if not previous and mode == 'resume':
            print('rollout 指标未知: 续用会话缺少上一卡基线')
        elif all(value >= 0 for value in delta.values()) and delta['cached'] <= delta['input']:
            entry.update(cached_ratio=delta['cached'] / delta['input'] if delta['input'] else None,
                         uncached_in=delta['input'] - delta['cached'], out=delta['output'], calls=delta['calls'])
            ratio = f"{entry['cached_ratio']:.2%}" if entry['cached_ratio'] is not None else '?'
            print(f"缓存占比={ratio} 未缓存输入={entry['uncached_in']} 输出={entry['out']} 调用={entry['calls']}")
        else:
            print('rollout 指标未知: 会话累计值倒退')
    else:
        print('rollout 指标未知')
    person['cards'] = [c for c in person['cards'] if (c.get('id') if isinstance(c, dict) else c) != card]
    person['cards'].append(entry)
    save_roster(roster)


def pilot():
    runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
    usage = runs / 'usage.tsv'
    rows = [line.split('\t') for line in usage.read_text(encoding='utf-8').splitlines()] if usage.is_file() else []
    latest = {r[0]: r[4] for r in rows if len(r) == 5}
    groups = [[], []]
    for path in location('PM_CARDS_DIR', str(CARDS)).glob('C*.md'):
        if not re.fullmatch(r'C[0-9]+', path.stem):
            continue
        content = path.read_text(encoding='utf-8')
        if field(content, 'ROLE') != 'builder' or field(content, 'STATUS').lower().startswith('ready'):
            continue
        groups[bool(field(content, 'STAFF'))].append((path, content))
    for label, group in zip(('新会话', '固定会话'), groups):
        count = len(group)
        done = sum(field(t, 'STATUS').startswith('DONE') for _, t in group)
        dispatch = sum(1 + len(list((runs / 'archive').glob(p.stem + '.md.*'))) for p, _ in group)
        tokens = [int(latest[p.stem]) for p, _ in group if latest.get(p.stem, '').isdigit()]
        print(f"{label} 卡数={count} DONE={done/count:.1%} 平均派发={dispatch/count:.2f} 平均 tokens={sum(tokens)/len(tokens):.0f}" if count and tokens else f"{label} 卡数={count} DONE={done/count:.1%} 平均派发={dispatch/count:.2f} 平均 tokens=?" if count else f'{label} 卡数=0 DONE=? 平均派发=? 平均 tokens=?')
    costs = {'resume': [], 'fresh': []}
    caches = {'resume': [], 'fresh': []}
    entries = {entry['id']: entry for person in load_roster() for entry in person.get('cards', [])
               if isinstance(entry, dict) and 'id' in entry}
    for group in groups:
        for path, content in group:
            marker = runs / (path.stem + '.staff')
            try:
                mode = json.loads(marker.read_text(encoding='utf-8')).get('mode')
            except (OSError, ValueError):
                mode = 'resume' if field(content, 'STAFF') else 'fresh'
            if mode not in costs:
                continue
            entry = entries.get(path.stem, {})
            if 'uncached_in' in entry and 'out' in entry:
                costs[mode].append(entry['uncached_in'] + entry['out'])
                if entry.get('cached_ratio') is not None:
                    caches[mode].append(entry['cached_ratio'])
    averages = {mode: sum(values) / len(values) if values else None for mode, values in costs.items()}
    for mode, label in (('resume', '续用卡'), ('fresh', '新会话卡')):
        average = f"{averages[mode]:.0f}" if averages[mode] is not None else '?'
        cache = f"{sum(caches[mode]) / len(caches[mode]):.2%}" if caches[mode] else '?'
        print(f'{label}: {len(costs[mode])} 张 平均未缓存输入+输出 {average} 缓存占比 {cache}')
    ratio = f"{averages['resume'] / averages['fresh']:.2f}" if averages['resume'] is not None and averages['fresh'] else '?'
    print('推广条件:固定会话组一次通过率≥新会话组+10pp 或 平均 tokens 不高于 1.2 倍,且至少各 5 张卡; 续用成本/新会话成本 = ' + ratio)
    return 0


def memory_add(name, text):
    root = location('PM_STAFF_DIR', '/home/exedev/fw/staff')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name):
        print('invalid staff name')
        return 2
    text = text.strip()
    if not text or len(text) > 200 or '\n' in text or '\r' in text:
        print('memory must be one line of at most 200 characters')
        return 2
    path = root / name / 'MEMORY.md'
    try:
        roster = json.loads((root / 'roster.json').read_text(encoding='utf-8'))
        if not isinstance(roster, list) or not any(isinstance(item, dict) and item.get('name') == name for item in roster):
            print('unknown staff member')
            return 2
        if path.parent.resolve().parent != root.resolve() or path.resolve().parent != path.parent.resolve():
            print('invalid staff memory path')
            return 2
        content = path.read_text(encoding='utf-8')
        if any(re.fullmatch(r'\d{4}-\d{2}-\d{2}\|' + re.escape(text), line) for line in content.splitlines()):
            return 0
        date = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with path.open('a', encoding='utf-8') as output:
            output.write(('\n' if content and not content.endswith('\n') else '') + f'{date}|{text}\n')
    except (OSError, ValueError):
        print('cannot read staff roster or memory')
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    hire_parser = sub.add_parser('hire')
    hire_parser.add_argument('name')
    hire_parser.add_argument('--role', required=True, choices=('builder', 'runner', 'auditor', 'observer'))
    hire_parser.add_argument('--model', required=True)
    hire_parser.add_argument('--effort', required=True)
    hire_parser.add_argument('--dry-run', action='store_true')
    sub.add_parser('roster')
    memory_parser = sub.add_parser('memory')
    memory_parser.add_argument('name')
    memory_parser.add_argument('text')
    roll = sub.add_parser('rollover')
    roll.add_argument('name')
    roll.add_argument('--reason', default='手动')
    sub.add_parser('pilot')
    daycheck = sub.add_parser('daycheck')
    daycheck.add_argument('--date', required=True)
    denial_parser = sub.add_parser('denials')
    denial_parser.add_argument('--date', default=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
                               type=lambda value: datetime.strptime(value, '%Y-%m-%d').date().isoformat())
    wait = sub.add_parser('wait')
    wait.add_argument('--ledger-event', required=True)
    wait.add_argument('--date', default=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
    wait.add_argument('--timeout', type=float, default=1700)
    wait.add_argument('--interval', type=float, default=20)
    wait.add_argument('--state-root', type=Path, default=Path.home() / 'core-data/guardian-native')
    wait_until = sub.add_parser('wait-until')
    wait_until.add_argument('time', metavar='HH:MM')
    accept_parser = sub.add_parser('accept')
    accept_parser.add_argument('card')
    accept_parser.add_argument('--dry-run', action='store_true')
    accept_parser.add_argument('--no-audit', action='store_true')
    accept_parser.add_argument('--deep', action='store_true')
    listing = sub.add_parser('testlist')
    group = listing.add_mutually_exclusive_group(required=True)
    group.add_argument('--files')
    group.add_argument('--card')
    new = sub.add_parser('newcard')
    new.add_argument('--recipe', required=True)
    new.add_argument('--tier', choices=('easy', 'normal', 'hard', 'review'))
    new.add_argument('--question')
    for option in ('title', 'serves', 'plan', 'approval', 'scope', 'real-data', 'oracle', 'accept-by', 'epic', 'depends'):
        new.add_argument('--' + option)
    goals = new.add_mutually_exclusive_group()
    goals.add_argument('--goal')
    goals.add_argument('--goal-file')
    new.add_argument('--param', action='append', default=[])
    new.add_argument('--dry-run', action='store_true')
    new.add_argument('--no-worktree', action='store_true')
    new.add_argument('--no-pack', action='store_true')
    pack_parser = sub.add_parser('pack')
    pack_parser.add_argument('card')
    pack_parser.add_argument('--out-dir')
    lesson_parser = sub.add_parser('lesson').add_subparsers(dest='lesson_command', required=True).add_parser('add')
    lesson_parser.add_argument('--role', required=True, choices=('builder', 'runner', 'auditor', 'pm'))
    for option in ('card', 'mistake', 'rule'):
        lesson_parser.add_argument('--' + option, required=True)
    score_parser = sub.add_parser('scorecard')
    score_parser.add_argument('--since', type=lambda value: datetime.strptime(value, '%Y-%m-%d').date().isoformat())
    reconcile_parser = sub.add_parser('reconcile')
    reconcile_parser.add_argument('--apply', action='store_true')
    reconcile_parser.add_argument('--limit', type=int)
    metric_parser = sub.add_parser('metrics')
    metric_parser.add_argument('--since', type=lambda value: datetime.strptime(value, '%Y-%m-%d').date().isoformat())
    cards = sub.add_parser('cards')
    cards.add_argument('--open', action='store_true')
    cards.add_argument('--limit', type=int, default=20)
    for name in ('status', 'dispatch', 'setstatus', 'wait-card'):
        command = sub.add_parser(name)
        command.add_argument('card')
        if name == 'dispatch':
            command.add_argument('--to')
            command.add_argument('--no-lint', action='store_true')
            command.add_argument('--no-pack', action='store_true')
        if name == 'setstatus':
            command.add_argument('text')
        if name == 'wait-card':
            command.add_argument('--timeout', type=float, default=1700)
            command.add_argument('--interval', type=float, default=20)
    args = parser.parse_args(argv)
    if args.command == 'daycheck':
        try:
            from examples.day_check import main as day_check_main
        except ModuleNotFoundError:
            from day_check import main as day_check_main
        return day_check_main(['--date', args.date])
    if args.command == 'denials':
        return denials(args)
    if args.command == 'memory':
        return memory_add(args.name, args.text)
    if args.command == 'hire':
        return hire(args)
    if args.command == 'rollover':
        return rollover_staff(args.name, args.reason)
    if args.command == 'roster':
        return roster_list()
    if args.command == 'pilot':
        return pilot()
    if args.command == 'lesson':
        return lesson_add(args)
    if args.command == 'scorecard':
        return scorecard(args)
    if args.command == 'pack':
        return pack(args)
    if args.command == 'reconcile':
        if args.limit is not None and args.limit < 0:
            parser.error('--limit 必须非负')
        return reconcile(args)
    if args.command == 'metrics':
        return metrics(args)
    if args.command == 'newcard':
        return newcard(args)
    if args.command == 'testlist':
        return testlist(args)
    if args.command in ('cards', 'status', 'dispatch', 'setstatus', 'wait-card'):
        return card_command(args)
    if args.command == 'accept':
        return accept(args)
    if args.command == 'wait-until':
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', args.time):
            parser.error('时间格式必须为 HH:MM')
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        hour, minute = map(int, args.time.split(':'))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        if delay > 3600:
            parser.error('等待超过 60 分钟，请分段等待')
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(30, remaining))
        print(f'{args.time} reached')
        return 0
    if args.timeout < 0 or args.interval <= 0:
        parser.error('timeout must be nonnegative and interval must be positive')
    try:
        datetime.strptime(args.date, '%Y-%m-%d')
    except ValueError:
        parser.error('date must be YYYY-MM-DD')
    ledger = args.state_root / 'state' / 'ledger' / (args.date + '.jsonl')
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with ledger.open(encoding='utf-8') as lines:
                state = latest_state(lines, args.ledger_event)
        except FileNotFoundError:
            state = None
        status = state.get('status', 'NONE') if state is not None else 'NONE'
        if status in TERMINAL:
            print(f"{args.ledger_event} {status} completed_at={state.get('completed_at', '')} last_error={str(state.get('last_error', ''))[:120]}")
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f'TIMEOUT {args.ledger_event} last_status={status}')
            return 2
        time.sleep(min(args.interval, remaining))


if __name__ == '__main__':
    raise SystemExit(main())
