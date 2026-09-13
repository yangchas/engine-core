# DataResult timestamp contract (2026-09-13)

## Change

Code commit `6275853072b4686deb3ccbe78764f0a452208adb` adds immutable-boundary
validation for `DataResult` epoch-millisecond fields. `observed_at_ms` is
required; `effective_at_ms` and `available_at_ms` remain optional. Every
provided value must be a positive integer, and booleans, zero, negative and
non-integer values are rejected before semantic hashing.

This closes the hole where an external provider could submit an ambiguous
timestamp that passed the availability gate. The change does not alter the
source/provider or temporal semantics; it only makes the existing contract
fail closed at construction time.

## Verification

```text
local: 305 passed, compileall PASS, git diff --check PASS
cobra-ion: 305 passed, compileall PASS
python: 3.12.3
timezone: Asia/Shanghai
PYTHONHASHSEED: 0
production writes: 0
```

The Cobra run used a streamed archive of the exact code commit in a temporary
directory. No production service, Redis/TD table, Rabbit ACK or notification
path was touched. Tests include boundary cases for required observation time,
zero/negative/bool timestamps and optional timestamp fields.
