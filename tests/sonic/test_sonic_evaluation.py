import copy

import sonic_audit.evaluation as evaluation


def record(post_id, label, *, source_hash=""):
    return {
        "post_id": post_id,
        "reference_label": label,
        "quality": {"status": "usable"},
        "source_audio_sha256": source_hash,
    }


def install_similarity(monkeypatch, records, *, same=0.95, different=0.2, overrides=None):
    labels = {row["post_id"]: row["reference_label"] for row in records}
    overrides = overrides or {}

    def compare(left, right):
        pair = tuple(sorted((left["post_id"], right["post_id"])))
        score = overrides.get(
            pair,
            same if labels[left["post_id"]] == labels[right["post_id"]] else different,
        )
        return {"comparable": True, "recording_score": score}

    monkeypatch.setattr(evaluation, "recording_similarity", compare)


def test_reference_group_disjoint_validation_is_deterministic(monkeypatch):
    rows = []
    for label in ("a", "b", "c", "d"):
        rows.extend([record(f"{label}1", label), record(f"{label}2", label)])
    rows.extend([record("s1", "single-1"), record("s2", "single-2")])
    install_similarity(monkeypatch, rows)

    first = evaluation.build_statistical_validation(rows, bootstrap_replicates=200)
    second = evaluation.build_statistical_validation(copy.deepcopy(rows), bootstrap_replicates=200)

    assert first == second
    assert first["status"] == "evaluated_exploratory"
    assert first["recommendation_status"] == "exploratory_only"
    assert first["out_of_fold"]["query_metrics"]["recall_at_1"] == 1.0
    assert first["out_of_fold"]["query_metrics"]["resolved_query_precision"] == 1.0
    held = [label for fold in first["folds"] for label in fold["held_reference_labels"]]
    assert sorted(held) == ["a", "b", "c", "d"]
    assert len(set(held)) == len(held)
    assert len(first["validation_hash"]) == 64


def test_threshold_is_selected_without_held_reference_scores(monkeypatch):
    rows = []
    for label in ("a", "b", "c", "d"):
        rows.extend([record(f"{label}1", label), record(f"{label}2", label)])
    rows.extend([record("s1", "single-1"), record("s2", "single-2")])
    overrides = {("a1", "a2"): 0.3}
    install_similarity(monkeypatch, rows, same=0.95, different=0.2, overrides=overrides)

    result = evaluation.build_statistical_validation(rows, bootstrap_replicates=200)

    a_fold = next(fold for fold in result["folds"] if fold["held_reference_labels"] == ["a"])
    assert a_fold["threshold_selection"]["threshold"] > 0.3
    a_queries = [
        row for row in result["out_of_fold"]["queries"] if row["reference_label"] == "a"
    ]
    assert all(row["resolved_correctly"] is False for row in a_queries)


def test_identical_audio_under_different_labels_is_flagged(monkeypatch):
    rows = []
    for label in ("a", "b"):
        rows.extend([record(f"{label}1", label), record(f"{label}2", label)])
    rows.extend([record("s1", "single-1"), record("s2", "single-2")])
    rows[0]["source_audio_sha256"] = "f" * 64
    rows[2]["source_audio_sha256"] = "f" * 64
    install_similarity(monkeypatch, rows)

    result = evaluation.build_statistical_validation(
        rows,
        maximum_folds=2,
        bootstrap_replicates=200,
    )

    conflicts = result["conflict_diagnostics"][
        "identical_source_audio_with_multiple_reference_labels"
    ]
    assert conflicts == [
        {"source_audio_sha256": "f" * 64, "reference_labels": ["a", "b"]}
    ]


def test_insufficient_repeated_labels_is_not_evaluable(monkeypatch):
    rows = [record("a1", "a"), record("a2", "a"), record("s1", "single")]
    install_similarity(monkeypatch, rows)

    result = evaluation.build_statistical_validation(rows, bootstrap_replicates=200)

    assert result["status"] == "not_evaluable"
    assert result["reason"] == "insufficient_repeated_reference_support"
    assert result["recommendation_status"] == "exploratory_only"
