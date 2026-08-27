import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonic_audit.evaluation as evaluation_module
import sonic_audit_tiktok as runner
from sonic_audit.contracts import (
    REPORT_SCHEMA,
    bind_feature_record,
    bind_hash,
    build_run_manifest,
    verify_hash,
)


def _candidate(post_id: str, *, creator: str = "bankbca") -> dict:
    return {
        "post_id": post_id,
        "canonical_url": f"https://www.tiktok.com/@{creator}/video/{post_id}",
        "creator_handle": creator,
        "content_type": "video",
        "base_snapshot_id": f"snapshot-{post_id}",
        "base_evidence_hash": "a" * 64,
        "base_observed_at": "2026-08-15T00:00:00+00:00",
        "reference_kind": "apple_track_id",
        "reference_id": "track-a",
        "reference_title": "Song",
        "reference_artist": "Artist",
        "reference_basis": "tt2dsp_exact_apple_id_resolution",
        "reference_group_size": 2,
        "selection_bucket": "repeated_catalog_reference",
        "platform_music_id": "music",
        "platform_music_title": "Song",
        "platform_music_author": "Artist",
        "platform_music_original": False,
    }


def _make_complete_run(
    output_root: Path,
    *,
    run_id: str,
    post_id: str,
    master: Path,
    creator: str = "bankbca",
    algorithm: str = "classical-sonic-cpu-v2",
    config: dict | None = None,
) -> tuple[Path, dict, dict]:
    row = _candidate(post_id, creator=creator)
    manifest = build_run_manifest(
        run_id=run_id,
        project="suite-test",
        creator=creator,
        requested_count=1,
        master_database=master,
        candidates=[row],
        transient_audio_authorized=True,
    )
    features = {
        "schema_version": "sonic-feature-v1",
        "algorithm_version": algorithm,
        "config": config or {"sample_rate": 16_000},
        "feature_hash": post_id.rjust(64, "0")[-64:],
        "quality": {"status": "usable"},
    }
    record = bind_feature_record(
        {
            "run_id": run_id,
            "manifest_hash": manifest["manifest_hash"],
            "post_id": post_id,
            "creator_handle": creator,
            "base_snapshot_id": row["base_snapshot_id"],
            "base_evidence_hash": row["base_evidence_hash"],
            "status": "completed",
            "observed_at": "2026-08-15T00:00:00+00:00",
            "reference": {"reference_id": "track-a", "reference_kind": "apple_track_id"},
            "transport": {
                "provenance": {"account_handle": "kitascore"},
                "cleanup_before_checkpoint": True,
            },
            "features": features,
            "error": "",
            "raw_media_retained": False,
            "decoded_audio_retained": False,
        }
    )
    run_dir = output_root / run_id
    runner._atomic_write_json(run_dir / "manifest.json", manifest)
    runner._atomic_write_json(run_dir / "records" / f"{post_id}.json", record)
    state = runner._state_for_records(manifest, [record], status="sonic_complete")
    report = bind_hash(
        {
            "schema_version": REPORT_SCHEMA,
            "run_id": run_id,
            "manifest_hash": manifest["manifest_hash"],
        },
        "report_hash",
    )
    runner._atomic_write_json(run_dir / "state.json", state)
    runner._atomic_write_json(run_dir / "report.json", report)
    return run_dir, manifest, record


@pytest.fixture
def suite_stubs(monkeypatch):
    monkeypatch.setattr(runner, "validate_feature_record", lambda record, manifest: None)

    def feature_inputs(records):
        result = []
        for record in records:
            if record.get("status") != "completed":
                continue
            item = dict(record["features"])
            item["post_id"] = record["post_id"]
            item["reference_label"] = record["reference"]["reference_id"]
            item["reference_kind"] = record["reference"]["reference_kind"]
            result.append(item)
        return result

    monkeypatch.setattr(runner, "_feature_inputs", feature_inputs)
    monkeypatch.setattr(
        evaluation_module,
        "build_statistical_validation",
        lambda records: {
            "schema_version": "sonic-statistical-validation-v1",
            "status": "evaluated_exploratory",
            "input_post_ids": sorted(record["post_id"] for record in records),
        },
    )


def _args(output_root: Path, target: Path, *run_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        output_root=str(output_root),
        run_id=list(run_ids),
        file=str(target),
    )


def test_validate_suite_writes_ordered_hash_bound_offline_artifact(
    tmp_path, suite_stubs
):
    output_root = tmp_path / "runs"
    master = tmp_path / "master.sqlite"
    master.write_bytes(b"read-only-baseline")
    first_dir, first_manifest, first_record = _make_complete_run(
        output_root,
        run_id="sonic_1111111111111111",
        post_id="101",
        master=master,
    )
    second_dir, second_manifest, second_record = _make_complete_run(
        output_root,
        run_id="sonic_2222222222222222",
        post_id="202",
        master=master,
    )
    before_master = master.read_bytes()
    before_sources = {
        path: path.read_bytes()
        for run_dir in (first_dir, second_dir)
        for path in run_dir.rglob("*.json")
    }
    target = tmp_path / "suite.json"

    result = runner._run_statistical_validation_suite(
        _args(
            output_root,
            target,
            second_manifest["run_id"],
            first_manifest["run_id"],
        )
    )

    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == runner.STATISTICAL_VALIDATION_SUITE_SCHEMA
    assert verify_hash(artifact, "statistical_validation_suite_hash")
    assert artifact["ordered_run_ids"] == [
        second_manifest["run_id"],
        first_manifest["run_id"],
    ]
    assert [row["manifest_hash"] for row in artifact["source_run_bindings"]] == [
        second_manifest["manifest_hash"],
        first_manifest["manifest_hash"],
    ]
    assert [row["report_hash"] for row in artifact["source_run_bindings"]] == [
        json.loads((second_dir / "report.json").read_text())["report_hash"],
        json.loads((first_dir / "report.json").read_text())["report_hash"],
    ]
    assert [row["feature_record_set_hash"] for row in artifact["source_run_bindings"]] == [
        runner._feature_record_set_hash([second_record]),
        runner._feature_record_set_hash([first_record]),
    ]
    assert artifact["evaluation"]["input_post_ids"] == ["101", "202"]
    assert artifact["browser_access_performed"] is False
    assert artifact["media_access_performed"] is False
    assert artifact["master_registry_read"] is False
    assert artifact["master_registry_mutated"] is False
    assert artifact["ai_analysis_performed"] is False
    assert result["statistical_validation_suite_hash"] == artifact[
        "statistical_validation_suite_hash"
    ]
    assert master.read_bytes() == before_master
    assert all(path.read_bytes() == value for path, value in before_sources.items())


def test_validate_suite_rejects_overlap_contract_mismatch_and_incomplete_run(
    tmp_path, suite_stubs
):
    output_root = tmp_path / "runs"
    master = tmp_path / "master.sqlite"
    first_dir, first_manifest, _ = _make_complete_run(
        output_root,
        run_id="sonic_1111111111111111",
        post_id="101",
        master=master,
    )
    _, second_manifest, _ = _make_complete_run(
        output_root,
        run_id="sonic_2222222222222222",
        post_id="101",
        master=master,
    )
    with pytest.raises(runner.SonicAuditError, match="post IDs overlap"):
        runner._run_statistical_validation_suite(
            _args(output_root, tmp_path / "overlap.json", first_manifest["run_id"], second_manifest["run_id"])
        )

    output_root = tmp_path / "contract-runs"
    _, first_manifest, _ = _make_complete_run(
        output_root,
        run_id="sonic_3333333333333333",
        post_id="303",
        master=master,
    )
    second_dir, second_manifest, _ = _make_complete_run(
        output_root,
        run_id="sonic_4444444444444444",
        post_id="404",
        master=master,
        algorithm="different-algorithm",
    )
    with pytest.raises(runner.SonicAuditError, match="same feature schema"):
        runner._run_statistical_validation_suite(
            _args(output_root, tmp_path / "contract.json", first_manifest["run_id"], second_manifest["run_id"])
        )

    state = json.loads((second_dir / "state.json").read_text(encoding="utf-8"))
    state["status"] = "sonic_incomplete"
    state = bind_hash({key: value for key, value in state.items() if key != "state_hash"}, "state_hash")
    runner._atomic_write_json(second_dir / "state.json", state)
    with pytest.raises(runner.SonicAuditError, match="not complete"):
        runner._run_statistical_validation_suite(
            _args(output_root, tmp_path / "incomplete.json", first_manifest["run_id"], second_manifest["run_id"])
        )


def test_validate_suite_refuses_existing_and_protected_output_targets(
    tmp_path, suite_stubs
):
    output_root = tmp_path / "runs"
    master = tmp_path / "master.sqlite"
    first_dir, first_manifest, _ = _make_complete_run(
        output_root,
        run_id="sonic_1111111111111111",
        post_id="101",
        master=master,
    )
    _, second_manifest, _ = _make_complete_run(
        output_root,
        run_id="sonic_2222222222222222",
        post_id="202",
        master=master,
    )
    ids = (first_manifest["run_id"], second_manifest["run_id"])

    existing = tmp_path / "existing.json"
    existing.write_text("preserve me", encoding="utf-8")
    with pytest.raises(runner.SonicAuditError, match="already exists"):
        runner._run_statistical_validation_suite(_args(output_root, existing, *ids))
    assert existing.read_text(encoding="utf-8") == "preserve me"

    protected = first_dir / "new-suite.json"
    with pytest.raises(runner.SonicAuditError, match="protected run state"):
        runner._run_statistical_validation_suite(_args(output_root, protected, *ids))
    assert not protected.exists()

    with pytest.raises(runner.SonicAuditError, match="protected master state"):
        runner._run_statistical_validation_suite(_args(output_root, master, *ids))


def test_validate_suite_parser_and_minimum_run_gate(tmp_path):
    parser = runner.build_parser()
    parsed = parser.parse_args(
        [
            "--output-root",
            str(tmp_path),
            "validate-suite",
            "--run-id",
            "sonic_1111111111111111",
            "--run-id",
            "sonic_2222222222222222",
            "--file",
            str(tmp_path / "suite.json"),
        ]
    )
    assert parsed.command == "validate-suite"
    assert parsed.run_id == ["sonic_1111111111111111", "sonic_2222222222222222"]

    with pytest.raises(runner.SonicAuditError, match="at least two"):
        runner._run_statistical_validation_suite(
            _args(
                tmp_path,
                tmp_path / "one.json",
                "sonic_1111111111111111",
            )
        )
