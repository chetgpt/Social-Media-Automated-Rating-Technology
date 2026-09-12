from __future__ import annotations

import json
from pathlib import Path

import pytest

import social_music_audit as cli
from tiktok_scraper.social_music_contract import validate_capability_manifest
from tiktok_scraper.social_music_state import SocialMusicState


def _invoke(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    stream = captured.out if code == 0 else captured.err
    return code, json.loads(stream)


def _record(
    content_id: str,
    *,
    platform: str = "youtube",
    comment_status: str = "complete",
    collector_error: str = "",
) -> dict:
    record = {
        "platform": platform,
        "video_id": content_id,
        "channel_id": "channel-1",
        "username": "artist",
        "title": f"Title {content_id} https://unsafe.example/watch",
        "description": "A description with https://unsafe.example/media",
        "view_count": 100,
        "like_count": 12,
        "comment_count": 1,
        "comments": [
            {
                "id": f"comment-{content_id}",
                "author": "listener",
                "text": "nice song https://unsafe.example/avatar",
                "like_count": 2,
                "replies": [],
                "avatar_url": "https://unsafe.example/avatar.jpg",
            }
        ],
        "comments_exhausted": comment_status == "complete",
        "comment_limit_reached": comment_status == "truncated",
        "subtitle_status": "not_provided",
        "subtitle_source": "youtube_captions_api",
        "transcript_status": "not_provided",
        "transcript_source": "youtube_captions_api",
        "video_url": "https://unsafe.example/signed",
        "cookie": "do-not-store",
        "analysis": {"sentiment": "positive"},
    }
    if comment_status in {"partial", "unknown"}:
        record["comments_exhausted"] = False if comment_status == "partial" else None
        if comment_status == "unknown":
            record.pop("comments_exhausted")
            record.pop("comment_count")
    if comment_status == "error":
        record["comment_error"] = "comments denied"
    if collector_error:
        record["error"] = collector_error
    return record


def _create(
    capsys: pytest.CaptureFixture[str], database: Path, *, posts: int = 1, run_id: str = "run-1"
) -> dict:
    code, output = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "create",
            "--platform",
            "youtube",
            "--source-mode",
            "topic",
            "--target",
            "indie music",
            "--posts",
            str(posts),
            "--run-id",
            run_id,
        ],
    )
    assert code == 0
    return output


def test_capabilities_and_global_database_order(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    code, output = _invoke(
        capsys,
        ["--database", str(database), "capabilities", "--platform", "youtube"],
    )
    assert code == 0
    assert len(output["capabilities"]) == 1
    assert validate_capability_manifest(output["capabilities"][0])

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["capabilities", "--database", str(database)])


def test_create_is_immutable_and_status_is_collection_only(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "state.sqlite3"
    created = _create(capsys, database, posts=2)
    assert created["workflow"] == "listen"
    assert created["requested_count"] == 2
    assert created["status"] == "collecting"

    code, status = _invoke(
        capsys, ["--database", str(database), "status", "--run-id", "run-1"]
    )
    assert code == 0
    assert status["source_mode"] == "topic"
    assert "analysis" not in status
    assert "publication" not in status

    code, error = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "create",
            "--platform",
            "youtube",
            "--source-mode",
            "topic",
            "--target",
            "changed",
            "--posts",
            "2",
            "--run-id",
            "run-1",
        ],
    )
    assert code == 1
    assert error["status"] == "error"


@pytest.mark.parametrize("shape", ["list", "single", "videos"])
def test_ingest_payload_shapes_platform_filter_and_safe_allowlist(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, shape: str
) -> None:
    database = tmp_path / f"{shape}.sqlite3"
    _create(capsys, database, run_id=f"run-{shape}")
    good = _record("yt-1")
    wrong = _record("tt-1", platform="tiktok")
    if shape == "list":
        payload: object = [good, wrong]
    elif shape == "single":
        payload = good
    else:
        payload = {"platform": "youtube", "videos": [good, wrong]}
    source = tmp_path / f"{shape}.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    code, result = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "ingest",
            "--run-id",
            f"run-{shape}",
            "--file",
            str(source),
        ],
    )
    assert code == 0
    assert result["inserted"] == 1
    assert result["evidence_ready"] == 1
    assert result["platform_filtered"] == (0 if shape == "single" else 1)

    evidence = SocialMusicState(database).records(f"run-{shape}")[0]["evidence"]
    assert set(evidence) == {
        "schema_version",
        "identity",
        "source",
        "caption",
        "description",
        "public_metrics",
        "collector",
        "comments",
        "social_music",
    }
    serialized = json.dumps(evidence)
    assert "unsafe.example" not in serialized
    assert "cookie" not in serialized
    assert "video_url" not in serialized
    assert "analysis" not in serialized
    assert evidence["social_music"]["platform"] == "youtube"
    assert evidence["source"]["mode"] == "topic"
    assert evidence["source"]["provenance"]["source"] == "immutable_run_scope"
    assert evidence["public_metrics"]["availability"]["views"] == "available"
    assert evidence["comments"]["completion_status"] == "complete"
    assert evidence["comments"]["stored_count"] == 1


@pytest.mark.parametrize(
    ("comment_status", "collector_error", "expected_reason"),
    [
        ("truncated", "", "comments_truncated"),
        ("partial", "", "comments_partial"),
        ("unknown", "", "comments_unknown"),
        ("error", "", "collector_error"),
        ("complete", "collector failed", "collector_error"),
    ],
)
def test_ingest_checkpoints_nonterminal_comments_and_errors_as_not_ready(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    comment_status: str,
    collector_error: str,
    expected_reason: str,
) -> None:
    run_id = f"run-{comment_status}-{bool(collector_error)}"
    database = tmp_path / f"{run_id}.sqlite3"
    _create(capsys, database, run_id=run_id)
    source = tmp_path / f"{run_id}.json"
    source.write_text(
        json.dumps(_record("item-1", comment_status=comment_status, collector_error=collector_error)),
        encoding="utf-8",
    )
    code, result = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", run_id, "--file", str(source)],
    )
    assert code == 0
    assert result["not_ready"] == 1
    row = SocialMusicState(database).records(run_id)[0]
    assert row["evidence_ready"] is False
    assert expected_reason in row["collection_error"]


def test_unique_idempotent_ingest_finalize_and_atomic_export(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "state.sqlite3"
    _create(capsys, database, posts=2)
    source = tmp_path / "input.json"
    source.write_text(json.dumps([_record("a"), _record("a"), _record("b")]), encoding="utf-8")

    code, ingested = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "run-1", "--file", str(source)],
    )
    assert code == 0
    assert ingested["inserted"] == 2
    assert ingested["duplicate_input"] == 1

    retry_source = tmp_path / "retry.json"
    retry_source.write_text(json.dumps([_record("a"), _record("b")]), encoding="utf-8")
    code, retried = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "ingest",
            "--run-id",
            "run-1",
            "--file",
            str(retry_source),
        ],
    )
    assert code == 0
    assert retried["idempotent"] == 2

    export_path = tmp_path / "evidence.jsonl"
    code, error = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "export",
            "--run-id",
            "run-1",
            "--file",
            str(export_path),
        ],
    )
    assert code == 1
    assert "collection_complete" in error["error"]
    assert not export_path.exists()

    code, finalized = _invoke(
        capsys, ["--database", str(database), "finalize", "--run-id", "run-1"]
    )
    assert code == 0
    assert finalized["status"] == "collection_complete"
    assert finalized["evidence_ready"] == 2

    code, exported = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "export",
            "--run-id",
            "run-1",
            "--file",
            str(export_path),
        ],
    )
    assert code == 0
    assert exported["records"] == 2
    lines = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert [line["content_id"] for line in lines] == ["a", "b"]
    assert all(line["schema_version"] == cli.EXPORT_SCHEMA_VERSION for line in lines)
    assert all(line["evidence_hash"] == cli._hash_json(line["evidence"]) for line in lines)

    code, protected = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "export",
            "--run-id",
            "run-1",
            "--file",
            str(database),
        ],
    )
    assert code == 1
    assert "must not replace" in protected["error"]
    assert SocialMusicState(database).status("run-1")["status"] == "collection_complete"


def test_finalize_fails_closed_and_known_content_is_excluded(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "state.sqlite3"
    _create(capsys, database, posts=2, run_id="first")
    source = tmp_path / "one.json"
    source.write_text(json.dumps(_record("known")), encoding="utf-8")
    code, _ = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "first", "--file", str(source)],
    )
    assert code == 0
    code, incomplete = _invoke(
        capsys,
        [
            "--database",
            str(database),
            "finalize",
            "--run-id",
            "first",
            "--reason",
            "source_exhausted",
        ],
    )
    assert code == 0
    assert incomplete["status"] == "collection_incomplete"
    assert incomplete["status_summary"] == "collection_incomplete: 1/2"

    _create(capsys, database, run_id="second")
    code, result = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "second", "--file", str(source)],
    )
    assert code == 0
    assert result["known_content"] == 1
    assert result["inserted"] == 0


def test_creator_scope_normalizes_and_rejects_owner_substitution(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "creator.sqlite3"
    code, created = _invoke(
        capsys,
        [
            "--database", str(database), "create", "--platform", "youtube",
            "--source-mode", "creator", "--target", "https://www.youtube.com/@Artist/",
            "--posts", "1", "--run-id", "creator-run",
        ],
    )
    assert code == 0
    assert created["target"] == "@artist"

    substituted = _record("creator-video")
    substituted["username"] = "intruder"
    source = tmp_path / "substituted.json"
    source.write_text(json.dumps(substituted), encoding="utf-8")
    code, rejected = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "creator-run", "--file", str(source)],
    )
    assert code == 0
    assert rejected["inserted"] == 0
    assert rejected["invalid_records"] == 1
    assert rejected["reasons"] == ["source_identity_mismatch:creator-video"]

    exact = _record("creator-video")
    exact["video_url"] = "https://youtu.be/creator-video?feature=share"
    source.write_text(json.dumps(exact), encoding="utf-8")
    code, accepted = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "creator-run", "--file", str(source)],
    )
    assert code == 0
    assert accepted["inserted"] == accepted["evidence_ready"] == 1
    row = SocialMusicState(database).records("creator-run")[0]
    assert row["evidence"]["source"]["target_creator_handle"] == "@artist"
    assert row["evidence"]["source"]["canonical_url"] == (
        "https://www.youtube.com/watch?v=creator-video"
    )


def test_url_scope_requires_one_and_rejects_post_substitution(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "url.sqlite3"
    create_args = [
        "--database", str(database), "create", "--platform", "youtube",
        "--source-mode", "url", "--target", "https://youtu.be/exact123?si=transport",
    ]
    code, rejected_count = _invoke(capsys, create_args + ["--posts", "2", "--run-id", "bad-url"])
    assert code == 1
    assert "--posts 1" in rejected_count["error"]

    code, created = _invoke(capsys, create_args + ["--posts", "1", "--run-id", "url-run"])
    assert code == 0
    assert created["target"] == "https://www.youtube.com/watch?v=exact123"
    source = tmp_path / "url.json"
    substituted = _record("other123")
    substituted["video_url"] = "https://www.youtube.com/watch?v=other123"
    source.write_text(json.dumps(substituted), encoding="utf-8")
    code, rejected = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "url-run", "--file", str(source)],
    )
    assert code == 0
    assert rejected["inserted"] == 0
    assert rejected["reasons"] == ["source_identity_mismatch:other123"]

    exact = _record("exact123")
    exact["video_url"] = "https://www.youtube.com/shorts/exact123?feature=share"
    source.write_text(json.dumps(exact), encoding="utf-8")
    code, accepted = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "url-run", "--file", str(source)],
    )
    assert code == 0
    assert accepted["inserted"] == accepted["evidence_ready"] == 1
    evidence = SocialMusicState(database).records("url-run")[0]["evidence"]
    assert evidence["source"]["canonical_url"] == created["target"]
    assert evidence["source"]["target_content_id"] == "exact123"


def test_comment_envelope_declares_local_500_item_truncation_and_is_not_ready(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "comments.sqlite3"
    _create(capsys, database, run_id="comment-run")
    record = _record("many-comments")
    record["comment_count"] = cli.MAX_COMMENTS + 1
    record["comments"] = [
        {"id": f"c-{index}", "author": "listener", "text": f"comment {index}", "replies": []}
        for index in range(cli.MAX_COMMENTS + 1)
    ]
    source = tmp_path / "comments.json"
    source.write_text(json.dumps(record), encoding="utf-8")
    code, ingested = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "comment-run", "--file", str(source)],
    )
    assert code == 0
    assert ingested["not_ready"] == 1
    assert "comments_storage_truncated:many-comments" in ingested["reasons"]
    row = SocialMusicState(database).records("comment-run")[0]
    comments = row["evidence"]["comments"]
    assert comments["flat_collected"] == cli.MAX_COMMENTS + 1
    assert comments["stored_count"] == len(comments["items"]) == cli.MAX_COMMENTS
    assert comments["frontier"]["storage_truncated"] is True
    assert row["evidence_ready"] is False


def test_finalize_recalculates_readiness_from_persisted_envelope(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "readiness.sqlite3"
    _create(capsys, database, run_id="readiness-run")
    with SocialMusicState(database) as state:
        run = state.status("readiness-run")
        raw = _record("partial-proof", comment_status="partial")
        normalized = cli.finalize_content_record(
            raw,
            platform="youtube",
            source_context=cli._source_context(run),
            collection_context=cli._collection_context(run),
            observed_at=run["created_at"],
        )
        music = cli.social_music_evidence_from_record(
            normalized,
            platform="youtube",
            adapter_version=run["adapter_version"],
        )
        envelope = cli._safe_record_envelope(normalized, run, music)
        assert "comments_partial" in cli._readiness_reasons(envelope)
        state.checkpoint_record(
            "readiness-run",
            ordinal=1,
            content_id="partial-proof",
            evidence=envelope,
            evidence_ready=True,
            collection_error="",
        )
    code, rejected = _invoke(
        capsys,
        ["--database", str(database), "finalize", "--run-id", "readiness-run"],
    )
    assert code == 1
    assert "stored readiness flag mismatch" in rejected["error"]


def test_structured_text_and_malformed_or_unsafe_url_paths_fail_closed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "closed-inputs.sqlite3"
    _create(capsys, database, run_id="structured-run")
    structured = _record("structured")
    structured["title"] = {"unexpected": "mapping"}
    source = tmp_path / "structured.json"
    source.write_text(json.dumps(structured), encoding="utf-8")
    code, rejected = _invoke(
        capsys,
        ["--database", str(database), "ingest", "--run-id", "structured-run", "--file", str(source)],
    )
    assert code == 1
    assert "structured values" in rejected["error"]

    base = [
        "--database", str(database), "create", "--platform", "x",
        "--source-mode", "url", "--posts", "1",
    ]
    code, bad_port = _invoke(
        capsys,
        base + ["--target", "https://x.com:bad/artist/status/123", "--run-id", "bad-port"],
    )
    assert code == 1
    assert "invalid port" in bad_port["error"]
    code, bad_path = _invoke(
        capsys,
        base + ["--target", "https://x.com/bad%2Fname/status/123", "--run-id", "bad-path"],
    )
    assert code == 1
    assert "creator handle" in bad_path["error"]
    code, canonical = _invoke(
        capsys,
        base + ["--target", "https://twitter.com/Artist/status/123?tracking=yes", "--run-id", "x-url"],
    )
    assert code == 0
    assert canonical["target"] == "https://x.com/artist/status/123"
