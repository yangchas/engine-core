"""Auto-discovered dataset registry."""
from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules

from .spec import CheckSpec, DatasetSpec, FieldSpec


def _discover() -> dict[str, DatasetSpec]:
    specs: dict[str, DatasetSpec] = {}
    for module_info in iter_modules(__path__):
        if module_info.name == "spec":
            continue
        module = import_module(f".{module_info.name}", __name__)
        spec = getattr(module, "SPEC", None)
        if isinstance(spec, DatasetSpec):
            if spec.name in specs:
                raise ValueError(f"duplicate_dataset:{spec.name}")
            specs[spec.name] = spec
    return dict(sorted(specs.items()))


DATASETS = _discover()


def get_dataset(name: str) -> DatasetSpec:
    try:
        return DATASETS[name]
    except KeyError as exc:
        raise KeyError(f"unknown_dataset:{name}") from exc


__all__ = ["CheckSpec", "DATASETS", "DatasetSpec", "FieldSpec", "get_dataset"]
