"""Decoupled, offline-first sentiment research factors."""
from .contracts import Timepoint, FactorOutput, DayTimeline, DatasetMeta
from .factors import *
from .factors import __all__ as _factor_names
from .pipeline import build_timeseries_dataset

FACTOR_REGISTRY = {name.removeprefix('compute_'): globals()[name] for name in _factor_names}
__all__ = ['Timepoint', 'FactorOutput', 'DayTimeline', 'DatasetMeta', 'FACTOR_REGISTRY', 'build_timeseries_dataset', *_factor_names]
