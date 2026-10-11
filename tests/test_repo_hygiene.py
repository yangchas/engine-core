from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "tests" / "fixtures" / "evidence_raw_allowlist.json"
RAW_SUFFIXES = {".jsonl", ".csv", ".gz", ".sqlite", ".log", ".tar", ".pyc"}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:amqp|amqps|redis|rediss|https?)://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"(?i)\b(?:password|passwd|token|secret)\s*=\s*['\"]?[^\s'\"`]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r'''(?i)["'](?:password|passwd|secret|api_key)["']\s*:\s*["']
        (?!redacted\b|unavailable\b|unknown\b|null\b|none(?=["'])|<|\*+)
        [^"'\s]{4,}["']''',
        re.VERBOSE,
    ),
)


def _tracked_evidence_paths() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "docs/evidence"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def test_tracked_raw_evidence_is_absent_or_hash_pinned_and_justified():
    entries = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    allowlist = {entry["path"]: entry for entry in entries}
    assert len(allowlist) == len(entries), "raw-evidence allowlist has duplicate paths"

    for path in _tracked_evidence_paths():
        relative = path.relative_to(ROOT).as_posix()
        if path.stat().st_size > 5 * 1024 * 1024:
            warnings.warn(
                f"large tracked evidence should have a documented reason: {relative}",
                stacklevel=1,
            )
        if path.suffix.lower() not in RAW_SUFFIXES:
            continue
        entry = allowlist.get(relative)
        assert entry is not None, f"raw evidence requires explicit review: {relative}"
        assert entry.get("reason"), f"raw evidence requires a reason: {relative}"
        assert entry.get("sha256") == _sha256(path), (
            f"raw evidence hash differs from allowlist: {relative}"
        )

    assert set(allowlist) == {
        path.relative_to(ROOT).as_posix()
        for path in _tracked_evidence_paths()
        if path.suffix.lower() in RAW_SUFFIXES
    }, "allowlist contains an untracked or non-raw evidence path"


def test_tracked_evidence_contains_no_obvious_credentials():
    for path in _tracked_evidence_paths():
        content = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            assert pattern.search(content) is None, (
                f"credential-like content found in {path.relative_to(ROOT)}"
            )


def test_obvious_private_key_header_is_detected():
    content = "-----BEGIN RSA PRIVATE KEY-----\nredacted fixture body\n"

    assert any(pattern.search(content) for pattern in SECRET_PATTERNS)


def test_json_credentials_are_detected_without_flagging_placeholders():
    credential_examples = (
        '{"password": "correct-horse-battery"}',
        '{"api_key":"AKIA1234567890EXAMPLE"}',
    )
    placeholders = (
        '{"password": "<redacted>"}',
        '{"api_key": "unavailable"}',
        '{"secret": "null"}',
        '{"password": "none"}',
    )

    assert all(
        any(pattern.search(content) for pattern in SECRET_PATTERNS)
        for content in credential_examples
    )
    assert all(
        not any(pattern.search(content) for pattern in SECRET_PATTERNS)
        for content in placeholders
    )


def test_core_source_does_not_import_legacy_runtime():
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(
                name == "engine_next" or name.startswith("engine_next.")
                for name in names
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "Core source imports engine_next: " + ", ".join(offenders)
