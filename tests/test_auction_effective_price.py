from engine_core.auction_effective_price import effective_price


def test_book_one_is_effective_indicative_price_before_final():
    assert effective_price(
        {"px_milli": None, "bp1_milli": 12345, "ap1_milli": 12345},
        phase_cutoff_time="09:24:10", tick_ts="2026-10-14 09:24:09",
    ) == (12345, "TICK_BOOK1_INDICATIVE", ())


def test_final_tick_price_has_priority_after_0925():
    assert effective_price(
        {"px_milli": 12400, "bp1_milli": 12300, "ap1_milli": 12300},
        phase_cutoff_time="09:25:06", tick_ts="2026-10-14 09:25:03",
    ) == (12400, "TICK_PX_FINAL", ())
    assert effective_price(
        {"px_milli": 0, "bp1_milli": 12300, "ap1_milli": 12300},
        phase_cutoff_time="09:25:06", tick_ts="2026-10-14 09:25:03",
    ) == (12300, "TICK_BOOK1_INDICATIVE", ())


def test_indicative_price_after_0925_is_flagged_pre_final():
    assert effective_price(
        {"px_milli": 12300, "bp1_milli": 12300, "ap1_milli": 12300},
        phase_cutoff_time="09:25:06", tick_ts="2026-10-14 09:24:57",
    ) == (12300, "TICK_BOOK1_INDICATIVE", ("PRE_FINAL_INDICATIVE",))


def test_no_matching_book_one_does_not_invent_a_price():
    assert effective_price(
        {"px_milli": None, "bp1_milli": 12300, "ap1_milli": 12310},
        phase_cutoff_time="09:24:10", tick_ts="2026-10-14 09:24:09",
    ) == (None, "NO_EFFECTIVE_PRICE", ())
