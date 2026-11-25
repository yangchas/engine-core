import signal

import pytest

from examples import rehearse_event


def test_main_installs_sigterm_handler(monkeypatch):
    original = signal.getsignal(signal.SIGTERM)

    class Parser:
        def parse_args(self, argv):
            raise SystemExit(0)

    monkeypatch.setattr(rehearse_event, "_parser", lambda: Parser())
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        with pytest.raises(SystemExit):
            rehearse_event.main([])
        handler = signal.getsignal(signal.SIGTERM)
        assert handler != signal.SIG_DFL
        with pytest.raises(SystemExit) as exc:
            handler(signal.SIGTERM, None)
        assert exc.value.code == 143
    finally:
        signal.signal(signal.SIGTERM, original)
