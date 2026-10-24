"""Pure source-to-canonical value conversions."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from zoneinfo import ZoneInfo

from .units import parse_source_code


class ConversionError(str, Enum):
    UNPARSEABLE = "UNPARSEABLE"
    NON_INTEGER = "NON_INTEGER"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    UNSUPPORTED_CONVERSION = "UNSUPPORTED_CONVERSION"


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    number = Decimal(str(value))
    if not number.is_finite():
        raise InvalidOperation
    return number


def _integer(number: Decimal) -> int:
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _exact_integer(number: Decimal) -> int:
    if number != number.to_integral_value():
        raise ArithmeticError
    return int(number)


def _iso_date(value: object, encoding: str) -> str:
    text = str(value)
    if encoding == "yyyymmdd":
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    return date.fromisoformat(text).isoformat()


def _iso_datetime(value: object, source_unit: str) -> str:
    if source_unit == "epoch_ms":
        milliseconds = _exact_integer(_decimal(value))
        moment = (_EPOCH + timedelta(milliseconds=milliseconds)).astimezone(_SHANGHAI)
    else:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_SHANGHAI)
        else:
            moment = moment.astimezone(_SHANGHAI)
    return moment.isoformat(timespec="milliseconds")


def to_canonical(value: object, source_code: str, canonical_unit: str) -> object | None | ConversionError:
    """Convert a source value without raising for malformed or unsupported data."""
    if value is None or (isinstance(value, str) and value in {"", "--"}):
        return None
    try:
        code = parse_source_code(source_code)
        if code.special == "ABSENT":
            return None
        if code.special == "UNKNOWN":
            return ConversionError.UNKNOWN_UNIT
        unit = code.unit
        encoding = code.encoding
        assert unit is not None and encoding is not None

        if unit == "yyyymmdd" and canonical_unit == "iso_date":
            return _iso_date(value, "yyyymmdd")
        if unit == "iso_date" and canonical_unit == "iso_date":
            return _iso_date(value, "iso_date")
        if unit in {"epoch_ms", "local_datetime_cst"} and canonical_unit == "iso_datetime":
            return _iso_datetime(value, unit)

        if unit == "none" and canonical_unit == "none":
            if encoding == "bool":
                return value if isinstance(value, bool) else ConversionError.UNPARSEABLE
            if encoding == "str":
                return str(value)
            number = _decimal(value)
            return _exact_integer(number)
        if unit == "category" and canonical_unit in {"category", "none"}:
            return str(value)
        if unit == "hands" and canonical_unit == "hand":
            return str(value)

        number = _decimal(value)
        if unit == "yuan" and canonical_unit == "price_milli":
            return _integer(number * 1000)
        if unit == "milli_yuan" and canonical_unit == "price_milli":
            return _exact_integer(number)
        if unit == "percent" and canonical_unit == "bp":
            return _integer(number * 100)
        if unit == "bp" and canonical_unit == "bp":
            return _exact_integer(number)
        if unit == "yuan" and canonical_unit == "yuan":
            return _integer(number)
        if unit == "hands" and canonical_unit == "shares":
            return _integer(number * 100)
        if unit == "shares" and canonical_unit == "shares":
            return _integer(number)
        if unit == "percent" and canonical_unit == "percent":
            return float(number)
        if unit == "count" and canonical_unit == "count":
            return _integer(number)
        return ConversionError.UNSUPPORTED_CONVERSION
    except InvalidOperation:
        return ConversionError.UNPARSEABLE
    except ArithmeticError:
        return ConversionError.NON_INTEGER
    except (ValueError, TypeError, OverflowError, OSError):
        return ConversionError.UNPARSEABLE
