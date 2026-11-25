from .spec import DatasetSpec

SPEC = DatasetSpec("trade_calendar", "explicit_calendar_provider", "low_frequency", hot_view=False,
                empty_is_valid=False, key_fields=("date",))
