"""Local SONIC helpers, with numeric dependencies loaded only when requested."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .features import (
        FEATURE_SCHEMA_VERSION,
        SIMILARITY_REPORT_SCHEMA_VERSION,
        STABILITY_REPORT_SCHEMA_VERSION,
        SonicFeatureConfig,
        SonicFeatureError,
        analyze_audio,
        build_similarity_report,
        evaluate_clustering_stability,
        extract_sonic_features,
        recording_similarity,
        validate_feature_record,
    )
    from .evaluation import (
        ALGORITHM_VERSION as STATISTICAL_VALIDATION_ALGORITHM_VERSION,
        SCHEMA_VERSION as STATISTICAL_VALIDATION_SCHEMA_VERSION,
        build_statistical_validation,
    )

__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "SIMILARITY_REPORT_SCHEMA_VERSION",
    "STABILITY_REPORT_SCHEMA_VERSION",
    "SonicFeatureConfig",
    "SonicFeatureError",
    "STATISTICAL_VALIDATION_ALGORITHM_VERSION",
    "STATISTICAL_VALIDATION_SCHEMA_VERSION",
    "analyze_audio",
    "build_statistical_validation",
    "build_similarity_report",
    "evaluate_clustering_stability",
    "extract_sonic_features",
    "recording_similarity",
    "validate_feature_record",
]

_EVALUATION_EXPORTS = {
    "STATISTICAL_VALIDATION_ALGORITHM_VERSION": "ALGORITHM_VERSION",
    "STATISTICAL_VALIDATION_SCHEMA_VERSION": "SCHEMA_VERSION",
    "build_statistical_validation": "build_statistical_validation",
}


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = ".evaluation" if name in _EVALUATION_EXPORTS else ".features"
    value = getattr(import_module(module, __name__), _EVALUATION_EXPORTS.get(name, name))
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
