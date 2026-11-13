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
        print(f"card {card} not found; closest: {', '.join(difflib.get_close_matches(card, choices, n=3, cutoff=0))}")
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


def show_status(card, path):
    text = path.read_text(encoding='utf-8')
    for name in ('STATUS', 'ROLE', 'EXECUTOR', 'TITLE'):
        print(f'{name}: {field(text, name)[:200]}')
    report = location('PM_REPORTS_DIR', '/home/exedev/fw/reports') / (card + '.md')
    if report.is_file():
        with report.open(encoding='utf-8', errors='replace') as f:
            first = f.readline().strip()[:160]
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
                print(line.rstrip()[:160])
    else:
        print('log: none')
    processes = card_processes(card)
    print(f'process: running {max(processes)}s' if processes else 'process: exited')
    worktree = worktree_from_card(text)
    if worktree:
        for label, command in [('git log', ['log', '-1', '--oneline']), ('git status', ['status', '--short'])]:
            result = subprocess.run(['git', '-C', worktree, *command], capture_output=True, text=True)
            lines = result.stdout.splitlines()[:1 if label == 'git log' else 5]
            print(label + ': ' + (' | '.join(line[:160] for line in lines) or '(empty or unavailable)'))
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
                print(line)
            if not actionable:
                print('audit: ok')
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f'audit: unavailable ({str(exc)[:120]})')
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
    entries += [('GOAL', understanding + '先读 CONTEXT 文件。\n' + values['goal'])] + [(k, fixed[k]) for k in ('NOT_DO', 'REPORT', 'ACCEPT')]
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
            runs = location('PM_RUNS_DIR', '/home/exedev/fw/runs')
            runs.mkdir(parents=True, exist_ok=True)
            (runs / (args.card + '.dispatch.timestamp')).write_text(str(time.time_ns()), encoding='utf-8')
            with (runs / (args.card + '.dispatch.out')).open('ab') as out:
                process = subprocess.Popen([os.environ.get('PM_RUNCARD', '/home/exedev/fw/bin/runcard'), args.card],
                                           stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
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
        category = next((key for key in categories if status.startswith(key)), '其他')
        counts[category] += 1
        stale += category == 'ready' and report.is_file()
        if dispatched:
            rows.append((path.stem, len(archives) + 1, status))
    rows.sort(key=lambda row: (-row[1], int(row[0][1:])))
    average = sum(row[1] for row in rows) / len(rows) if rows else 0
    rate = sum(count == 1 and status.startswith('DONE') for _, count, status in rows) / len(rows) if rows else 0
    print(f'卡总数: {total} | 已派发: {len(rows)}')
    print('状态: ' + ' '.join(f'{key}={counts[key]}' for key in counts))
    print(f'陈旧 ready: {stale}')
    print('派发最多: ' + (' '.join(f'{card}:{count}' for card, count, _ in rows[:3]) or '无'))
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
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
    metric_parser = sub.add_parser('metrics')
    metric_parser.add_argument('--since', type=lambda value: datetime.strptime(value, '%Y-%m-%d').date().isoformat())
    cards = sub.add_parser('cards')
    cards.add_argument('--open', action='store_true')
    cards.add_argument('--limit', type=int, default=20)
    for name in ('status', 'dispatch', 'setstatus', 'wait-card'):
        command = sub.add_parser(name)
        command.add_argument('card')
        if name == 'dispatch':
            command.add_argument('--no-lint', action='store_true')
            command.add_argument('--no-pack', action='store_true')
        if name == 'setstatus':
            command.add_argument('text')
        if name == 'wait-card':
            command.add_argument('--timeout', type=float, default=1700)
            command.add_argument('--interval', type=float, default=20)
    args = parser.parse_args(argv)
    if args.command == 'lesson':
        return lesson_add(args)
    if args.command == 'scorecard':
        return scorecard(args)
    if args.command == 'pack':
        return pack(args)
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
