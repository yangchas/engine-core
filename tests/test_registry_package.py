from __future__ import annotations

import dataclasses
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from engine_core.data_pipeline.registry import DATASETS, get_dataset


def _legacy_registry():
    source = subprocess.run(
        ["git", "show", "HEAD:src/engine_core/data_pipeline/registry.py"],
        check=True, capture_output=True, text=True,
    ).stdout
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry_baseline.py"
        path.write_text(source, encoding="utf-8")
        module_spec = importlib.util.spec_from_file_location("registry_baseline", path)
        assert module_spec and module_spec.loader
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        try:
            module_spec.loader.exec_module(module)
            return module.DATASETS
        finally:
            sys.modules.pop(module_spec.name, None)


def test_registry_package_discovers_datasets_and_preserves_legacy_specs():
    legacy = _legacy_registry()
    assert list(DATASETS) == sorted(DATASETS)
    assert set(DATASETS) == set(legacy) | {"quote_slice"}
    for name, old_spec in legacy.items():
        current = DATASETS[name]
        for field in dataclasses.fields(old_spec):
            assert getattr(current, field.name) == getattr(old_spec, field.name), (name, field.name)


def test_unknown_dataset_error_keeps_legacy_message():
    with pytest.raises(KeyError, match=r"unknown_dataset:nope"):
        get_dataset("nope")
