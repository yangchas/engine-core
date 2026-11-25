import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "run_wencai_fallback", Path(__file__).parents[1] / "examples" / "run_wencai_fallback.py"
)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def test_cli_rejects_nonempty_output_directory(tmp_path):
    (tmp_path / "existing.txt").write_text("x")
    calls = []
    with pytest.raises(SystemExit) as exc:
        cli.main(["--what", "auction", "--output-dir", str(tmp_path)],
                 fetch=lambda *args: calls.append(args))
    assert exc.value.code == 2
    assert calls == []
