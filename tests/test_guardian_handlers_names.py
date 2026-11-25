"""Guard against NameError in guardian handler modules (C367: core_guardian.py missed a SimpleNamespace import)."""
import ast
import builtins
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "examples/core_guardian.py", ROOT / "examples/guardian_handler.py",
         *sorted((ROOT / "src/engine_core/guardian").glob("*.py"))]


def _undefined(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = set(dir(builtins)) | {"__file__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    return sorted({(n.id, n.lineno) for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound})


def test_guardian_modules_have_no_undefined_names():
    bad = {p.name: _undefined(p) for p in FILES if _undefined(p)}
    assert not bad
