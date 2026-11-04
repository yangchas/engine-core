# DATA: spec (the scan reads the repository source; the exemption list is a declared config)
"""C669 / CT-12: file writes in the production closure are allowed, or exempt with a target and an expiry."""
import json
from datetime import date
from pathlib import Path

from examples import file_write_scan

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "src/engine_core/guardian/file_write_exemptions.json"


def _config():
    return json.loads(LIST.read_text(encoding="utf-8"))


def _key(item):
    return item["file"], item["function"]


def test_every_production_write_site_is_allowed_or_exempt():
    config = _config()
    known = {_key(i) for i in config["allowed"]} | {_key(i) for i in config["exempt"]}
    found = {(s["file"], s["function"]): s for s in file_write_scan.scan()}
    new = sorted(f"{f}::{fn} ({found[(f, fn)]['kind']}, line {found[(f, fn)]['line']})"
                 for f, fn in found if (f, fn) not in known)
    assert not new, "new file-writing code in the production closure (store in Redis/TD, or register it):\n" + "\n".join(new)


def test_no_stale_entries():
    found = {(s["file"], s["function"]) for s in file_write_scan.scan()}
    config = _config()
    stale = sorted(f"{i['file']}::{i['function']}" for i in config["allowed"] + config["exempt"] if _key(i) not in found)
    assert not stale, "entries without a matching write site (remove them):\n" + "\n".join(stale)


def test_exemptions_are_complete_and_not_expired():
    today = date.today().isoformat()
    for item in _config()["exempt"]:
        assert all(item.get(k) for k in ("ref", "file", "function", "reason", "target", "expires")), item
        assert item["expires"] >= today, f"exemption expired: {item['file']}::{item['function']} ({item['expires']})"
    for item in _config()["allowed"]:
        assert all(item.get(k) for k in ("file", "function", "reason")), item


def test_scanner_recognises_write_forms(tmp_path):
    import textwrap

    code = textwrap.dedent('''
        import os, tempfile, gzip
        def a(p): open(p, "w")
        def b(p): p.write_text("x")
        def c(p): os.replace("a", "b")
        def d(p): gzip.open(p, "wt")
        def e(p): tempfile.mkstemp()
        def f(p): open(p)
        def g(p): open(p, "rb")
        def h(s): s.replace("a", "b")
    ''')
    path = tmp_path / "probe.py"
    path.write_text(code, encoding="utf-8")
    kinds = {s["function"]: s["kind"] for s in file_write_scan.scan_file(path)}
    assert set(kinds) == {"a", "b", "c", "d", "e"}, kinds
