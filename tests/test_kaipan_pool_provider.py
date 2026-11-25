

def test_kaipan_limit_up_pool_stops_at_errcode_1020_past_top_level():
    # Real getHisBans(2026-10-19) responses fetched 2026-10-20: level 6 is past the
    # top populated level and returns {'errcode': '1020', 'errmsg': '参数出错'}.
    import json
    from pathlib import Path
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
    levels = json.loads((Path(__file__).parent / "fixtures/kaipan_pool/his_bans_20261019.json").read_text())["levels"]

    class Client:
        def get_his_bans(self, day, level):
            return levels.get(str(level), levels["6"])

    result = KaipanProvider(Client(), max_requests=50).limit_up_pool("2026-10-19")
    rows = result["rows"] if isinstance(result, dict) and "rows" in result else result
    assert len(rows) > 0
