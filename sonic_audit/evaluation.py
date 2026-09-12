"""Offline, reference-group-disjoint validation for SONIC AUDIT features."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from typing import Any, Mapping, Sequence

from .features import SonicFeatureError, recording_similarity


SCHEMA_VERSION = "sonic-statistical-validation-v1"
ALGORITHM_VERSION = "reference-group-oof-v1"


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    materialized = [float(value) for value in values]
    return {
        "count": len(materialized),
        "minimum": _round(min(materialized)) if materialized else None,
        "p05": _round(_quantile(materialized, 0.05)),
        "p25": _round(_quantile(materialized, 0.25)),
        "median": _round(_quantile(materialized, 0.5)),
        "mean": _round(sum(materialized) / len(materialized)) if materialized else None,
        "p75": _round(_quantile(materialized, 0.75)),
        "p95": _round(_quantile(materialized, 0.95)),
        "maximum": _round(max(materialized)) if materialized else None,
    }


def _classification_metrics(
    scored: Sequence[tuple[float, bool]],
    threshold: float,
) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    for score, truth in scored:
        predicted = score >= threshold
        if truth and predicted:
            tp += 1
        elif truth:
            fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    predicted_positive = tp + fp
    positives = tp + fn
    negatives = fp + tn
    return {
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "positive_support": positives,
        "negative_support": negatives,
        "precision": _round(tp / predicted_positive) if predicted_positive else None,
        "recall": _round(tp / positives) if positives else None,
        "false_match_rate": _round(fp / negatives) if negatives else None,
    }


def _candidate_thresholds(scored: Sequence[tuple[float, bool]]) -> list[float]:
    unique = sorted({max(0.0, min(1.0, float(score))) for score, _ in scored})
    if not unique:
        return []
    thresholds = {0.0, 1.0, *unique}
    thresholds.update((left + right) / 2.0 for left, right in zip(unique, unique[1:]))
    return sorted(thresholds)


def _choose_threshold(
    scored: Sequence[tuple[float, bool]],
    *,
    target_false_match_rate: float,
) -> tuple[float | None, dict[str, Any]]:
    positives = sum(truth for _, truth in scored)
    negatives = len(scored) - positives
    if not positives or not negatives:
        return None, {
            "status": "not_evaluable",
            "reason": "calibration_requires_positive_and_negative_pairs",
            "positive_support": positives,
            "negative_support": negatives,
        }
    eligible: list[tuple[tuple[float, float, float], float, dict[str, Any]]] = []
    for threshold in _candidate_thresholds(scored):
        metrics = _classification_metrics(scored, threshold)
        fmr = metrics["false_match_rate"]
        if fmr is not None and fmr <= target_false_match_rate:
            precision = metrics["precision"] if metrics["precision"] is not None else 0.0
            recall = metrics["recall"] if metrics["recall"] is not None else 0.0
            eligible.append(((recall, precision, threshold), threshold, metrics))
    if not eligible:
        return None, {
            "status": "not_evaluable",
            "reason": "no_threshold_met_calibration_constraint",
            "positive_support": positives,
            "negative_support": negatives,
        }
    _, threshold, metrics = max(eligible, key=lambda item: item[0])
    return threshold, {
        "status": "selected",
        "objective": "maximize_recall_then_precision_then_threshold",
        "target_false_match_rate": target_false_match_rate,
        "threshold": _round(threshold),
        **metrics,
    }


def _percentile_interval(values: Sequence[float]) -> dict[str, Any]:
    return {
        "method": "reference_label_grouped_bootstrap_percentile",
        "confidence_level": 0.95,
        "lower": _round(_quantile(values, 0.025)),
        "upper": _round(_quantile(values, 0.975)),
        "replicates": len(values),
    }


def _wilson_interval(successes: int, trials: int) -> dict[str, Any]:
    if trials <= 0:
        return {
            "method": "wilson_score_diagnostic",
            "confidence_level": 0.95,
            "successes": int(successes),
            "trials": int(trials),
            "lower": None,
            "upper": None,
        }
    z = 1.959963984540054
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    center = (proportion + z * z / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / trials
            + z * z / (4.0 * trials * trials)
        )
        / denominator
    )
    return {
        "method": "wilson_score_diagnostic",
        "confidence_level": 0.95,
        "successes": int(successes),
        "trials": int(trials),
        "lower": _round(max(0.0, center - margin)),
        "upper": _round(min(1.0, center + margin)),
    }


def _query_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    count = len(rows)
    resolved = [row for row in rows if row.get("resolved") is True]
    return {
        "recall_at_1": sum(row.get("hit_at_1") is True for row in rows) / count if count else None,
        "recall_at_5": sum(row.get("hit_at_5") is True for row in rows) / count if count else None,
        "resolution_coverage": len(resolved) / count if count else None,
        "resolved_query_precision": (
            sum(row.get("resolved_correctly") is True for row in resolved) / len(resolved)
            if resolved
            else None
        ),
    }


def _grouped_bootstrap(
    query_rows: Sequence[Mapping[str, Any]],
    *,
    seed: int,
    replicates: int,
) -> dict[str, Any]:
    by_label: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in query_rows:
        by_label[str(row.get("reference_label") or "")].append(row)
    labels = sorted(label for label in by_label if label)
    if len(labels) < 2:
        return {
            "status": "not_evaluable",
            "reason": "fewer_than_two_repeated_reference_groups",
            "group_count": len(labels),
        }
    generator = random.Random(seed)
    sampled: dict[str, list[float]] = defaultdict(list)
    for _ in range(replicates):
        rows: list[Mapping[str, Any]] = []
        for label in (generator.choice(labels) for _ in labels):
            rows.extend(by_label[label])
        metrics = _query_metrics(rows)
        for key, value in metrics.items():
            if value is not None:
                sampled[key].append(float(value))
    return {
        "status": "evaluated",
        "group_count": len(labels),
        "seed": seed,
        "replicates": replicates,
        "intervals": {key: _percentile_interval(values) for key, values in sampled.items()},
    }


def build_statistical_validation(
    records: Sequence[Mapping[str, Any]],
    references: Mapping[str, str] | None = None,
    *,
    target_false_match_rate: float = 0.0,
    maximum_folds: int = 4,
    bootstrap_replicates: int = 2000,
    seed: int = 17,
) -> dict[str, Any]:
    """Evaluate recording similarity without tuning on the held reference IDs."""

    if not 0.0 <= target_false_match_rate <= 1.0:
        raise SonicFeatureError("invalid_target_fmr", "target false-match rate must be between zero and one")
    if maximum_folds < 2 or bootstrap_replicates < 100:
        raise SonicFeatureError("invalid_validation_config", "validation fold/bootstrap bounds are invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    labels: dict[str, str] = {}
    quality_exclusions: dict[str, str] = {}
    for record in records:
        post_id = str(record.get("post_id") or "")
        if not post_id or post_id in by_id:
            raise SonicFeatureError("invalid_post_set", "post identifiers must be present and unique")
        by_id[post_id] = record
        supplied = references.get(post_id) if references is not None else record.get("reference_label")
        if supplied is not None and str(supplied).strip():
            labels[post_id] = str(supplied).strip()
        quality = record.get("quality")
        quality_status = str(quality.get("status") or "missing") if isinstance(quality, Mapping) else "missing"
        if quality_status != "usable":
            quality_exclusions[post_id] = f"quality_{quality_status}"
    labeled_ids = sorted(post_id for post_id in labels if post_id not in quality_exclusions)
    label_groups: dict[str, list[str]] = defaultdict(list)
    for post_id in labeled_ids:
        label_groups[labels[post_id]].append(post_id)
    repeated_labels = sorted(label for label, ids in label_groups.items() if len(ids) >= 2)
    singleton_labels = sorted(label for label, ids in label_groups.items() if len(ids) == 1)

    comparisons: dict[tuple[str, str], dict[str, Any]] = {}
    pair_rows: list[dict[str, Any]] = []
    for index, left_id in enumerate(labeled_ids):
        for right_id in labeled_ids[index + 1 :]:
            comparison = recording_similarity(by_id[left_id], by_id[right_id])
            if not comparison.get("comparable"):
                continue
            score = float(comparison["recording_score"])
            key = (left_id, right_id)
            comparisons[key] = comparison
            pair_rows.append(
                {
                    "left_post_id": left_id,
                    "right_post_id": right_id,
                    "left_reference_label": labels[left_id],
                    "right_reference_label": labels[right_id],
                    "score": score,
                    "truth_same_reference": labels[left_id] == labels[right_id],
                }
            )

    positive_scores = [row["score"] for row in pair_rows if row["truth_same_reference"]]
    negative_scores = [row["score"] for row in pair_rows if not row["truth_same_reference"]]
    descriptive_scored = [(row["score"], row["truth_same_reference"]) for row in pair_rows]
    sweep = []
    for threshold in sorted(_candidate_thresholds(descriptive_scored), reverse=True):
        sweep.append({"threshold": _round(threshold), **_classification_metrics(descriptive_scored, threshold)})

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "status": "not_evaluable",
        "recommendation_status": "exploratory_only",
        "configuration": {
            "target_false_match_rate": target_false_match_rate,
            "maximum_folds": maximum_folds,
            "bootstrap_replicates": bootstrap_replicates,
            "seed": seed,
        },
        "support": {
            "input_record_count": len(records),
            "usable_labeled_record_count": len(labeled_ids),
            "distinct_reference_count": len(label_groups),
            "repeated_reference_group_count": len(repeated_labels),
            "singleton_reference_group_count": len(singleton_labels),
            "positive_pair_count": len(positive_scores),
            "negative_pair_count": len(negative_scores),
            "quality_excluded_record_count": len(quality_exclusions),
        },
        "descriptive_in_sample": {
            "warning": "These distributions and sweep use all labels and are not held-out accuracy estimates.",
            "positive_score_distribution": _distribution(positive_scores),
            "negative_score_distribution": _distribution(negative_scores),
            "threshold_sweep": sweep,
        },
        "folds": [],
        "out_of_fold": {},
        "conflict_diagnostics": {},
        "limitations": [
            "Apple item IDs are proxy reference labels, not verified equality of the audible TikTok mixes.",
            "Reference groups are few and the sample is stratified rather than portfolio-representative.",
            "Pair comparisons reuse posts and are not independent binomial trials.",
            "The matcher caps embedding-only comparisons below the legacy 0.72 threshold, so the old score gap is partly structural.",
            "This validation does not authorize or perform new media access, AI analysis, or catalog identification.",
        ],
    }
    if len(repeated_labels) < 2 or not positive_scores or not negative_scores:
        body["reason"] = "insufficient_repeated_reference_support"
        body["validation_hash"] = _hash(body)
        return body

    fold_count = min(maximum_folds, len(repeated_labels))
    held_by_fold = [repeated_labels[index::fold_count] for index in range(fold_count)]
    query_rows: list[dict[str, Any]] = []
    directional_scored: list[tuple[float, bool]] = []
    for fold_index, held_labels in enumerate(held_by_fold):
        held_set = set(held_labels)
        calibration_ids = [post_id for post_id in labeled_ids if labels[post_id] not in held_set]
        calibration_set = set(calibration_ids)
        calibration_rows = [
            row
            for row in pair_rows
            if row["left_post_id"] in calibration_set and row["right_post_id"] in calibration_set
        ]
        threshold, selection = _choose_threshold(
            [(row["score"], row["truth_same_reference"]) for row in calibration_rows],
            target_false_match_rate=target_false_match_rate,
        )
        if threshold is None:
            body["reason"] = "at_least_one_fold_could_not_select_a_threshold"
            body["folds"].append(
                {
                    "fold": fold_index,
                    "held_reference_labels": held_labels,
                    "threshold_selection": selection,
                }
            )
            body["validation_hash"] = _hash(body)
            return body
        held_ids = [post_id for post_id in labeled_ids if labels[post_id] in held_set]
        fold_queries: list[dict[str, Any]] = []
        for query_id in held_ids:
            candidates: list[tuple[float, str]] = []
            for candidate_id in labeled_ids:
                if candidate_id == query_id:
                    continue
                key = tuple(sorted((query_id, candidate_id)))
                comparison = comparisons.get(key)
                if comparison is None:
                    continue
                score = float(comparison["recording_score"])
                truth = labels[query_id] == labels[candidate_id]
                directional_scored.append((score, truth))
                candidates.append((score, candidate_id))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            top = candidates[0] if candidates else None
            resolved = bool(top and top[0] >= threshold)
            hit_at_1 = bool(top and labels[top[1]] == labels[query_id])
            hit_at_5 = any(labels[candidate_id] == labels[query_id] for _, candidate_id in candidates[:5])
            row = {
                "fold": fold_index,
                "query_post_id": query_id,
                "reference_label": labels[query_id],
                "selected_threshold": _round(threshold),
                "top_post_id": top[1] if top else None,
                "top_score": _round(top[0]) if top else None,
                "resolved": resolved,
                "resolved_correctly": bool(resolved and hit_at_1),
                "hit_at_1": hit_at_1,
                "hit_at_5": hit_at_5,
            }
            query_rows.append(row)
            fold_queries.append(row)
        body["folds"].append(
            {
                "fold": fold_index,
                "held_reference_labels": held_labels,
                "calibration_record_count": len(calibration_ids),
                "calibration_pair_count": len(calibration_rows),
                "threshold_selection": selection,
                "held_query_count": len(fold_queries),
                "held_query_metrics": {key: _round(value) for key, value in _query_metrics(fold_queries).items()},
            }
        )

    query_metrics = _query_metrics(query_rows)
    # Directional rows use fold-specific thresholds, so aggregate their actual
    # decisions rather than applying a synthetic single threshold.
    tp = fp = tn = fn = 0
    for row in query_rows:
        query_id = row["query_post_id"]
        threshold = float(row["selected_threshold"])
        for candidate_id in labeled_ids:
            if candidate_id == query_id:
                continue
            comparison = comparisons.get(tuple(sorted((query_id, candidate_id))))
            if comparison is None:
                continue
            truth = labels[query_id] == labels[candidate_id]
            predicted = float(comparison["recording_score"]) >= threshold
            if truth and predicted:
                tp += 1
            elif truth:
                fn += 1
            elif predicted:
                fp += 1
            else:
                tn += 1
    predicted = tp + fp
    positives = tp + fn
    negatives = fp + tn
    body["status"] = "evaluated_exploratory"
    body["reason"] = None
    body["out_of_fold"] = {
        "split_unit": "reference_label",
        "fold_count": fold_count,
        "query_count": len(query_rows),
        "query_metrics": {key: _round(value) for key, value in query_metrics.items()},
        "directional_query_candidate_metrics": {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
            "positive_support": positives,
            "negative_support": negatives,
            "precision": _round(tp / predicted) if predicted else None,
            "recall": _round(tp / positives) if positives else None,
            "false_match_rate": _round(fp / negatives) if negatives else None,
            "warning": "Directional comparisons reuse posts and are descriptive support, not independent trials.",
        },
        "grouped_bootstrap": _grouped_bootstrap(
            query_rows,
            seed=seed,
            replicates=bootstrap_replicates,
        ),
        "query_level_wilson_diagnostics": {
            "warning": "Queries sharing a reference label are dependent; use these finite-support bounds only as a diagnostic alongside the grouped bootstrap.",
            "recall_at_1": _wilson_interval(
                sum(row.get("hit_at_1") is True for row in query_rows),
                len(query_rows),
            ),
            "recall_at_5": _wilson_interval(
                sum(row.get("hit_at_5") is True for row in query_rows),
                len(query_rows),
            ),
            "resolution_coverage": _wilson_interval(
                sum(row.get("resolved") is True for row in query_rows),
                len(query_rows),
            ),
            "resolved_query_precision": _wilson_interval(
                sum(row.get("resolved_correctly") is True for row in query_rows),
                sum(row.get("resolved") is True for row in query_rows),
            ),
        },
        "queries": query_rows,
    }
    source_hash_labels: dict[str, set[str]] = defaultdict(set)
    for post_id in labeled_ids:
        source_hash = str(by_id[post_id].get("source_audio_sha256") or "")
        if source_hash:
            source_hash_labels[source_hash].add(labels[post_id])
    body["conflict_diagnostics"] = {
        "identical_source_audio_with_multiple_reference_labels": [
            {"source_audio_sha256": source_hash, "reference_labels": sorted(values)}
            for source_hash, values in sorted(source_hash_labels.items())
            if len(values) > 1
        ],
        "same_reference_pairs_below_legacy_threshold": sum(score < 0.72 for score in positive_scores),
        "legacy_threshold": 0.72,
    }
    body["validation_hash"] = _hash(body)
    return body


__all__ = [
    "ALGORITHM_VERSION",
    "SCHEMA_VERSION",
    "build_statistical_validation",
]
