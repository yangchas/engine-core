#!/usr/bin/env python3
"""Mechanically audit a builder card against a committed branch."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def check_scope(changed_files: list[str], scope_write: list[str], worktree: str) -> list[str]:
    root = Path(worktree).resolve()
    allowed = [Path(p).resolve() for p in scope_write]
    return [name for name in changed_files if not any((root / name).resolve() == p or p in (root / name).resolve().parents for p in allowed)]


def count_new_tests(base_text_by_file: dict[str, str], branch_text_by_file: dict[str, str]) -> int:
    def names(text: str) -> set[str]:
        tree = ast.parse(text)
        return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')}
    return sum(len(names(text) - names(base_text_by_file.get(path, ''))) for path, text in branch_text_by_file.items() if path.startswith('tests/') and path.endswith('.py'))


def verdict(problems: list[str]) -> dict:
    return {'verdict': 'FAIL' if problems else 'PASS', 'problems': problems}


def _git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], check=check, capture_output=True, text=True)


def _test_env():
    env = os.environ.copy()
    env.pop('TD_CONFIG', None)
    env.pop('REDIS_URL', None)
    env.update(TMPDIR='/tmp', PYTHONPATH='src:.', PYTHONDONTWRITEBYTECODE='1')
    return env


def _pytest(directory, test_files, python):
    return subprocess.run([python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *test_files], cwd=directory, env=_test_env(), capture_output=True, text=True)


def collect_go_tests(repo, base, branch, files):
    packages = {}
    for path in files:
        if not path.startswith('native/') or not path.endswith('_test.go') or '..' in Path(path).parts:
            continue
        diff = _git(repo, 'diff', '--unified=0', f'{base}...{branch}', '--', path).stdout
        names = re.findall(r'^\+\s*func\s+(Test[A-Za-z0-9_]*)\s*\(', diff, re.MULTILINE)
        if names:
            packages.setdefault(str(Path(path).parent), set()).update(names)
    return {pkg: sorted(names) for pkg, names in packages.items()}


def _go_test(directory, package, names):
    pattern = '^(' + '|'.join(re.escape(name) for name in names) + ')$'
    root = Path(directory)
    pkg = root / package
    module = next((p for p in (pkg, *pkg.parents) if p == root or (p / 'go.mod').is_file()), root)
    relative = pkg.relative_to(module).as_posix()
    target = './' if relative == '.' else './' + relative
    return subprocess.run(['go', 'test', '-count=1', '-run', pattern, target], cwd=module, env=_test_env(), capture_output=True, text=True, timeout=300)


def fail_first(repo, base, branch, test_files, python, go_tests=None) -> tuple[bool, str]:
    go_tests = go_tests or {}
    if (not test_files and not go_tests) or any(not p.startswith('tests/') or '..' in Path(p).parts for p in test_files):
        raise ValueError('test files must be nonempty paths under tests/')
    tmp = tempfile.mkdtemp(prefix='audit-card-')
    added = False
    try:
        _git(repo, 'worktree', 'add', '--detach', tmp, base)
        added = True
        go_files = _git(repo, 'diff', '--name-only', f'{base}...{branch}').stdout.splitlines() if go_tests else []
        go_files = [p for p in go_files if p.startswith('native/') and p.endswith('_test.go') and str(Path(p).parent) in go_tests and _git(repo, 'cat-file', '-e', f'{branch}:{p}', check=False).returncode == 0]
        for path in [*test_files, *go_files]:
            content = _git(repo, 'show', f'{branch}:{path}').stdout
            target = Path(tmp) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        outputs = []
        failed = True
        if test_files:
            result = _pytest(tmp, test_files, python)
            failed = result.returncode != 0
            outputs.append('\n'.join((result.stdout + result.stderr).splitlines()[-20:]))
        for package, names in go_tests.items():
            result = _go_test(tmp, package, names)
            output = result.stdout + result.stderr
            outputs.append('\n'.join(output.splitlines()[-20:]))
            if result.returncode == 0:
                failed = False
                outputs.append(f'{package}: tests did not fail on base')
            elif 'FAIL' not in output:
                failed = False
                outputs.append(f'{package}: go test failed without FAIL output')
        return failed, '\n'.join(outputs)
    finally:
        if added:
            _git(repo, 'worktree', 'remove', '--force', tmp, check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--card', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--base', default='codex/feature-session-engine-integration')
    parser.add_argument('--repo', default='/home/exedev/repos/engine_core')
    parser.add_argument('--cards-dir', default='/home/exedev/fw/cards')
    parser.add_argument('--python', default='/home/exedev/services/engine-next/shared/venv/bin/python')
    args = parser.parse_args()
    problems = []
    output = ''
    try:
        if not re.fullmatch(r'C\d+', args.card):
            raise ValueError('invalid card ID')
        header = {}
        for line in (Path(args.cards_dir) / f'{args.card}.md').read_text(encoding='utf-8').splitlines():
            match = re.match(r'^(ROLE|SCOPE_WRITE|MAX_NEW_TESTS):\s*(.*)$', line)
            if match:
                header.setdefault(match.group(1), match.group(2).strip())
        if not header.get('ROLE', '').lower().startswith('builder'):
            problems.append('ROLE is not builder')
        scope = [p.strip() for p in header['SCOPE_WRITE'].split(',') if p.strip()]
        if not scope or not all(Path(p).is_absolute() for p in scope):
            raise ValueError('SCOPE_WRITE must contain absolute paths')
        maximum = int(header['MAX_NEW_TESTS'])
        if maximum < 0:
            raise ValueError('MAX_NEW_TESTS must be nonnegative')
        worktrees = _git(args.repo, 'worktree', 'list', '--porcelain').stdout.split('\n\n')
        roots = [block.splitlines()[0][9:] for block in worktrees if f'branch refs/heads/{args.branch}' in block.splitlines()]
        if len(roots) != 1:
            raise ValueError('branch must have exactly one checked-out worktree')
        files = _git(args.repo, 'diff', '--name-only', '-z', f'{args.base}...{args.branch}').stdout.split('\0')
        files = [p for p in files if p]
        problems.extend(f'outside scope: {p}' for p in check_scope(files, scope, roots[0]))
        test_files = [p for p in files if p.startswith('tests/') and p.endswith('.py') and _git(args.repo, 'cat-file', '-e', f'{args.branch}:{p}', check=False).returncode == 0]
        go_tests = collect_go_tests(args.repo, args.base, args.branch, files)
        def contents(ref):
            result = {}
            for p in test_files:
                item = _git(args.repo, 'show', f'{ref}:{p}', check=False)
                if item.returncode == 0:
                    result[p] = item.stdout
            return result
        count = count_new_tests(contents(args.base), contents(args.branch)) + sum(map(len, go_tests.values()))
        if count > maximum:
            problems.append(f'new tests {count} exceed maximum {maximum}')
        failed, output = fail_first(args.repo, args.base, args.branch, test_files, args.python, go_tests)
        if not failed:
            problems.append('tests did not fail on base')
        tmp = tempfile.mkdtemp(prefix='audit-card-branch-')
        added = False
        try:
            _git(args.repo, 'worktree', 'add', '--detach', tmp, args.branch)
            added = True
            if test_files:
                result = _pytest(tmp, test_files, args.python)
                if result.returncode != 0:
                    problems.append('tests failed on branch: ' + '\n'.join((result.stdout + result.stderr).splitlines()[-20:]))
            for package, names in go_tests.items():
                result = _go_test(tmp, package, names)
                if result.returncode != 0:
                    problems.append('tests failed on branch: ' + '\n'.join((result.stdout + result.stderr).splitlines()[-20:]))
        finally:
            if added:
                _git(args.repo, 'worktree', 'remove', '--force', tmp, check=False)
            shutil.rmtree(tmp, ignore_errors=True)
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.SubprocessError) as exc:
        problems.append(str(exc))
    result = verdict(problems)
    result['fail_first_output'] = output
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['verdict'] == 'PASS' else 3


if __name__ == '__main__':
    raise SystemExit(main())
