#!/usr/bin/env python3
"""Read-only audit of tests and fixture changes in a Git revision range."""
import argparse
import ast
import difflib
import json
import re
import subprocess
from pathlib import Path

FIELDS = {'px_milli', 'bp1_milli', 'ap1_milli', 'bv1', 'bv2', 'av1', 'av2',
          'amt_yuan', 'price_milli', 'prev_close_milli', 'rest_bid', 'rest_ask', 'seal_bid'}


def git(repo, *args, missing_ok=False):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    if result.returncode and not missing_ok:
        raise RuntimeError(result.stderr.strip() or 'git command failed')
    return result.stdout if result.returncode == 0 else ''


def source(repo, rev, path):
    return git(repo, 'show', f'{rev}:{path}', missing_ok=True)


def parsed(text):
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def test_names(tree):
    names = set()
    def visit(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = parents + (child.name,)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith('test_'):
                    names.add(name)
                visit(child, name)
            else:
                visit(child, parents)
    if tree:
        visit(tree)
    return names


def literal_fields(node):
    return {item.value for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
            and any(item.value == field or item.value.startswith(field + '_') for field in FIELDS)}


def audit(repo, base, card_text, evidence=None):
    changes = git(repo, 'diff', '--name-status', '--no-renames', base, 'HEAD', '--', 'tests/').splitlines()
    count = 0
    warnings = []
    provenance_text = source(repo, 'HEAD', 'tests/fixtures/real/PROVENANCE.json')
    try:
        provenance = json.loads(provenance_text)
    except ValueError:
        provenance = {}
    def registered(obj, path):
        if isinstance(obj, dict):
            return path in obj or any(registered(v, path) for v in obj.values())
        if isinstance(obj, list):
            return any(registered(v, path) for v in obj)
        return isinstance(obj, str) and obj in (path, 'tests/fixtures/real/' + path)
    scope_match = re.search(r'^SCOPE_WRITE:\s*(.*)$', card_text, re.M)
    scope = scope_match.group(1).split(',') if scope_match else []
    stubs_match = re.search(r'^STUBS_ALLOWED:\s*(.*)$', card_text, re.M)
    stubs = stubs_match.group(1) if stubs_match else ''
    for change in changes:
        status, path = change.split('\t', 1)
        if status == 'D':
            continue
        if not path.endswith('.py'):
            if status != 'A':
                continue
            relative = path.removeprefix('tests/fixtures/real/')
            allowed_real = path.startswith('tests/fixtures/real/') and (relative == 'PROVENANCE.json' or registered(provenance, relative))
            scoped = [item.strip().split('/tests/', 1)[-1].rstrip('/') for item in scope if '/tests/fixtures/' in item]
            allowed_scope = any(path == 'tests/' + item or path.startswith('tests/' + item + '/') for item in scoped)
            if not (allowed_real or allowed_scope):
                warnings.append(f'WARN unlisted_fixture: {path}')
            continue
        old = source(repo, base, path)
        new = source(repo, 'HEAD', path)
        old_tree, new_tree = parsed(old), parsed(new)
        if new_tree is None:
            warnings.append(f'WARN audit_parse_error: {path}')
            continue
        count += len(test_names(new_tree) - test_names(old_tree))
        if re.search(r'(?<![\w/.-])' + re.escape(path) + r'(?![\w/.-])', stubs):
            continue
        old_lines, new_lines = old.splitlines(), new.splitlines()
        added = set()
        for tag, _, _, start, end in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
            if tag in ('insert', 'replace'):
                added.update(range(start + 1, end + 1))
        imports = '\n'.join(ast.get_source_segment(new, node) or '' for node in new_tree.body if isinstance(node, (ast.Import, ast.ImportFrom)))
        for node in ast.walk(new_tree):
            if isinstance(node, (ast.Dict, ast.List)) and len(literal_fields(node)) >= 3 and any(node.lineno <= line <= node.end_lineno for line in added):
                owners = [parent for parent in ast.walk(new_tree) if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and parent.lineno <= node.lineno <= parent.end_lineno]
                owner = min(owners, key=lambda parent: parent.end_lineno - parent.lineno) if owners else None
                context = (ast.get_source_segment(new, owner) or '') if owner else '\n'.join(new_lines[i - 1] for i in sorted(added))
                if 'fixtures' not in imports and 'fixtures' not in context:
                    warnings.append(f'WARN handwritten_market_rows: {path}')
                    break
    result = [f'NOTE new_tests: {count}']
    budget = re.search(r'^TEST_BUDGET:.*?新增测试\s*≤\s*(\d+)', card_text, re.M) or re.search(r'^MAX_NEW_TESTS:\s*(\d+)', card_text, re.M)
    if budget and count > int(budget.group(1)):
        result.append(f'FAIL test_budget_exceeded: {count} > {budget.group(1)}')
    result.extend(warnings)
    card = re.search(r'^ID:\s*(C\d+)\b', card_text, re.M)
    if card or evidence is not None:
        card_id = card.group(1) if card else ''
        evidence_dir = Path(evidence) if evidence is not None else Path('/home/exedev/fw/evidence') / card_id
        has_fail = evidence_dir.is_dir() and any('fail' in p.name.lower() for p in evidence_dir.rglob('*') if p.is_file())
        report = Path('/home/exedev/fw/reports') / (card_id + '.md')
        report_text = report.read_text(encoding='utf-8', errors='replace') if card_id and report.is_file() else ''
        if not has_fail and not re.search(r'修复前失败|fail-first|ImportError|ModuleNotFoundError', report_text, re.I):
            result.append('WARN no_fail_first_evidence: no failure evidence found')
    # Keep failures visible even if many fixtures produce warnings.
    result.sort(key=lambda line: (not line.startswith('FAIL '), not line.startswith('WARN ')))
    return result[:12]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--card', type=Path)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    try:
        lines = audit(args.repo, args.base, args.card.read_text(encoding='utf-8') if args.card else '', args.evidence)
    except (OSError, RuntimeError) as exc:
        print(f'FAIL audit_error: {exc}')
        return 1
    print('\n'.join(lines))
    return int(any(line.startswith('FAIL ') for line in lines))


if __name__ == '__main__':
    raise SystemExit(main())
