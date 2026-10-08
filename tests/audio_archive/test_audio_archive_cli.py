"""Offline tests for the durable AUDIO ARCHIVE runner.

No test in this module starts Edge, performs a TikTok request, or invokes
ffmpeg. The fake transport writes deterministic bytes to the exact frozen
destinations and reports the same dual-format receipt contract as the real
transport adapter.
"""

import asyncio
import builtins
import copy
import datetime as dt
import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_archive_transport as transport_module
import audio_archive_tiktok as runner
import tiktok_scraper.run_lineage as run_lineage


POST_A = "7673070116011216148"
POST_B = "7673070116011216149"
CREATOR = "bankbca"
RUN_ID = "audio_archive_test_creator_bankbca_2p_20260829_010203_123456"


@pytest.fixture(autouse=True)
def _bind_canonical_test_archive_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner,
        "CANONICAL_OUTPUT_ROOT",
        tmp_path / "archive-runs",
    )


def _timestamp(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _lineage(post_ids=(POST_A,), *, content_types=None):
    content_types = content_types or ("video",) * len(post_ids)
    candidates = []
    for ordinal, (post_id, content_type) in enumerate(
        zip(post_ids, content_types), start=1
    ):
        candidates.append(
            {
                "ordinal": ordinal,
                "post_id": post_id,
                "canonical_url": (
                    f"https://www.tiktok.com/@{CREATOR}/{content_type}/{post_id}"
                ),
                "creator_handle": CREATOR,
                "content_type": content_type,
                "snapshot_id": f"snapshot-{post_id}",
                "evidence_hash": hashlib.sha256(
                    f"evidence-{post_id}".encode("ascii")
                ).hexdigest(),
            }
        )
    return {
        "schema_version": "tiktok-completed-run-lineage-v1",
        "source_run": {
            "database_path": r"D:\fake\source.sqlite",
            "run_id": "engage_aaaaaaaaaaaaaaaa",
            "master_database_path": r"D:\fake\master.sqlite",
            "source_id": "1" * 32,
            "master_run_id": "2" * 32,
            "project": "music_audit_test_creator_bankbca_2p",
            "workflow": "listen",
            "status": "collection_complete",
            "source_mode": "creator",
            "collection_policy": "new_only",
            "requested_count": len(candidates),
            "evidence_ready": len(candidates),
            "expected_account": "operator",
        },
        "candidates": candidates,
        "lineage_hash": "3" * 64,
    }


def _run_fixture(
    tmp_path,
    *,
    post_ids=(POST_A,),
    content_types=None,
    storage_mode="permanent",
    created_at="2026-08-29T01:02:03Z",
    retention_days=None,
):
    lineage = _lineage(post_ids, content_types=content_types)
    candidates = runner._select_candidates(lineage, None)
    rights_basis = "owned" if storage_mode == "permanent" else "public-research"
    authorization = {
        "authorized": True,
        "authorized_by": "Alice Operator",
        "authorization_kind": "named_human_exact_run_audio_storage",
        "rights_basis": rights_basis,
        "storage_mode": storage_mode,
        "permission_carry_forward": False,
        "authorized_at": created_at,
        "scope": "all-evidence-ready",
        "selected_post_ids_hash": runner._json_hash(list(post_ids)),
    }
    manifest = runner._build_manifest(
        run_id=RUN_ID,
        lineage=lineage,
        candidates=candidates,
        authorization=authorization,
        storage_mode=storage_mode,
        retention_days=retention_days,
        expected_account="operator",
        created_at=created_at,
    )
    runner._validate_manifest(manifest)
    output_root = tmp_path / "archive-runs"
    run_dir = output_root / RUN_ID
    (run_dir / "records").mkdir(parents=True)
    (run_dir / "audio").mkdir()
    (run_dir / ".staging").mkdir()
    runner._atomic_write_text(
        run_dir / runner.RUN_LOCK_FILE,
        "tiktok-audio-archive-run-lock-v1",
    )
    runner._atomic_write_json(run_dir / "manifest.json", manifest)
    runner._atomic_write_json(
        run_dir / "state.json",
        runner._state_for_records(manifest, [], status="planned"),
    )
    return output_root, run_dir, manifest


def _paths(run_dir, candidate):
    return {
        format_name: run_dir / "audio" / filename
        for format_name, filename in candidate["audio_files"].items()
    }


def _file_receipt(format_name, path):
    payload = Path(path).read_bytes()
    if format_name == "m4a":
        mime_type, codec = "audio/mp4", "aac-lc"
    else:
        mime_type, codec = "audio/mpeg", "mp3"
    return {
        "format": format_name,
        "mime_type": mime_type,
        "codec": codec,
        "target_bitrate_kbps": 192,
        "sample_rate_hz": 44100,
        "channels": 2,
        "destination_path": Path(path),
        "byte_count": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "duration_seconds": 2.0 if format_name == "m4a" else 2.05,
    }


class FakeArchiveTransport:
    def __init__(
        self,
        *,
        unavailable=(),
        half_pair=False,
        fail_if_called=False,
        raise_after=None,
        before_receipt=None,
    ):
        self.unavailable = set(unavailable)
        self.half_pair = half_pair
        self.fail_if_called = fail_if_called
        self.raise_after = raise_after
        self.before_receipt = before_receipt
        self.calls = []

    async def archive_candidates(self, **kwargs):
        if self.fail_if_called:
            raise AssertionError("offline transport must not be called")
        self.calls.append(kwargs)
        receipts = []
        for candidate in kwargs["candidates"]:
            post_id = str(candidate["post_id"])
            pair = kwargs["destinations"][post_id]
            provenance = {
                "account_handle": "operator",
                "browser_profile": "Profile 7",
                "browser_mode": "existing_profile_attach",
                "source_video_retained": False,
                "signed_url_persisted": False,
            }
            if post_id in self.unavailable:
                receipt = SimpleNamespace(
                    post_id=post_id,
                    status="unavailable",
                    error_code="synthetic_unavailable",
                    files={},
                    duration_seconds=0.0,
                    transport_owner_run_id="sonic_synthetic",
                    provenance=provenance,
                    timing={},
                )
            else:
                pair["m4a"].write_bytes(f"m4a:{post_id}".encode("ascii"))
                files = {"m4a": _file_receipt("m4a", pair["m4a"])}
                if not self.half_pair:
                    pair["mp3"].write_bytes(f"mp3:{post_id}".encode("ascii"))
                    files["mp3"] = _file_receipt("mp3", pair["mp3"])
                receipt = SimpleNamespace(
                    post_id=post_id,
                    status="completed",
                    error_code="",
                    files=files,
                    duration_seconds=2.0,
                    transport_owner_run_id="sonic_synthetic",
                    provenance=provenance,
                    timing={},
                )
            if self.before_receipt is not None:
                self.before_receipt(post_id)
            await _maybe_await(kwargs["receipt_processor"](receipt))
            receipts.append(receipt)
            if self.raise_after is not None and len(receipts) >= self.raise_after:
                raise runner.AudioArchiveError("synthetic_interruption")
        return receipts


def _execute(run_dir, manifest, transport):
    return asyncio.run(
        runner._execute(
            run_dir,
            manifest,
            expected_account="operator",
            transport=transport,
        )
    )


def test_minimal_run_cli_has_no_authorization_rights_or_retention_gate():
    args = runner.build_parser().parse_args(
        [
            "run",
            "--source-database",
            "project.sqlite",
            "--source-run-id",
            "engage_aaaaaaaaaaaaaaaa",
        ]
    )
    assert args.post_id is None
    assert args.all_evidence_ready is False
    assert args.storage_mode == "archive"
    assert args.authorized_by == ""
    assert args.retention_days is None


def test_legacy_gate_options_accept_arbitrary_inert_values():
    args = runner.build_parser().parse_args(
        [
            "run",
            "--source-database",
            "project.sqlite",
            "--source-run-id",
            "engage_aaaaaaaaaaaaaaaa",
            "--storage-mode",
            "old-custom-mode",
            "--rights-basis",
            "user-requested-without-form",
            "--retention-days",
            "forever",
            "--authorized-by",
            "Gemini automation",
            "--authorize-audio-storage",
            "old-custom-attestation",
        ]
    )
    assert args.storage_mode == "old-custom-mode"
    assert args.rights_basis == "user-requested-without-form"
    assert args.retention_days == "forever"
    assert args.authorized_by == "Gemini automation"
    assert args.authorize_audio_storage == "old-custom-attestation"


def test_creation_request_metadata_ignores_all_legacy_gate_values():
    args = SimpleNamespace(
        authorized_by="Gemini automation",
        storage_mode="old-custom-mode",
        rights_basis="anything",
        retention_days="not-a-ttl",
        authorize_audio_storage="anything",
    )
    metadata = runner._validate_creation_authorization(args)
    assert metadata == {"authorization_kind": "user_requested_audio_archive"}


def test_new_run_uses_relaxed_local_source_without_master_or_terminal_gate(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "moved" / "legacy.sqlite"
    source.parent.mkdir()
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE engage_tiktok_runs (
                run_id TEXT PRIMARY KEY,
                project TEXT,
                workflow TEXT,
                status TEXT,
                master_database TEXT,
                expected_account TEXT
            );
            CREATE TABLE engage_tiktok_posts (
                run_id TEXT,
                post_id TEXT,
                url TEXT,
                evidence_ready INTEGER,
                evidence_json TEXT,
                evidence_hash TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO engage_tiktok_runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "engage_legacy",
                "legacy_brand",
                "listen",
                "collection_incomplete",
                r"D:\missing\old_master.sqlite",
                "different_old_account",
            ),
        )
        packet = {
            "post_id": POST_A,
            "url": f"https://www.tiktok.com/@{CREATOR}/video/{POST_A}",
        }
        connection.execute(
            "INSERT INTO engage_tiktok_posts VALUES (?, ?, ?, 1, ?, ?)",
            (
                "engage_legacy",
                POST_A,
                packet["url"],
                json.dumps(packet),
                hashlib.sha256(b"legacy").hexdigest(),
            ),
        )

    async def offline_execute(run_dir, manifest, **kwargs):
        return {
            "status": "planned_for_test",
            "run_id": manifest["run_id"],
            "run_directory": str(run_dir),
        }

    monkeypatch.setattr(runner, "_execute", offline_execute)
    result = runner._new_run(
        SimpleNamespace(
            source_database=str(source),
            source_run_id="engage_legacy",
            all_evidence_ready=False,
            post_id=None,
            expected_account="",
            output_root=str(tmp_path / "custom-archives"),
            storage_mode="old-custom-mode",
            rights_basis="no-form-required",
            retention_days="forever",
            authorized_by="Gemini automation",
            authorize_audio_storage="old-custom-attestation",
            master_database=str(tmp_path / "missing-master.sqlite"),
        )
    )
    manifest = json.loads(
        (Path(result["run_directory"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["storage"]["mode"] == "archive"
    assert manifest["storage"]["retention_days"] is None
    assert manifest["storage"]["expires_at"] == ""
    assert "legacy_authorized_by" not in manifest["authorization"]
    assert manifest["source"]["status_at_freeze"] == "collection_incomplete"
    assert manifest["selected_count"] == 1
    assert manifest["expected_account"] == ""


def test_one_post_archives_distinct_m4a_and_mp3_with_separate_hashes(tmp_path):
    _, run_dir, manifest = _run_fixture(tmp_path)
    result = _execute(run_dir, manifest, FakeArchiveTransport())
    assert result["status"] == "archive_complete"
    candidate = manifest["selection"][0]
    paths = _paths(run_dir, candidate)
    assert set(paths) == {"m4a", "mp3"}
    assert all(path.is_file() for path in paths.values())

    record = json.loads(
        (run_dir / "records" / candidate["record_file"]).read_text(encoding="utf-8")
    )
    assert record["status"] == "archived"
    assert set(record["audio_files"]) == {"m4a", "mp3"}
    for format_name, path in paths.items():
        metadata = record["audio_files"][format_name]
        assert metadata["file"] == path.name
        assert metadata["byte_count"] == path.stat().st_size
        assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["audio_files"]["m4a"]["sha256"] != record["audio_files"]["mp3"]["sha256"]
    assert result["audio_files_present"] == 2


def test_unavailable_post_checkpoints_no_audio_and_later_post_continues(tmp_path):
    _, run_dir, manifest = _run_fixture(tmp_path, post_ids=(POST_A, POST_B))
    result = _execute(
        run_dir,
        manifest,
        FakeArchiveTransport(unavailable={POST_A}),
    )
    assert result["archived"] == 1
    assert result["unavailable"] == 1
    assert not any(path.exists() for path in _paths(run_dir, manifest["selection"][0]).values())
    assert all(path.is_file() for path in _paths(run_dir, manifest["selection"][1]).values())
    first_record = json.loads(
        (
            run_dir
            / "records"
            / manifest["selection"][0]["record_file"]
        ).read_text(encoding="utf-8")
    )
    assert first_record["status"] == "unavailable"
    assert first_record["audio_files"] == {}


def test_photo_post_is_terminal_unavailable_without_transport(tmp_path):
    _, run_dir, manifest = _run_fixture(
        tmp_path,
        post_ids=(POST_A,),
        content_types=("photo",),
    )
    transport = FakeArchiveTransport(fail_if_called=True)
    result = _execute(run_dir, manifest, transport)
    assert result["status"] == "archive_complete"
    assert result["archived"] == 0
    assert result["unavailable"] == 1
    assert result["audio_files_present"] == 0
    assert transport.calls == []
    record = json.loads(
        (
            run_dir
            / "records"
            / manifest["selection"][0]["record_file"]
        ).read_text(encoding="utf-8")
    )
    assert record["error"] == "photo_posts_not_supported_by_audio_archive_v1"
    assert record["audio_files"] == {}


def test_more_than_sixty_posts_are_partitioned_without_truncation(tmp_path):
    post_ids = tuple(str(7600000000000000000 + offset) for offset in range(61))
    _, run_dir, manifest = _run_fixture(tmp_path, post_ids=post_ids)
    transport = FakeArchiveTransport()
    result = _execute(run_dir, manifest, transport)
    assert result["status"] == "archive_complete"
    assert result["selected"] == 61
    assert result["archived"] == 61
    assert result["audio_files_present"] == 122
    assert [len(call["candidates"]) for call in transport.calls] == [60, 1]
    assert len(list((run_dir / "records").glob("*.json"))) == 61
    assert len(list((run_dir / "audio").glob("*.m4a"))) == 61
    assert len(list((run_dir / "audio").glob("*.mp3"))) == 61


def test_runner_never_checkpoints_completed_receipt_missing_one_format(tmp_path):
    _, run_dir, manifest = _run_fixture(tmp_path)
    candidate = manifest["selection"][0]
    with pytest.raises(runner.AudioArchiveError):
        _execute(run_dir, manifest, FakeArchiveTransport(half_pair=True))
    assert not (run_dir / "records" / candidate["record_file"]).exists()
    assert not any(path.exists() for path in _paths(run_dir, candidate).values())


def test_resume_skips_valid_complete_pair_without_transport(monkeypatch, tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    lineage_checks = []
    monkeypatch.setattr(
        runner,
        "_revalidate_resume_lineage",
        lambda frozen: lineage_checks.append(frozen["manifest_hash"]),
    )
    result = runner._resume(
        SimpleNamespace(
            output_root=str(output_root),
            run_id=RUN_ID,
            expected_account="operator",
        )
    )
    assert result["status"] == "archive_complete"
    assert result["audio_files_present"] == 2
    assert lineage_checks == [manifest["manifest_hash"]]


def test_resume_repairs_state_cache_after_record_checkpoint_interruption(
    monkeypatch, tmp_path
):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    # Model an interruption after the no-clobber record commit but before the
    # derived state counters were atomically replaced.
    runner._atomic_write_json(
        run_dir / "state.json",
        runner._state_for_records(manifest, [], status="planned"),
    )
    monkeypatch.setattr(runner, "_revalidate_resume_lineage", lambda frozen: None)
    result = runner._resume(
        SimpleNamespace(
            output_root=str(output_root),
            run_id=RUN_ID,
            expected_account="operator",
        )
    )
    assert result["status"] == "archive_complete"
    assert result["terminal"] == 1
    assert result["audio_files_present"] == 2


def test_resume_refuses_state_ahead_of_missing_record(monkeypatch, tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    (run_dir / "records" / manifest["selection"][0]["record_file"]).unlink()
    monkeypatch.setattr(
        runner,
        "_revalidate_resume_lineage",
        lambda frozen: (_ for _ in ()).throw(AssertionError("must not reach lineage")),
    )

    with pytest.raises(runner.AudioArchiveError, match="state_counter_mismatch"):
        runner._resume(
            SimpleNamespace(
                output_root=str(output_root),
                run_id=RUN_ID,
                expected_account="operator",
            )
        )


def test_resume_hash_validates_and_skips_archived_pair_then_processes_pending(
    monkeypatch, tmp_path
):
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        post_ids=(POST_A, POST_B),
    )
    with pytest.raises(runner.AudioArchiveError, match="synthetic_interruption"):
        _execute(
            run_dir,
            manifest,
            FakeArchiveTransport(raise_after=1),
        )
    first_paths = _paths(run_dir, manifest["selection"][0])
    first_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in first_paths.items()
    }
    resumed_transport = FakeArchiveTransport()
    monkeypatch.setattr(runner, "_revalidate_resume_lineage", lambda frozen: None)
    monkeypatch.setattr(
        transport_module,
        "AudioArchiveTransport",
        lambda **kwargs: resumed_transport,
    )
    result = runner._resume(
        SimpleNamespace(
            output_root=str(output_root),
            run_id=RUN_ID,
            expected_account="operator",
        )
    )
    assert result["status"] == "archive_complete"
    assert result["archived"] == 2
    assert len(resumed_transport.calls) == 1
    assert [row["post_id"] for row in resumed_transport.calls[0]["candidates"]] == [
        POST_B
    ]
    assert {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in first_paths.items()
    } == first_hashes


def test_resume_does_not_require_source_database_revalidation(
    monkeypatch,
    tmp_path,
):
    _, _, manifest = _run_fixture(tmp_path)
    monkeypatch.setattr(
        run_lineage,
        "load_archive_run_posts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resume must use the frozen manifest")
        ),
    )

    runner._revalidate_resume_lineage(manifest)


def test_resume_uses_frozen_manifest_after_source_changes(
    monkeypatch,
    tmp_path,
):
    output_root, _, _ = _run_fixture(tmp_path)
    monkeypatch.setattr(
        run_lineage,
        "load_archive_run_posts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("source must not be reopened")
        ),
    )
    transport = FakeArchiveTransport()
    monkeypatch.setattr(
        transport_module,
        "AudioArchiveTransport",
        lambda **kwargs: transport,
    )

    result = runner._resume(
        SimpleNamespace(
            output_root=str(output_root),
            run_id=RUN_ID,
            expected_account="operator",
        )
    )
    assert result["status"] == "archive_complete"
    assert len(transport.calls) == 1


def test_resume_removes_hard_crash_half_pair_before_reacquisition(
    monkeypatch,
    tmp_path,
):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    candidate = manifest["selection"][0]
    paths = _paths(run_dir, candidate)
    paths["m4a"].write_bytes(b"orphan-from-interrupted-first-promotion")
    monkeypatch.setattr(runner, "_revalidate_resume_lineage", lambda frozen: None)
    transport = FakeArchiveTransport()
    monkeypatch.setattr(
        transport_module,
        "AudioArchiveTransport",
        lambda **kwargs: transport,
    )

    result = runner._resume(
        SimpleNamespace(
            output_root=str(output_root),
            run_id=RUN_ID,
            expected_account="operator",
        )
    )
    assert result["status"] == "archive_complete"
    assert len(transport.calls) == 1
    assert paths["m4a"].read_bytes() != b"orphan-from-interrupted-first-promotion"
    assert paths["mp3"].is_file()


@pytest.mark.parametrize("format_name, mutation", [("m4a", "delete"), ("mp3", "tamper")])
def test_resume_rejects_missing_or_tampered_half_pair_before_transport(
    monkeypatch, tmp_path, format_name, mutation
):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    path = _paths(run_dir, manifest["selection"][0])[format_name]
    if mutation == "delete":
        path.unlink()
    else:
        path.write_bytes(b"tampered")
    monkeypatch.setattr(runner, "_revalidate_resume_lineage", lambda frozen: None)
    with pytest.raises(runner.AudioArchiveError, match="audio|file|pair|hash"):
        runner._resume(
            SimpleNamespace(
                output_root=str(output_root),
                run_id=RUN_ID,
                expected_account="operator",
            )
        )


def test_status_and_validate_are_strictly_offline(monkeypatch, tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    real_import = builtins.__import__

    def offline_import(name, *args, **kwargs):
        if name in {"audio_archive_transport", "social_browser", "sonic_audio_transport"}:
            raise AssertionError(f"offline command imported browser transport: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", offline_import)
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    status = runner._status(args)
    validation = runner._validate(args)
    assert status["status"] == "archive_complete"
    assert validation["valid"] is True
    assert validation["verified_audio_files"] == 2


def test_second_operation_fails_closed_while_run_lock_is_held(tmp_path):
    output_root, run_dir, _ = _run_fixture(tmp_path)
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    with runner._run_lock(run_dir):
        with pytest.raises(runner.AudioArchiveError, match="run_locked"):
            runner._status(args)


def test_validate_rejects_audio_directory_symlink_without_touching_target(tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    audio_dir = run_dir / "audio"
    for path in audio_dir.iterdir():
        path.unlink()
    audio_dir.rmdir()
    try:
        audio_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this Windows host")
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    with pytest.raises(runner.AudioArchiveError, match="audio_directory_invalid"):
        runner._validate(args)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_validate_rejects_records_directory_symlink(tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    records_dir = run_dir / "records"
    outside = tmp_path / "outside-records"
    records_dir.rename(outside)
    try:
        records_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        outside.rename(records_dir)
        pytest.skip("directory symlinks are not available on this Windows host")

    with pytest.raises(runner.AudioArchiveError, match="records_directory_invalid"):
        runner._validate(SimpleNamespace(output_root=str(output_root), run_id=RUN_ID))


def test_validate_rejects_symlinked_review_markdown(tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    review_path = run_dir / "review.md"
    outside = tmp_path / "outside-review.md"
    outside.write_bytes(review_path.read_bytes())
    review_path.unlink()
    try:
        review_path.symlink_to(outside)
    except OSError:
        review_path.write_bytes(outside.read_bytes())
        pytest.skip("file symlinks are not available on this Windows host")

    with pytest.raises(runner.AudioArchiveError, match="unsafe_run_root_artifact"):
        runner._validate(SimpleNamespace(output_root=str(output_root), run_id=RUN_ID))


@pytest.mark.parametrize(
    ("relative_path", "expected_error"),
    [
        ("unexpected.txt", "unexpected_run_root_artifact"),
        ("records/unexpected.json", "unexpected_or_unsafe_record_artifact"),
    ],
)
def test_validate_rejects_unexpected_exact_layout_artifacts(
    tmp_path,
    relative_path,
    expected_error,
):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    artifact = run_dir / relative_path
    artifact.write_text("unexpected", encoding="utf-8")

    with pytest.raises(runner.AudioArchiveError, match=expected_error):
        runner._validate(SimpleNamespace(output_root=str(output_root), run_id=RUN_ID))


def test_custom_output_root_is_supported(tmp_path):
    other_root = tmp_path / "other-archive-root"
    other_root.mkdir()
    assert runner._canonical_output_root(other_root) == other_root.resolve()


def test_default_relative_master_database_resolves_to_workspace_canonical_path(
    monkeypatch,
    tmp_path,
):
    master = tmp_path / runner.DEFAULT_MASTER_DATABASE
    master.parent.mkdir(parents=True)
    master.write_bytes(b"sqlite-placeholder")
    monkeypatch.setattr(runner, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(runner, "CANONICAL_MASTER_DATABASE", master.resolve())

    assert runner._canonical_master_database(
        runner.DEFAULT_MASTER_DATABASE
    ) == master.resolve()


def test_manifest_does_not_gate_legacy_authorization_fields(tmp_path):
    _, _, manifest = _run_fixture(tmp_path)
    mutated = copy.deepcopy(manifest)
    mutated["authorization"]["permission_carry_forward"] = True
    mutated = runner._bind_hash(mutated, "manifest_hash")
    runner._validate_manifest(mutated)


def test_standard_archive_manifest_has_no_expiry_or_retention_gate(tmp_path):
    _, _, manifest = _run_fixture(tmp_path, storage_mode="archive")
    mutated = copy.deepcopy(manifest)
    mutated["storage"]["expires_at"] = ""
    mutated["storage"]["retention_days"] = None
    mutated = runner._bind_hash(mutated, "manifest_hash")
    runner._validate_manifest(mutated)


def test_validate_rejects_each_tampered_or_missing_format(tmp_path):
    output_root, run_dir, manifest = _run_fixture(tmp_path)
    _execute(run_dir, manifest, FakeArchiveTransport())
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    paths = _paths(run_dir, manifest["selection"][0])
    paths["mp3"].write_bytes(b"tampered")
    with pytest.raises(runner.AudioArchiveError, match="hash"):
        runner._validate(args)


def test_legacy_research_expiry_does_not_interrupt_archive(
    monkeypatch,
    tmp_path,
):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    clock = {"now": created}
    monkeypatch.setattr(runner, "_now", lambda: clock["now"])
    _, run_dir, manifest = _run_fixture(
        tmp_path,
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )

    def expire(_post_id):
        clock["now"] = created + dt.timedelta(days=2)

    result = _execute(
        run_dir,
        manifest,
        FakeArchiveTransport(before_receipt=expire),
    )
    candidate = manifest["selection"][0]
    assert result["status"] == "archive_complete"
    assert (run_dir / "records" / candidate["record_file"]).is_file()
    assert all(path.is_file() for path in _paths(run_dir, candidate).values())


def test_expiry_purge_verifies_and_deletes_both_formats(monkeypatch, tmp_path):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(runner, "_now", lambda: created)
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )
    _execute(run_dir, manifest, FakeArchiveTransport())
    paths = _paths(run_dir, manifest["selection"][0])
    assert all(path.is_file() for path in paths.values())

    monkeypatch.setattr(runner, "_now", lambda: created + dt.timedelta(days=2))
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    result = runner._purge_expired(args)
    assert result["purged"] is True
    assert result["purged_audio_files"] == 2
    assert not any(path.exists() for path in paths.values())
    purge = json.loads((run_dir / "purge.json").read_text(encoding="utf-8"))
    assert purge["status"] == "purged"
    assert {entry["format"] for entry in purge["entries"]} == {"m4a", "mp3"}
    validation = runner._validate(args)
    assert validation["valid"] is True
    assert validation["verified_audio_files"] == 0
    assert validation["purged"] is True


def test_purge_refuses_to_delete_any_file_when_one_hash_is_wrong(monkeypatch, tmp_path):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(runner, "_now", lambda: created)
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )
    _execute(run_dir, manifest, FakeArchiveTransport())
    paths = _paths(run_dir, manifest["selection"][0])
    paths["mp3"].write_bytes(b"tampered")
    monkeypatch.setattr(runner, "_now", lambda: created + dt.timedelta(days=2))
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    with pytest.raises(runner.AudioArchiveError, match="purge_audio_hash_mismatch"):
        runner._purge_expired(args)
    # The two-pass purge verifies the whole set before deleting either member.
    assert all(path.exists() for path in paths.values())


def test_initial_purge_requires_both_files_before_writing_plan(monkeypatch, tmp_path):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(runner, "_now", lambda: created)
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )
    _execute(run_dir, manifest, FakeArchiveTransport())
    paths = _paths(run_dir, manifest["selection"][0])
    paths["mp3"].unlink()
    monkeypatch.setattr(runner, "_now", lambda: created + dt.timedelta(days=2))
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    with pytest.raises(runner.AudioArchiveError, match="missing|file|set"):
        runner._purge_expired(args)
    assert paths["m4a"].exists()
    assert not (run_dir / "purge.json").exists()


def test_completed_purge_repairs_lagging_state_on_retry(monkeypatch, tmp_path):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(runner, "_now", lambda: created)
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )
    _execute(run_dir, manifest, FakeArchiveTransport())
    stale_state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(runner, "_now", lambda: created + dt.timedelta(days=2))
    args = SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    runner._purge_expired(args)
    runner._atomic_write_json(run_dir / "state.json", stale_state)

    repaired = runner._purge_expired(args)
    assert repaired["status"] == "purged"
    assert repaired["audio_files_present"] == 0
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "purged"
    assert state["audio_files_present"] == 0


def test_planned_purge_rejects_nonprefix_missing_file(monkeypatch, tmp_path):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(runner, "_now", lambda: created)
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        post_ids=(POST_A, POST_B),
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )
    _execute(run_dir, manifest, FakeArchiveTransport())
    records = runner._load_records(run_dir, manifest)
    plan = runner._purge_plan(manifest, records)
    runner._atomic_write_json(run_dir / "purge.json", plan)
    # Delete a later entry while the first entry remains: this cannot be the
    # prefix produced by the ordered purge loop.
    later = run_dir / "audio" / plan["entries"][1]["file"]
    later.unlink()
    first = run_dir / "audio" / plan["entries"][0]["file"]
    monkeypatch.setattr(runner, "_now", lambda: created + dt.timedelta(days=2))

    with pytest.raises(runner.AudioArchiveError, match="prefix_ordered"):
        runner._purge_expired(
            SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
        )
    assert first.exists()


def test_planned_purge_resumes_one_hash_valid_staged_file(monkeypatch, tmp_path):
    created = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(runner, "_now", lambda: created)
    output_root, run_dir, manifest = _run_fixture(
        tmp_path,
        storage_mode="public-research",
        created_at=_timestamp(created),
        retention_days=1,
    )
    _execute(run_dir, manifest, FakeArchiveTransport())
    records = runner._load_records(run_dir, manifest)
    plan = runner._purge_plan(manifest, records)
    runner._atomic_write_json(run_dir / "purge.json", plan)
    first = plan["entries"][0]
    source = run_dir / "audio" / first["file"]
    staged = run_dir / ".staging" / runner._purge_staging_name(first)
    source.replace(staged)
    monkeypatch.setattr(runner, "_now", lambda: created + dt.timedelta(days=2))

    result = runner._purge_expired(
        SimpleNamespace(output_root=str(output_root), run_id=RUN_ID)
    )
    assert result["status"] == "purged"
    assert list((run_dir / "audio").iterdir()) == []
    assert list((run_dir / ".staging").iterdir()) == []
