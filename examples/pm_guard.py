#!/usr/bin/env python3
"""PreToolUse policy for the PM session."""
import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import time


READ = set('ls cat head tail grep rg wc find stat date echo test true'.split())
FORBIDDEN = set('redis-cli taos curl wget ssh scp kill pkill rm mv cp tee dd chmod chown sudo systemctl docker'.split())
GIT = set('log diff status show rev-parse branch merge-base'.split())
FW_BIN = set('runcard preflight_card card_callback ops_state clean_rh pm'.split())
SCRIPTS = set('release.py audit_card.py deploy_watch.py pm.py'.split())
ASSIGN = re.compile(r'^[a-zA-Z_][a-zA-Z_0-9]*=')


def _path(path, cwd):
    return os.path.normpath(path if path.startswith('/') else os.path.join(cwd, path))


def _under(path, root):
    return path.startswith(root + '/')


def _output_allowed(path, env):
    if path == '/dev/null':
        return True
    if path.startswith('$CLAUDE_JOB_DIR/tmp/') and '..' not in PurePosixPath(path).parts:
        return True
    job = env.get('CLAUDE_JOB_DIR', '')
    if job and path.startswith('${CLAUDE_JOB_DIR}/tmp/'):
        path = job + path[len('${CLAUDE_JOB_DIR}'):]
    if job and path.startswith('$CLAUDE_JOB_DIR/tmp/'):
        path = job + path[len('$CLAUDE_JOB_DIR'):]
    path = os.path.normpath(path)
    return bool(re.fullmatch(r'/home/exedev/\.claude/jobs/[^/]+/tmp/.+', path))


def _bash(command, env):
    if not isinstance(command, str):
        return False, 'parse: command must be a string'
    if '`' in command or '$(' in command or '<(' in command or '>(' in command:
        return False, 'shell_expansion: command substitution is forbidden'
    # Heredoc bodies are data, never shell commands. Keep them for script checks.
    lines = command.splitlines()
    headers, bodies = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        headers.append(line)
        matches = list(re.finditer(r'<<-?\s*[\'"]?([A-Za-z_][A-Za-z_0-9]*)[\'"]?', line))
        for match in matches:
            end = match.group(1)
            body = []
            i += 1
            while i < len(lines) and lines[i].strip() != end:
                body.append(lines[i])
                i += 1
            if i == len(lines):
                return False, 'parse: unterminated heredoc'
            bodies.extend(body)
        i += 1
    try:
        lexer = shlex.shlex('\n'.join(headers), posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        lexer.commenters = ''
        lexer.whitespace = ' \t\r'
        tokens = list(lexer)
    except ValueError as exc:
        return False, f'parse: {exc}'
    segments, segment = [], []
    for token in tokens:
        if token in (';', '&&', '||', '|', '\n'):
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    if not segments:
        return False, 'parse: empty command'
    cwd = '/home/exedev/repos/engine_core'
    for words in segments:
        if any(ASSIGN.match(w) and w.split('=', 1)[0] in ('TD_CONFIG', 'REDIS_URL') for w in words):
            return False, 'protected_assignment: TD_CONFIG or REDIS_URL'
        if all(ASSIGN.match(w) for w in words):
            continue
        args = []
        i = 0
        while i < len(words):
            word = words[i]
            if word in ('>', '>>', '>&', '<', '<<', '<<-'):
                if i + 1 >= len(words):
                    return False, 'parse: missing redirection target'
                target = words[i + 1]
                fd = args.pop() if args and args[-1].isdigit() else ''
                if word == '>&':
                    if (fd, target) != ('2', '1'):
                        return False, 'redirection: descriptor not allowed'
                elif word in ('>', '>>') and not _output_allowed(target, env):
                    return False, 'redirection: output target not allowed'
                elif word == '<':
                    return False, 'redirection: input target not allowed'
                i += 2
                continue
            if word in ('&', '(', ')', ';;', ';&', ';;&', '|||', '&&&'):
                return False, 'parse: unsupported shell operator'
            args.append(word)
            i += 1
        while args and ASSIGN.match(args[0]):
            args.pop(0)
        unset_vars = set()
        while args and args[0] in ('env', 'timeout'):
            wrapper = args.pop(0)
            if wrapper == 'timeout':
                if not args or not re.fullmatch(r'\d+(?:\.\d+)?[smhd]?', args.pop(0)):
                    return False, 'wrapper: invalid timeout'
            else:
                while args and (args[0] == '-u' or ASSIGN.match(args[0])):
                    if args[0] == '-u':
                        args.pop(0)
                        if not args or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', args[0]):
                            return False, 'wrapper: invalid env unset'
                        unset_vars.add(args.pop(0))
                    else:
                        args.pop(0)
        if not args:
            return False, 'parse: missing command'
        cmd, rest = args[0], args[1:]
        name = os.path.basename(cmd)
        if name in FORBIDDEN:
            return False, f'forbidden_command: {name}'
        if name == 'cd' and cmd == 'cd':
            if len(rest) != 1 or '$' in rest[0]:
                return False, 'cd: invalid directory'
            cwd = _path(rest[0], cwd)
        elif cmd in READ:
            # find and similar tools may execute commands or write files.
            if cmd == 'find' and any(x in rest for x in ('-exec', '-execdir', '-ok', '-okdir', '-delete', '-fprint', '-fprint0', '-fprintf', '-fls')):
                return False, 'find_action: unsafe action'
        elif cmd in ('cut', 'sort', 'uniq', 'tr'):
            if cmd == 'sort' and any((x.startswith('-') and not x.startswith('--') and 'o' in x[1:]) or x.startswith('--o') or x.startswith('--c') for x in rest):
                return False, 'sort: output or execution not allowed'
            if cmd == 'uniq' and ('--' in rest or sum(not x.startswith('-') for x in rest) > 1):
                return False, 'uniq: output not allowed'
        elif cmd == 'sed':
            if any(x.startswith('-i') or x.startswith('--in-place') for x in rest):
                return False, 'sed_in_place: forbidden'
            if '-n' not in rest or any(x in rest for x in ('-e', '--expression', '-f', '--file')):
                return False, 'sed: only simple -n allowed'
            if (len(rest) < 2 or rest[0] != '-n'
                    or not re.fullmatch(r'(?:[0-9]+(?:,(?:[0-9]+|\$))?p|/[^/\n]*/p)', rest[1])
                    or any(x.startswith('-') for x in rest[2:])):
                return False, 'sed: unsafe script'
        elif cmd == 'git':
            gitargs = rest[:]
            if len(gitargs) >= 2 and gitargs[0] == '-C':
                gitargs = gitargs[2:]
            if not gitargs or not (gitargs[0] in GIT or (len(gitargs) >= 2 and gitargs[:2] in (['worktree', 'list'], ['worktree', 'add']))):
                return False, 'git_command: not allowed'
            if gitargs[0] == 'branch' and any(x in gitargs for x in ('-d', '-D', '-m', '-M', '-c', '-C', '--delete', '--move', '--copy')):
                return False, 'git_branch: mutation not allowed'
        elif cmd == 'mkdir':
            paths = [x for x in rest if x != '-p']
            if '-p' not in rest or not paths or any(not (_under(_path(x, cwd), '/home/exedev/fw') or _under(_path(x, cwd), '/home/exedev/validation')) or x.startswith('-') for x in paths):
                return False, 'mkdir: target not allowed'
        elif name in ('python', 'python3') and (cmd == 'python3' or (cmd.startswith('/') and '/bin/python' in cmd)):
            if not rest:
                return False, 'python_script: missing script'
            if (cmd in ('python3', '/home/exedev/services/engine-next/shared/venv/bin/python')
                    and rest[:2] == ['-m', 'pytest']
                    and {'TD_CONFIG', 'REDIS_URL'} <= unset_vars
                    and not any(x == '--live' or x.startswith('--live=') for x in rest[2:])):
                continue
            script = rest[0]
            if script in ('-c', '-'):
                text = ' '.join(rest[1:] + bodies)
                if re.search(r'TD_CONFIG|REDIS_URL|taos|redis|connect_td|engine_core', text, re.I):
                    return False, 'python_data_access: forbidden script text'
                return False, 'python_script: inline scripts not allowed'
            resolved = _path(script, cwd)
            if resolved not in ('/home/exedev/fw/bin/ops_state', '/home/exedev/fw/bin/card_callback') and not (resolved.startswith('/home/exedev/repos/engine_core/examples/') and os.path.basename(resolved) in SCRIPTS and os.path.dirname(resolved) == '/home/exedev/repos/engine_core/examples'):
                return False, 'python_script: script not allowed'
        elif _path(cmd, cwd) in {f'/home/exedev/fw/bin/{x}' for x in FW_BIN}:
            pass
        else:
            return False, f'default_deny: {cmd}'
    return True, 'allowlist'


def decide(tool_name: str, tool_input: dict, permit_fresh: bool, env: dict | None = None) -> tuple[bool, str]:
    if permit_fresh:
        return True, 'inline_permit'
    if not isinstance(tool_input, dict):
        return False, 'input: invalid tool input'
    if tool_name == 'Bash':
        return _bash(tool_input.get('command'), env or {})
    if tool_name in ('Edit', 'Write'):
        path = tool_input.get('file_path')
        if not isinstance(path, str) or not path.startswith('/'):
            return False, 'file_path: invalid path'
        path = os.path.normpath(path)
        roots = ('/home/exedev/fw/cards', '/home/exedev/fw/design', '/home/exedev/fw/retros', '/home/exedev/fw/evidence')
        if any(_under(path, root) for root in roots) and Path(path).suffix in ('.md', '.json', '.txt'):
            return True, 'file_allowlist'
        if os.path.dirname(path) == '/home/exedev/fw' and path.endswith('.md'):
            return True, 'file_allowlist'
        if _under(path, '/home/exedev/.claude/projects/-home-exedev-repos/memory'):
            return True, 'file_allowlist'
        return False, 'file_path: not allowed'
    return False, 'tool: not allowed'


def main():
    fw = Path.home() / 'fw'
    permit = Path(os.environ.get('PM_GUARD_PERMIT', str(fw / '.inline_ok')))
    log = Path(os.environ.get('PM_GUARD_LOG', str(fw / 'retros' / 'guard.log')))
    try:
        age = time.time() - permit.stat().st_mtime
        fresh = 0 <= age <= 600
    except OSError:
        fresh = False
    try:
        payload = json.load(sys.stdin)
        tool = payload['tool_name']
        data = payload['tool_input']
        allowed, reason = decide(tool, data, fresh, dict(os.environ))
        detail = data.get('command' if tool == 'Bash' else 'file_path', '') if isinstance(data, dict) else ''
    except (ValueError, KeyError, TypeError) as exc:
        tool, detail, allowed, reason = 'unknown', '', False, f'input: {exc}'
    if fresh or not allowed:
        stamp = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        fields = (stamp, 'PERMIT' if fresh and allowed else 'DENY', str(tool), reason, str(detail)[:120])
        with log.open('a', encoding='utf-8') as handle:
            handle.write('\t'.join(x.replace('\t', ' ').replace('\n', ' ').replace('\r', ' ') for x in fields) + '\n')
    if not allowed:
        print(f'守卫拒绝: {reason}。请改用 pm 命令或写任务卡(cards/_TEMPLATE.md → bin/runcard);确属 ≤5 行小修再 ops_state --inline', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
