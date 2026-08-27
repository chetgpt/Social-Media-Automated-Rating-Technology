"""Local, derived-audio feature helpers for the isolated SONIC AUDIT workflow."""

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
