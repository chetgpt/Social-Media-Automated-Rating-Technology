import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonic_audit.features as feature_module
import sonic_audit_tiktok as runner
from sonic_audit.contracts import (
    build_initial_state,
    build_run_manifest,
    canonical_sha256,
)


def candidate(post_id, reference_id=""):
    return {
        "post_id": post_id,
        "canonical_url": f"https://www.tiktok.com/@bankbca/video/{post_id}",
        "creator_handle": "bankbca",
        "content_type": "video",
        "base_snapshot_id": f"snapshot-{post_id}",
        "base_evidence_hash": "a" * 64,
        "base_observed_at": "2026-08-15T00:00:00+00:00",
        "reference_kind": "apple_track_id" if reference_id else "unresolved",
        "reference_id": reference_id,
        "reference_title": "Song" if reference_id else "",
        "reference_artist": "Artist" if reference_id else "",
        "reference_basis": "tt2dsp_exact_apple_id_resolution" if reference_id else "none",
        "reference_group_size": 2 if reference_id else 0,
        "selection_bucket": "repeated_catalog_reference" if reference_id else "unresolved_platform_sound",
        "platform_music_id": "music",
        "platform_music_title": "original sound - BankBCA",
        "platform_music_author": "BankBCA",
        "platform_music_original": True,
    }


class FakeTransport:
    def __init__(self):
        self.last_cleanup_verified = True
        self.last_run_timing = {
            "preflight_ms": 1.0,
            "attach_ms": 2.0,
            "transport_ms": 3.0,
            "total_run_ms": 4.0,
        }

    def cleanup_stale_run(self, run_id):
        assert run_id == "sonic_0123456789abcdef"
        return True

    def verify_no_run_residue(self, run_id):
        assert run_id == "sonic_0123456789abcdef"
        return True

    async def process_candidates(
        self,
        *,
        run_id,
        candidates,
        processor,
        expected_account,
        continue_on_item_error,
        error_processor,
        receipt_processor,
    ):
        assert run_id == "sonic_0123456789abcdef"
        assert continue_on_item_error is True
        receipts = []
        for index, row in enumerate(candidates):
            if index == len(candidates) - 1:
                identity = SimpleNamespace(post_id=row["post_id"], creator_handle="bankbca")
                derived = error_processor(identity, "exact_post_html_unavailable")
                receipt = SimpleNamespace(
                    post_id=row["post_id"],
                    status="unavailable",
                    error_code="exact_post_html_unavailable",
                    duration_seconds=0.0,
                    source_byte_count=0,
                    audio_byte_count=0,
                    source_sha256="",
                    audio_sha256="",
                    provenance={
                        "temporary_media_retained": False,
                        "account_handle": "kitascore",
                    },
                    timing={"total_item_ms": 5.0},
                    derived_result=derived,
                )
            else:
                item = SimpleNamespace(
                    post_id=row["post_id"],
                    audio_path=Path("transient.wav"),
                    audio_sha256="b" * 64,
                    duration_seconds=10.0,
                    source_byte_count=100,
                    audio_byte_count=200,
                    source_sha256="c" * 64,
                    provenance={
                        "temporary_media_retained": False,
                        "account_handle": "kitascore",
                    },
                )
                receipt = SimpleNamespace(
                    post_id=row["post_id"],
                    status="completed",
                    error_code="",
                    duration_seconds=10.0,
                    source_byte_count=100,
                    audio_byte_count=200,
                    source_sha256="c" * 64,
                    audio_sha256="b" * 64,
                    provenance={
                        "temporary_media_retained": False,
                        "account_handle": "kitascore",
                    },
                    timing={"total_item_ms": 6.0},
                    derived_result=processor(item),
                )
            receipt_processor(receipt)
            receipts.append(receipt)
        return receipts


def make_run(tmp_path, candidates):
    run_id = "sonic_0123456789abcdef"
    manifest = build_run_manifest(
        run_id=run_id,
        project="pilot",
        creator="bankbca",
        requested_count=len(candidates),
        master_database=tmp_path / "master.sqlite",
        candidates=candidates,
        transient_audio_authorized=True,
    )
    run_dir = tmp_path / run_id
    runner._atomic_write_json(run_dir / "manifest.json", manifest)
    runner._atomic_write_json(run_dir / "state.json", build_initial_state(manifest))
    return run_dir, manifest


def make_complete_run(tmp_path, candidates, *, master_database):
    run_dir, manifest = make_run(tmp_path, candidates)
    assert Path(manifest["master_database"]) == Path(master_database).resolve()
    for row in candidates:
        runner._write_terminal_record(
            run_dir,
            manifest,
            row,
            status="unavailable",
            transport={"cleanup_before_checkpoint": True},
            error="item_error:unavailable",
        )
    records = runner._load_records(run_dir, manifest)
    runner._atomic_write_json(
        run_dir / "state.json",
        runner._state_for_records(manifest, records, status="sonic_complete"),
    )
    return run_dir, manifest


def test_execute_checkpoints_after_cleanup_and_builds_report(tmp_path, monkeypatch):
    rows = [candidate("101", "track-a"), candidate("102", "track-a"), candidate("103")]
    run_dir, manifest = make_run(tmp_path, rows)

    monkeypatch.setattr(
        feature_module,
        "analyze_audio",
        lambda path, *, post_id, audio_sha256: {
            "schema_version": "sonic-feature-v1",
            "post_id": post_id,
            "record_hash": "d" * 64,
        },
    )
    monkeypatch.setattr(feature_module, "validate_feature_record", lambda value: None)
    monkeypatch.setattr(
        feature_module,
        "build_similarity_report",
        lambda values: {"schema_version": "sonic-similarity-report-v1", "status": "evaluated"},
    )
    monkeypatch.setattr(
        feature_module,
        "evaluate_clustering_stability",
        lambda values, *, n_clusters: {
            "schema_version": "sonic-cluster-stability-v1",
            "status": "evaluated",
            "n_clusters": n_clusters,
        },
    )

    status = asyncio.run(
        runner._execute(run_dir, manifest, transport=FakeTransport())
    )

    assert status["status"] == "sonic_complete"
    assert status["completed"] == 2
    assert status["unavailable"] == 1
    assert len(list((run_dir / "records").glob("*.json"))) == 3
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["transport_cleanup_verified"] is True
    assert report["run_timing"]["total_run_ms"] == 4.0
    assert report["retention_verified"] is True


def test_cli_requires_explicit_transient_audio_authorization(tmp_path):
    parser = runner.build_parser()
    args = parser.parse_args(
        [
            "--output-root",
            str(tmp_path),
            "run",
            "--project",
            "pilot",
            "--creator",
            "bankbca",
            "--posts",
            "1",
        ]
    )
    with pytest.raises(runner.SonicAuditError, match="authorize-transient-audio"):
        runner._new_run(args)


def test_mirelo_options_require_enable_upload_authorization_and_budget():
    parser = runner.build_parser()
    base = [
        "run",
        "--project",
        "pilot",
        "--creator",
        "bankbca",
        "--posts",
        "1",
        "--authorize-transient-audio",
    ]

    stray = parser.parse_args([*base, "--authorize-mirelo-upload"])
    with pytest.raises(runner.SonicAuditError, match="requires --mirelo"):
        runner._mirelo_config_from_args(stray)

    missing_authorization = parser.parse_args(
        [*base, "--mirelo-audio-to-midi", "--mirelo-max-credits", "10"]
    )
    with pytest.raises(runner.SonicAuditError, match="authorize-mirelo-upload"):
        runner._mirelo_config_from_args(missing_authorization)

    missing_budget = parser.parse_args(
        [*base, "--mirelo-audio-to-midi", "--authorize-mirelo-upload"]
    )
    with pytest.raises(runner.SonicAuditError, match="mirelo-max-credits"):
        runner._mirelo_config_from_args(missing_budget)

    enabled = parser.parse_args(
        [
            *base,
            "--mirelo-audio-to-midi",
            "--authorize-mirelo-upload",
            "--mirelo-max-credits",
            "25",
        ]
    )
    config = runner._mirelo_config_from_args(enabled)
    assert config["provider"] == "mirelo"
    assert config["max_run_credits"] == 25
    assert config["credential_environment_variable"] == "MIRELO_API_KEY"
    assert config["changes_primary_recording_score"] is False
    assert runner.verify_hash(config, "config_hash")


def test_mirelo_key_loader_rejects_missing_or_malformed_values(monkeypatch):
    monkeypatch.setattr(runner.os, "name", "posix")
    monkeypatch.delenv("MIRELO_API_KEY", raising=False)
    with pytest.raises(runner.SonicAuditError, match="missing or invalid"):
        runner._load_mirelo_api_key()
    monkeypatch.setenv("MIRELO_API_KEY", "not-a-key")
    with pytest.raises(runner.SonicAuditError, match="missing or invalid"):
        runner._load_mirelo_api_key()
    monkeypatch.setenv("MIRELO_API_KEY", "sk-" + "x" * 50)
    assert runner._load_mirelo_api_key().startswith("sk-")


def test_unimplemented_mirelo_fails_before_registry_or_run_creation(tmp_path, monkeypatch):
    parser = runner.build_parser()
    args = parser.parse_args(
        [
            "--output-root",
            str(tmp_path / "runs"),
            "run",
            "--project",
            "pilot",
            "--creator",
            "bankbca",
            "--posts",
            "1",
            "--authorize-transient-audio",
            "--mirelo-audio-to-midi",
            "--authorize-mirelo-upload",
            "--mirelo-max-credits",
            "10",
        ]
    )
    monkeypatch.setattr(
        runner,
        "load_creator_registry_candidates",
        lambda *values, **options: pytest.fail("registry must not be read"),
    )

    with pytest.raises(runner.SonicAuditError, match="not implemented"):
        runner._new_run(args)
    assert not (tmp_path / "runs").exists()


def test_unimplemented_mirelo_plan_batch_fails_before_plan_or_run_creation(
    tmp_path,
    monkeypatch,
):
    parser = runner.build_parser()
    args = parser.parse_args(
        [
            "--output-root",
            str(tmp_path / "runs"),
            "run-plan-batch",
            "--project",
            "pilot",
            "--plan-file",
            str(tmp_path / "missing-plan.json"),
            "--batch-id",
            "sonic_batch_0123456789abcdef",
            "--authorize-transient-audio",
            "--mirelo-audio-to-midi",
            "--authorize-mirelo-upload",
            "--mirelo-max-credits",
            "10",
        ]
    )
    monkeypatch.setattr(
        runner,
        "_read_json",
        lambda *values, **options: pytest.fail("plan must not be read"),
    )

    with pytest.raises(runner.SonicAuditError, match="not implemented"):
        runner._new_plan_batch_run(args)
    assert not (tmp_path / "runs").exists()


def test_mirelo_credit_reservation_is_durable_and_budget_bounded(tmp_path):
    args = runner.build_parser().parse_args(
        [
            "run",
            "--project",
            "pilot",
            "--creator",
            "bankbca",
            "--posts",
            "2",
            "--authorize-transient-audio",
            "--mirelo-audio-to-midi",
            "--authorize-mirelo-upload",
            "--mirelo-max-credits",
            "5",
        ]
    )
    config = runner._mirelo_config_from_args(args)
    manifest = build_run_manifest(
        run_id="sonic_0123456789abcdef",
        project="pilot",
        creator="bankbca",
        requested_count=2,
        master_database=tmp_path / "master.sqlite",
        candidates=[candidate("101"), candidate("102")],
        transient_audio_authorized=True,
        third_party_audio_upload_authorized=True,
        symbolic_analysis=config,
    )
    run_dir = tmp_path / manifest["run_id"]
    runner._atomic_write_json(run_dir / "manifest.json", manifest)
    runner._atomic_write_json(
        run_dir / runner.MIRELO_CREDIT_LEDGER_FILE,
        runner._initial_mirelo_credit_ledger(manifest),
    )

    assert runner._reserve_mirelo_credits(
        run_dir,
        manifest,
        post_id="101",
        audio_sha256="b" * 64,
        quoted_credits=4,
        preflight_hash="c" * 64,
    ) is True
    assert runner._reserve_mirelo_credits(
        run_dir,
        manifest,
        post_id="101",
        audio_sha256="b" * 64,
        quoted_credits=4,
        preflight_hash="c" * 64,
    ) is False
    assert runner._reserve_mirelo_credits(
        run_dir,
        manifest,
        post_id="102",
        audio_sha256="d" * 64,
        quoted_credits=2,
        preflight_hash="e" * 64,
    ) is False

    ledger = runner._load_mirelo_credit_ledger(run_dir, manifest)
    assert ledger["reserved_credit_total"] == 4
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["status"] == "reserved"


def test_cli_accepts_repeatable_excluded_run_ids(tmp_path):
    parser = runner.build_parser()
    args = parser.parse_args(
        [
            "--output-root",
            str(tmp_path),
            "run",
            "--project",
            "pilot",
            "--creator",
            "bankbca",
            "--posts",
            "2",
            "--exclude-run-id",
            "sonic_0123456789abcdef",
            "--exclude-run-id",
            "sonic_fedcba9876543210",
            "--authorize-transient-audio",
        ]
    )

    assert args.exclude_run_id == [
        "sonic_0123456789abcdef",
        "sonic_fedcba9876543210",
    ]


def test_run_plan_batch_binds_plan_and_preserves_typed_reference(tmp_path, monkeypatch):
    master = tmp_path / "master.sqlite"
    master.touch()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "tiktok-sonic-audit-corpus-plan-v1",
                "source_scope": {"scope_hash": "1" * 64},
                "candidate_set_hash": "2" * 64,
            }
        ),
        encoding="utf-8",
    )
    batch_id = "sonic_batch_0123456789abcdef"
    frozen = {
        "post_id": "101",
        "canonical_url": "https://www.tiktok.com/@maker/video/101",
        "creator_handle": "maker",
        "content_type": "video",
        "base_snapshot_id": "snapshot-101",
        "base_evidence_hash": "a" * 64,
        "base_observed_at": "2026-08-01T00:00:00+00:00",
        "reference_provider": "apple_itunes_lookup",
        "reference_storefront": "ID",
        "reference_track_id": "110",
        "reference_label": "apple_itunes_lookup:ID:110",
        "reference_title": "Track 110",
        "reference_artist": "Artist",
        "music_observation_id": "observation-101",
        "music_observation_hash": "3" * 64,
        "music_evidence_hash": "4" * 64,
        "tt2dsp_resolution_hash": "5" * 64,
        "candidate_hash": "6" * 64,
        "group_id": "sonic_group_0123456789abcdef",
        "batch_id": batch_id,
        "batch_position": 1,
        "selection_position": 1,
    }
    batch = {
        "batch_id": batch_id,
        "batch_hash": "7" * 64,
        "candidate_set_hash": "8" * 64,
        "creator_handle": "maker",
        "group_hashes": ["9" * 64],
    }
    group = {
        "group_id": frozen["group_id"],
        "group_hash": "9" * 64,
        "selected_count": 2,
    }
    calls = []

    def validate(plan, *, batch_id, master_database):
        calls.append((dict(plan), batch_id, Path(master_database)))
        return {
            "plan_hash": "b" * 64,
            "master_database": str(master.resolve()),
            "batch": batch,
            "candidates": [frozen],
            "groups_by_id": {group["group_id"]: group},
        }

    captured = {}

    async def execute(run_dir, manifest, *, expected_account):
        captured.update(
            {"run_dir": run_dir, "manifest": manifest, "account": expected_account}
        )
        runner._write_terminal_record(
            run_dir,
            manifest,
            manifest["candidates"][0],
            status="unavailable",
            transport={"cleanup_before_checkpoint": True},
            error="item_error:unavailable",
        )
        return {"run_id": manifest["run_id"], "status": "sonic_complete"}

    monkeypatch.setattr(runner, "validate_corpus_plan_batch", validate)
    monkeypatch.setattr(runner, "_execute", execute)
    args = runner.build_parser().parse_args(
        [
            "--master-database",
            str(master),
            "--output-root",
            str(tmp_path / "runs"),
            "run-plan-batch",
            "--project",
            "corpus_batch",
            "--plan-file",
            str(plan_path),
            "--batch-id",
            batch_id,
            "--expected-account",
            "@KitaScore",
            "--authorize-transient-audio",
        ]
    )

    result = runner._new_plan_batch_run(args)
    manifest = captured["manifest"]
    selected = manifest["candidates"][0]
    context = manifest["selection_context"]
    record = json.loads(
        (captured["run_dir"] / "records" / "101.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "sonic_complete"
    assert calls[0][1:] == (batch_id, master.resolve())
    assert manifest["selection_method"] == "positive-pair-corpus-plan-batch-v1"
    assert context["corpus_plan_hash"] == "b" * 64
    assert context["batch_hash"] == "7" * 64
    assert context["expected_account"] == "kitascore"
    assert context["master_bindings_revalidated_before_browser"] is True
    assert selected["reference_id"] == "110"
    assert selected["reference_label"] == "apple_itunes_lookup:ID:110"
    assert selected["reference_title"] == "Track 110"
    assert selected["reference_artist"] == "Artist"
    assert selected["plan_candidate"] == frozen
    assert selected["plan_candidate_hash"] == "6" * 64
    assert "candidate_hash" not in selected
    assert captured["account"] == "kitascore"
    assert record["reference"]["reference_label"] == "apple_itunes_lookup:ID:110"
    assert record["selection_provenance"] == {
        "corpus_plan_hash": "b" * 64,
        "corpus_batch_id": batch_id,
        "corpus_batch_hash": "7" * 64,
        "plan_candidate_hash": "6" * 64,
        "group_id": frozen["group_id"],
        "corpus_group_hash": "9" * 64,
        "batch_position": 1,
        "selection_position": 1,
    }
    with pytest.raises(runner.SonicAuditError, match="already has run"):
        runner._new_plan_batch_run(args)


def test_run_plan_batch_requires_authorization_before_plan_validation(tmp_path, monkeypatch):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    called = False

    def validate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("validation must not run without authorization")

    monkeypatch.setattr(runner, "validate_corpus_plan_batch", validate)
    args = runner.build_parser().parse_args(
        [
            "--output-root",
            str(tmp_path / "runs"),
            "run-plan-batch",
            "--project",
            "corpus_batch",
            "--plan-file",
            str(plan_path),
            "--batch-id",
            "sonic_batch_0123456789abcdef",
        ]
    )
    with pytest.raises(runner.SonicAuditError, match="authorize-transient-audio"):
        runner._new_plan_batch_run(args)
    assert called is False
    assert not (tmp_path / "runs").exists()


def test_run_plan_batch_validation_failure_precedes_run_creation(tmp_path, monkeypatch):
    master = tmp_path / "master.sqlite"
    master.touch()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    executed = False

    def validate(*args, **kwargs):
        raise runner.CorpusPlanningError("candidate binding changed")

    async def execute(*args, **kwargs):
        nonlocal executed
        executed = True

    monkeypatch.setattr(runner, "validate_corpus_plan_batch", validate)
    monkeypatch.setattr(runner, "_execute", execute)
    args = runner.build_parser().parse_args(
        [
            "--master-database",
            str(master),
            "--output-root",
            str(tmp_path / "runs"),
            "run-plan-batch",
            "--project",
            "corpus_batch",
            "--plan-file",
            str(plan_path),
            "--batch-id",
            "sonic_batch_0123456789abcdef",
            "--authorize-transient-audio",
        ]
    )
    with pytest.raises(runner.SonicAuditError, match="candidate_binding_changed"):
        runner._new_plan_batch_run(args)
    assert executed is False
    assert not (tmp_path / "runs").exists()


def test_plan_bound_candidate_index_revalidates_inner_candidate_hash():
    frozen = runner.bind_hash(
        {
            "post_id": "101",
            "canonical_url": "https://www.tiktok.com/@maker/video/101",
            "creator_handle": "maker",
            "content_type": "video",
            "base_snapshot_id": "snapshot-101",
            "base_evidence_hash": "a" * 64,
            "music_observation_id": "observation-101",
            "music_observation_hash": "b" * 64,
            "music_evidence_hash": "c" * 64,
            "tt2dsp_resolution_hash": "d" * 64,
            "reference_provider": "apple_itunes_lookup",
            "reference_storefront": "ID",
            "reference_track_id": "110",
            "reference_label": "apple_itunes_lookup:ID:110",
            "reference_title": "Track 110",
            "reference_artist": "Artist",
            "group_id": "sonic_group_0123456789abcdef",
            "batch_id": "sonic_batch_0123456789abcdef",
            "batch_position": 1,
            "selection_position": 1,
        },
        "candidate_hash",
    )
    runtime = dict(frozen)
    runtime["plan_candidate"] = dict(frozen)
    runtime["plan_candidate_hash"] = frozen["candidate_hash"]
    runtime.pop("candidate_hash")
    runtime.update(
        {
            "reference_kind": "apple_track_id",
            "reference_id": "110",
            "corpus_plan_hash": "e" * 64,
        }
    )
    manifest = runner.build_run_manifest(
        run_id="sonic_0123456789abcdef",
        project="corpus",
        creator="maker",
        requested_count=1,
        master_database="master.sqlite",
        candidates=[runtime],
        transient_audio_authorized=True,
        selection_method="positive-pair-corpus-plan-batch-v1",
    )
    assert runner._candidate_index(manifest)["101"]["reference_label"] == (
        "apple_itunes_lookup:ID:110"
    )

    tampered = dict(manifest)
    tampered_candidate = dict(runtime)
    tampered_frozen = dict(frozen)
    tampered_frozen["reference_title"] = "Changed"
    tampered_candidate["plan_candidate"] = tampered_frozen
    tampered["candidates"] = [tampered_candidate]
    with pytest.raises(runner.SonicAuditError, match="candidate hash is invalid"):
        runner._candidate_index(tampered)


def test_plan_bound_resume_rejects_changed_expected_account(monkeypatch, tmp_path):
    manifest = {
        "selection_context": {"expected_account": "kitascore"},
    }
    monkeypatch.setattr(
        runner,
        "_load_run",
        lambda output_root, run_id: (tmp_path / run_id, manifest, {}),
    )
    args = SimpleNamespace(
        output_root=tmp_path,
        run_id="sonic_0123456789abcdef",
        expected_account="someone_else",
    )
    with pytest.raises(runner.SonicAuditError, match="expected-account binding"):
        runner._resume(args)


def test_plan_corpus_cli_writes_only_a_new_offline_plan(tmp_path, monkeypatch):
    captured = {}

    def fake_plan(master_database, **kwargs):
        captured["master_database"] = Path(master_database)
        captured.update(kwargs)
        return runner.bind_hash(
            {
                "schema_version": "tiktok-sonic-audit-corpus-plan-v1",
                "source_scope": runner.bind_hash(
                    {"mode": "creators", "creators": ["bankbca"]},
                    "scope_hash",
                ),
                "selected_candidate_count": 4,
                "selected_repeated_group_count": 2,
                "selected_positive_pair_count": 2,
                "batches": [{"batch_id": "sonic_batch_example"}],
            },
            "plan_hash",
        )

    monkeypatch.setattr(runner, "plan_corpus", fake_plan)
    target = tmp_path / "plans" / "plan.json"
    args = runner.build_parser().parse_args(
        [
            "--master-database",
            str(tmp_path / "master.sqlite"),
            "--output-root",
            str(tmp_path / "runs"),
            "plan-corpus",
            "--creator",
            "@bankbca",
            "--min-repeated-groups",
            "2",
            "--min-positive-pairs",
            "2",
            "--max-posts",
            "10",
            "--max-posts-per-reference",
            "4",
            "--file",
            str(target),
        ]
    )

    result = runner._plan_corpus_artifact(args)
    artifact = json.loads(target.read_text(encoding="utf-8"))

    assert result["selected_posts"] == 4
    assert result["browser_access_performed"] is False
    assert result["media_access_performed"] is False
    assert result["master_registry_mutated"] is False
    assert captured["creators"] == ["@bankbca"]
    assert captured["excluded_post_ids"] == []
    assert artifact["source_scope"]["excluded_runs"] == []
    assert runner.verify_hash(artifact["source_scope"], "scope_hash")
    assert runner.verify_hash(artifact, "plan_hash")
    with pytest.raises(runner.SonicAuditError, match="already exists"):
        runner._plan_corpus_artifact(args)

    ancestor_target = tmp_path / "future"
    args.file = str(ancestor_target)
    args.output_root = str(ancestor_target / "runs")
    with pytest.raises(runner.SonicAuditError, match="overlaps_run_state"):
        runner._plan_corpus_artifact(args)
    assert not ancestor_target.exists()


def test_plan_corpus_cli_does_not_accept_audio_authorization(tmp_path):
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            [
                "plan-corpus",
                "--creator",
                "bankbca",
                "--min-repeated-groups",
                "1",
                "--min-positive-pairs",
                "1",
                "--max-posts",
                "2",
                "--max-posts-per-reference",
                "2",
                "--file",
                str(tmp_path / "plan.json"),
                "--authorize-transient-audio",
            ]
        )


def test_excluded_run_context_is_complete_creator_and_database_bound(tmp_path):
    master_database = tmp_path / "master.sqlite"
    master_database.touch()
    rows = [candidate("101", "track-a"), candidate("102")]
    _, manifest = make_complete_run(
        tmp_path,
        rows,
        master_database=master_database,
    )

    excluded, context = runner._load_exclusion_context(
        tmp_path,
        [manifest["run_id"]],
        creator="@bankbca",
        master_database=master_database,
    )

    assert excluded == {"101", "102"}
    assert context["excluded_candidate_count"] == 2
    assert context["excluded_candidate_set_hash"] == canonical_sha256(["101", "102"])
    assert context["excluded_runs"] == [
        {
            "run_id": manifest["run_id"],
            "manifest_hash": manifest["manifest_hash"],
            "candidate_count": 2,
            "candidate_set_hash": canonical_sha256(["101", "102"]),
        }
    ]

    with pytest.raises(runner.SonicAuditError, match="master database mismatch"):
        runner._load_exclusion_context(
            tmp_path,
            [manifest["run_id"]],
            creator="bankbca",
            master_database=tmp_path / "different.sqlite",
        )
    with pytest.raises(runner.SonicAuditError, match="creator mismatch"):
        runner._load_exclusion_context(
            tmp_path,
            [manifest["run_id"]],
            creator="someone_else",
            master_database=master_database,
        )


def test_excluded_run_must_be_sonic_complete(tmp_path):
    master_database = tmp_path / "master.sqlite"
    _, manifest = make_run(tmp_path, [candidate("101")])

    with pytest.raises(runner.SonicAuditError, match="not sonic_complete"):
        runner._load_exclusion_context(
            tmp_path,
            [manifest["run_id"]],
            creator="bankbca",
            master_database=master_database,
        )


def test_new_run_freezes_non_overlapping_selection_context(tmp_path, monkeypatch):
    master_database = tmp_path / "master.sqlite"
    master_database.touch()
    prior_rows = [candidate("101", "track-a"), candidate("102")]
    _, prior_manifest = make_complete_run(
        tmp_path,
        prior_rows,
        master_database=master_database,
    )
    current_rows = [
        *prior_rows,
        candidate("103", "track-b"),
        candidate("104", "track-c"),
        candidate("201"),
        candidate("202"),
    ]
    captured = {}

    async def fake_execute(run_dir, manifest, *, expected_account):
        captured["run_dir"] = run_dir
        captured["manifest"] = manifest
        captured["expected_account"] = expected_account
        return {"run_id": manifest["run_id"], "status": "test_only"}

    monkeypatch.setattr(
        runner,
        "load_creator_registry_candidates",
        lambda database, creator: current_rows,
    )
    monkeypatch.setattr(runner, "_execute", fake_execute)
    monkeypatch.setattr(
        runner.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="f" * 32),
    )
    args = runner.build_parser().parse_args(
        [
            "--master-database",
            str(master_database),
            "--output-root",
            str(tmp_path),
            "run",
            "--project",
            "extension",
            "--creator",
            "bankbca",
            "--posts",
            "3",
            "--exclude-run-id",
            prior_manifest["run_id"],
            "--authorize-transient-audio",
        ]
    )

    result = runner._new_run(args)
    manifest = captured["manifest"]

    assert result["status"] == "test_only"
    assert manifest["selection_method"] == "externally-labelled-first-non-overlapping-v1"
    assert [row["post_id"] for row in manifest["candidates"][:2]] == [
        row["post_id"] for row in manifest["candidates"] if row["reference_id"]
    ]
    assert {row["post_id"] for row in manifest["candidates"]}.isdisjoint({"101", "102"})
    assert manifest["selection_context"]["excluded_runs"][0]["run_id"] == prior_manifest[
        "run_id"
    ]
    assert manifest["selection_context"]["excluded_runs"][0][
        "manifest_hash"
    ] == prior_manifest["manifest_hash"]
    assert manifest["selection_context"]["excluded_candidate_count"] == 2


def test_status_and_export_do_not_need_browser(tmp_path):
    rows = [candidate("101")]
    run_dir, manifest = make_run(tmp_path, rows)
    runner._write_terminal_record(
        run_dir,
        manifest,
        rows[0],
        status="unavailable",
        transport={"cleanup_before_checkpoint": True},
        error="item_error:unavailable",
    )
    records = runner._load_records(run_dir, manifest)
    state = runner._state_for_records(manifest, records, status="sonic_complete")
    report = runner._build_report(manifest, records)
    runner._atomic_write_json(run_dir / "state.json", state)
    runner._atomic_write_json(run_dir / "report.json", report)

    args = SimpleNamespace(output_root=str(tmp_path), run_id=manifest["run_id"])
    assert runner._status(args)["pending"] == 0
    validated = runner._run_statistical_validation(args)
    assert validated["media_access_performed"] is False
    assert validated["ai_analysis_performed"] is False
    validation_path = run_dir / runner.STATISTICAL_VALIDATION_FILE
    original_validation = validation_path.read_bytes()
    assert runner._run_statistical_validation(args)["statistical_validation_hash"] == validated[
        "statistical_validation_hash"
    ]
    assert validation_path.read_bytes() == original_validation
    assert runner._status(args)["statistical_validation_hash"] == validated[
        "statistical_validation_hash"
    ]
    target = tmp_path.parent / "sonic-export.json"
    args.file = str(target)
    exported = runner._export(args)
    assert exported["records"] == 1
    assert target.is_file()
    envelope = json.loads(target.read_text(encoding="utf-8"))
    assert envelope["statistical_validation"]["statistical_validation_hash"] == validated[
        "statistical_validation_hash"
    ]


def test_statistical_validation_tamper_is_rejected(tmp_path):
    rows = [candidate("101")]
    run_dir, manifest = make_run(tmp_path, rows)
    runner._write_terminal_record(
        run_dir,
        manifest,
        rows[0],
        status="unavailable",
        transport={"cleanup_before_checkpoint": True},
        error="item_error:unavailable",
    )
    records = runner._load_records(run_dir, manifest)
    state = runner._state_for_records(manifest, records, status="sonic_complete")
    report = runner._build_report(manifest, records)
    runner._atomic_write_json(run_dir / "state.json", state)
    runner._atomic_write_json(run_dir / "report.json", report)
    args = SimpleNamespace(output_root=str(tmp_path), run_id=manifest["run_id"])
    runner._run_statistical_validation(args)
    path = run_dir / runner.STATISTICAL_VALIDATION_FILE
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["media_access_performed"] = True
    runner._atomic_write_json(path, artifact)

    with pytest.raises(runner.SonicAuditError, match="invalid SONIC AUDIT statistical"):
        runner._run_statistical_validation(args)
