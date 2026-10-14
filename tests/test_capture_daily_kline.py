import importlib.util
import json
from pathlib import Path


def test_cli_runs_with_injected_fake_client(tmp_path, capsys):
    script = Path(__file__).resolve().parents[1] / "examples" / "capture_daily_kline.py"
    spec = importlib.util.spec_from_file_location("capture_daily_kline_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    symbols_path = tmp_path / "symbols.json"
    symbols_path.write_text(json.dumps(["600000"]), encoding="utf-8")
    output = tmp_path / "daily"

    class Client:
        def login(self):
            pass

        def query(self, code, date):
            return [{"code": code, "date": date, "open": "9", "high": "11", "low": "9", "close": "10", "preclose": "9", "volume": "2", "amount": "200", "turn": "1", "pctChg": "11.11"}]

        def logout(self):
            pass

    status = module.main(["--date", "2026-10-14", "--symbols-json", str(symbols_path), "--out-dir", str(output)], client=Client())
    assert status == 0
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["symbol_count"] == 1
    assert '"source": "baostock"' in capsys.readouterr().out
