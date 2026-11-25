"""Changed files -> the tests that can be affected (import graph + fixture references).

    python examples/affected_tests.py <git range>        # e.g. main..HEAD, HEAD~1
    python examples/affected_tests.py --files a.py b.json
    python examples/affected_tests.py <range> --command  # print a pytest command line
    (default follows imports 2 hops; --full follows them all the way, --depth 1 = direct importers only)

Also exposes ``import_closure`` (forward closure from entry modules) for the static checks.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "src", ROOT)          # src/engine_core.x -> engine_core.x ; examples/x.py -> examples.x ; tests/x.py -> tests.x
SCANNED = ("src", "examples", "tests")
DIRECTORY_TOKENS = ("dataset_specs",)      # files in these directories are read by directory, not by file name
ALL_TESTS_TRIGGERS = ("tests/conftest.py", "pyproject.toml", "pytest.ini", "setup.cfg", "tests/replay.py",
                      "tests/memory_redis.py")


def _py_files() -> list[Path]:
    return sorted(p for top in SCANNED for p in (ROOT / top).rglob("*.py") if "egg-info" not in p.parts)


def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path, names: set[str]) -> set[str]:
    """Project modules imported by ``path`` (``from a import b`` counts b when a.b is a module)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    own = module_name(path)
    package = own if path.name == "__init__.py" else own.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)] if package else []
                base = ".".join([*anchor, base] if base else anchor)
            candidates = [base] + [f"{base}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in candidates:
            parts = name.split(".")
            for i in range(len(parts), 0, -1):          # `import a.b.c` also loads a.b and a
                prefix = ".".join(parts[:i])
                if prefix in names:
                    found.add(prefix)
    found.discard(own)
    return found


_GRAPH: dict[str, set[str]] | None = None


def import_graph() -> dict[str, set[str]]:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


def _build_graph() -> dict[str, set[str]]:
    files = _py_files()
    names = {module_name(p) for p in files}
    return {module_name(p): _imports(p, names) for p in files}


def import_closure(entries, graph: dict[str, set[str]] | None = None) -> set[str]:
    """Every project module reachable from ``entries`` by imports (the entries included)."""
    graph = graph if graph is not None else import_graph()
    seen: set[str] = set()
    stack = [e for e in entries if e in graph]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(graph.get(name, ()))
    return seen


def _walk(start, reverse, depth):
    """Modules reaching ``start`` through at most ``depth`` reverse-import hops (None = unlimited)."""
    seen = {name: 0 for name in start}
    frontier = list(start)
    while frontier and (depth is None or min(seen[n] for n in frontier) < depth):
        nxt = []
        for name in frontier:
            if depth is not None and seen[name] >= depth:
                continue
            for parent in reverse.get(name, ()):
                if parent not in seen:
                    seen[parent] = seen[name] + 1
                    nxt.append(parent)
        frontier = nxt
    return set(seen)


def affected_tests(changed: list[str], depth: int | None = None) -> list[str]:
    """Repo-relative test files affected by ``changed`` (repo-relative paths).

    ``depth`` limits the reverse-import hops (1 = tests importing a changed module directly);
    the default follows imports all the way and is the conservative, complete answer."""
    changed = [c.replace("\\", "/") for c in changed]
    files = _py_files()
    by_module = {module_name(p): p for p in files}
    tests = {p: module_name(p) for p in files if p.relative_to(ROOT).parts[0] == "tests"
             and p.name.startswith("test_")}
    if any(c in ALL_TESTS_TRIGGERS for c in changed):
        return sorted(str(p.relative_to(ROOT)) for p in tests)
    graph = import_graph()
    reverse: dict[str, set[str]] = {}
    for module, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(module)
    start: set[str] = set()
    fixtures: list[str] = []
    for rel in changed:
        path = ROOT / rel
        if rel.endswith(".py"):
            name = module_name(path)
            if name in graph:
                start.add(name)
            if "_" in path.stem and len(path.stem) >= 10:
                fixtures.append(path.stem)               # scripts/modules a test runs or imports by name, not by import
        elif rel.startswith("tests/fixtures/"):
            fixtures.append(rel)
        else:
            fixtures.append(rel)                          # config/data files (json, go testdata, ...) referenced by text
            if path.parent.name in DIRECTORY_TOKENS:
                fixtures.append(path.parent.name)
    affected = _walk(start, reverse, depth)
    selected = {p for p, name in tests.items() if name in affected}
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in tests}
    # a test that reads a data file, or any module that does, names it by relative path or basename
    readers = {p for p, text in texts.items() for rel in fixtures if rel in text or Path(rel).name in text}
    selected |= readers
    reader_modules = {module_name(p) for p in files if p not in tests and any(
        (rel in (t := p.read_text(encoding="utf-8", errors="replace")) or Path(rel).name in t) for rel in fixtures)}
    seen = _walk(reader_modules, reverse, depth)
    selected |= {p for p, name in tests.items() if name in seen}
    return sorted(str(p.relative_to(ROOT)) for p in selected)


def changed_files(git_range: str) -> list[str]:
    spec = [git_range] if ".." in git_range else [f"{git_range}~1", git_range] if git_range != "HEAD" else ["HEAD~1", "HEAD"]
    out = subprocess.run(["git", "diff", "--name-only", *spec], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("range", nargs="?", help="git range or commit")
    parser.add_argument("--files", nargs="*", help="explicit changed files instead of a git range")
    parser.add_argument("--depth", type=int, default=2, help="reverse-import hops (default 2; 1 = direct importers only)")
    parser.add_argument("--full", action="store_true", help="follow imports all the way (complete but broad)")
    parser.add_argument("--command", action="store_true", help="print a pytest command line")
    args = parser.parse_args(argv)
    if not args.files and not args.range:
        parser.error("give a git range or --files")
    selected = affected_tests(args.files if args.files else changed_files(args.range), depth=None if args.full else args.depth)
    if args.command:
        print("python -m pytest -q " + " ".join(selected) if selected else "# no affected tests")
    else:
        print("\n".join(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
