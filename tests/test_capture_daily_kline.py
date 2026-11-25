import importlib.util
import json
from pathlib import Path


def test_cli_runs_with_injected_fake_client(tmp_path, capsys):
    script = Path(__file__).resolve().parents[1] / "examples" / "capture_daily_kline.py"
    spec = importlib.util.spec_from_file_location("capture_daily_kline_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    symbols_path = tmp_path / "symbols.json"
    symbols_path.write_text(json.dumps(["605258"]), encoding="utf-8")
    output = tmp_path / "daily"

    class Client:
        def login(self):
            pass

        def query(self, code, date):
            fixture = Path(__file__).parent / "fixtures/real/daily_kline_c755/baostock_raw.jsonl"
            call = json.loads(fixture.read_text().splitlines()[1])
            return [dict(zip(call["fields"], call["rows"][0]))]

        def logout(self):
            pass

    status = module.main(["--date", "2026-11-02", "--symbols-json", str(symbols_path), "--out-dir", str(output)], client=Client())
    assert status == 0
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["symbol_count"] == 1
    assert '"source": "baostock"' in capsys.readouterr().out
