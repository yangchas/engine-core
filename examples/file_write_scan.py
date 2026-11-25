"""Static scan for file-writing calls inside the production import closure (CT-12, design/31).

    python examples/file_write_scan.py            # print every write site found in the production closure
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples import affected_tests  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "src/engine_core/guardian/datasets.registry.json"
ENTRY_MODULES = ("examples.guardian_handler",)
ENTRY_PACKAGES = ("engine_core.guardian",)                  # handlers are loaded by string name
OPEN_NAMES = {"open"}
OPEN_ATTRS = {"open"}                                       # io.open, gzip.open, Path.open ...
ATTR_WRITES = {"write_text", "write_bytes", "mkdir", "touch", "makedirs", "mkstemp", "mkdtemp",
               "NamedTemporaryFile", "TemporaryDirectory", "mkfifo", "copyfile", "copy2", "copytree",
               "move", "savez", "savez_compressed", "to_csv", "to_parquet", "to_pickle", "to_excel"}
QUALIFIED_WRITES = {("os", "replace"), ("os", "rename"), ("os", "mkdir"), ("shutil", "copy"),
                    ("np", "save"), ("numpy", "save"), ("pickle", "dump")}
WRITE_MODE = re.compile(r"[wax+]")


def production_modules() -> set[str]:
    """Import closure of the guardian handler, the guardian package and the registry's fetch scripts."""
    graph = affected_tests.import_graph()
    names = set(graph)
    entries = set(ENTRY_MODULES)
    entries |= {m for m in names if any(m == p or m.startswith(p + ".") for p in ENTRY_PACKAGES)}
    texts = [REGISTRY.read_text(encoding="utf-8")]          # scripts started as subprocesses are named by path
    texts += [p.read_text(encoding="utf-8") for m in sorted(entries) if (p := _path_of(m)) is not None]
    for text in texts:
        entries |= {m.replace("/", ".")[:-3] for m in re.findall(r"examples/\w+\.py", text)}
    entries.add("examples.core_guardian")
    extended = {name: set(deps) for name, deps in graph.items()}
    for name in names:                                       # a module named in a string literal is loaded dynamically
        path = _path_of(name)
        if path is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
                extended[name].add(node.value)
    return affected_tests.import_closure(entries & names, extended)


def _path_of(module: str) -> Path | None:
    parts = module.split(".")
    base = ROOT / "src" if parts[0] == "engine_core" else ROOT
    for candidate in (base.joinpath(*parts).with_suffix(".py"), base.joinpath(*parts, "__init__.py")):
        if candidate.exists():
            return candidate
    return None


def _mode_of(call: ast.Call) -> str | None:
    node = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if node is None:
        return "r"
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "?"


def _kind(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name) and func.id in OPEN_NAMES or isinstance(func, ast.Attribute) and func.attr in OPEN_ATTRS:
        mode = _mode_of(call)
        if isinstance(func, ast.Attribute) and not call.args and not call.keywords:
            return None                                       # Path.open() with no mode = read
        if isinstance(func, ast.Attribute) and call.args and isinstance(func.value, ast.Name) is False and len(call.args) == 1:
            mode = _mode_of(ast.Call(func=func, args=[ast.Constant("x"), call.args[0]], keywords=call.keywords)) \
                if False else mode
        return "open:" + mode if mode == "?" or WRITE_MODE.search(mode) else None
    if isinstance(func, ast.Attribute):
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        if (receiver, func.attr) in QUALIFIED_WRITES:
            return f"{receiver}.{func.attr}"
        if func.attr in ATTR_WRITES:
            return func.attr
    return None


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def scan_file(path: Path) -> list[dict]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: list[dict] = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + [child.name]
            if isinstance(child, ast.Call):
                kind = _kind(child)
                if kind:
                    sites.append({"file": _rel(path), "function": ".".join(scope) or "<module>",
                                  "kind": kind, "line": child.lineno})
            visit(child, inner)

    visit(tree, [])
    return sites


def scan() -> list[dict]:
    sites: list[dict] = []
    for module in sorted(production_modules()):
        path = _path_of(module)
        if path is not None and path.relative_to(ROOT).parts[0] in ("src", "examples"):
            sites.extend(scan_file(path))
    return sites


if __name__ == "__main__":
    found = scan()
    for site in found:
        print(f'{site["file"]}:{site["line"]} {site["function"]} {site["kind"]}')
    print(f"{len(found)} write sites in {len(production_modules())} production modules", file=sys.stderr)
