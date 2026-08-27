import json
import sqlite3

import pytest

from tiktok_scraper.social_music_state import (
    CheckpointConflict,
    ImmutableScopeConflict,
    InvalidStateInput,
    SocialMusicState,
    StateIntegrityError,
    canonical_sha256,
)


MANIFEST_HASH = "a" * 64


def create_run(
    state: SocialMusicState,
    run_id: str = "listen-youtube-1",
    *,
    platform: str = "youtube",
    requested_count: int = 2,
):
    return state.create_run(
        run_id,
        platform=platform,
        source_mode="topic",
        target="banking music",
        requested_count=requested_count,
        capability_manifest_hash=MANIFEST_HASH,
        adapter_version="youtube-data-v3@2026-06",
    )


def evidence(content_id: str, *, platform: str = "youtube"):
    return {
        "identity": {"platform": platform, "content_id": content_id},
        "caption": f"caption for {content_id}",
        "music_declaration": {
            "status": "not_provided",
            "reason": "official_api_has_no_structured_music_declaration",
        },
    }


def test_run_scope_is_hash_bound_positive_and_idempotent(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        created = create_run(state)
        assert created["workflow"] == "listen"
        assert created["status"] == "collecting"
        assert created["requested_count"] == 2
        assert created["evidence_ready"] == 0
        assert len(created["scope_hash"]) == 64

        repeated = create_run(state)
        assert repeated["scope_hash"] == created["scope_hash"]
        with pytest.raises(ImmutableScopeConflict):
            create_run(state, requested_count=3)
        with pytest.raises(InvalidStateInput):
            state.create_run(
                "bad-count",
                platform="youtube",
                source_mode="topic",
                target="topic",
                requested_count=0,
                capability_manifest_hash=MANIFEST_HASH,
                adapter_version="v1",
            )
        with pytest.raises(InvalidStateInput):
            state.create_run(
                "bad-source",
                platform="youtube",
                source_mode="feed",
                target="topic",
                requested_count=1,
                capability_manifest_hash=MANIFEST_HASH,
                adapter_version="v1",
            )


def test_url_scope_requires_exactly_one_requested_record(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        with pytest.raises(InvalidStateInput, match="requested_count=1"):
            state.create_run(
                "bad-url-count",
                platform="youtube",
                source_mode="url",
                target="https://www.youtube.com/watch?v=exact123",
                requested_count=2,
                capability_manifest_hash=MANIFEST_HASH,
                adapter_version="v1",
            )
        created = state.create_run(
            "exact-url",
            platform="youtube",
            source_mode="url",
            target="https://www.youtube.com/watch?v=exact123",
            requested_count=1,
            capability_manifest_hash=MANIFEST_HASH,
            adapter_version="v1",
        )
        assert created["requested_count"] == 1


def test_checkpoint_requires_matching_nested_cli_identity(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        create_run(state, requested_count=1)
        with pytest.raises(StateIntegrityError, match="identity is required"):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=1,
                content_id="vid-1",
                evidence={"platform": "youtube", "content_id": "vid-1"},
            )
        with pytest.raises(StateIntegrityError, match="platform binding"):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=1,
                content_id="vid-1",
                evidence=evidence("vid-1", platform="x"),
            )
        with pytest.raises(StateIntegrityError, match="content_id binding"):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=1,
                content_id="vid-1",
                evidence=evidence("other-id"),
            )
        inserted = state.checkpoint_record(
            "listen-youtube-1",
            ordinal=1,
            content_id="vid-1",
            evidence=evidence("vid-1"),
        )
        assert inserted["inserted"] is True


def test_checkpoint_is_exactly_once_and_hash_verified(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        create_run(state)
        packet = evidence("vid-1")
        expected_hash = canonical_sha256(packet)
        inserted = state.checkpoint_record(
            "listen-youtube-1",
            ordinal=1,
            content_id="vid-1",
            evidence=packet,
            evidence_hash=expected_hash,
        )
        assert inserted["inserted"] is True
        assert inserted["evidence_hash"] == expected_hash

        repeated = state.checkpoint(
            "listen-youtube-1",
            ordinal=1,
            content_id="vid-1",
            evidence=packet,
            evidence_hash=expected_hash,
        )
        assert repeated["inserted"] is False
        assert len(state.records("listen-youtube-1")) == 1

        with pytest.raises(CheckpointConflict):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=1,
                content_id="vid-2",
                evidence=evidence("vid-2"),
            )
        with pytest.raises(CheckpointConflict):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=2,
                content_id="vid-1",
                evidence=evidence("vid-1"),
            )
        with pytest.raises(StateIntegrityError, match="supplied evidence hash"):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=2,
                content_id="vid-2",
                evidence=evidence("vid-2"),
                evidence_hash="f" * 64,
            )


def test_workspace_global_known_content_is_platform_scoped(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        create_run(state, requested_count=1)
        state.checkpoint_record(
            "listen-youtube-1",
            ordinal=1,
            content_id="shared-id",
            evidence=evidence("shared-id"),
        )
        assert state.is_known_content("youtube", "shared-id") is True
        assert state.is_known_content("x", "shared-id") is False
        assert state.exclude_known_content(
            "youtube", ["new-1", "shared-id", "new-1", "new-2"]
        ) == ["new-1", "new-2"]

        create_run(
            state,
            "listen-youtube-2",
            platform="youtube",
            requested_count=1,
        )
        with pytest.raises(CheckpointConflict, match="already known"):
            state.checkpoint_record(
                "listen-youtube-2",
                ordinal=1,
                content_id="shared-id",
                evidence=evidence("shared-id"),
            )

        create_run(state, "listen-x-1", platform="x", requested_count=1)
        state.checkpoint_record(
            "listen-x-1",
            ordinal=1,
            content_id="shared-id",
            evidence=evidence("shared-id", platform="x"),
        )
        assert state.known_content_ids("x") == {"shared-id"}


def test_finalize_completes_only_at_exact_ready_cardinality(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        create_run(state)
        for ordinal, content_id in enumerate(("vid-1", "vid-2"), 1):
            state.append_checkpoint(
                "listen-youtube-1",
                ordinal=ordinal,
                content_id=content_id,
                evidence=evidence(content_id),
            )
        status = state.status("listen-youtube-1")
        assert status["checkpointed"] == status["evidence_ready"] == 2
        assert status["pending"] == 0

        final = state.finalize(
            "listen-youtube-1", reasons=["an earlier candidate was skipped"]
        )
        assert final["status"] == "collection_complete"
        assert final["status_summary"] == "collection_complete: 2/2"
        assert final["reasons"] == []
        assert state.finalize("listen-youtube-1") == final
        repeated = state.checkpoint_record(
            "listen-youtube-1",
            ordinal=1,
            content_id="vid-1",
            evidence=evidence("vid-1"),
        )
        assert repeated["inserted"] is False
        with pytest.raises(CheckpointConflict, match="terminal"):
            state.checkpoint_record(
                "listen-youtube-1",
                ordinal=3,
                content_id="vid-3",
                evidence=evidence("vid-3"),
            )


def test_finalize_incomplete_preserves_counts_and_reasons(tmp_path):
    with SocialMusicState(tmp_path / "state.sqlite") as state:
        create_run(state, requested_count=3)
        state.checkpoint_record(
            "listen-youtube-1",
            ordinal=1,
            content_id="vid-ready",
            evidence=evidence("vid-ready"),
        )
        state.checkpoint_record(
            "listen-youtube-1",
            ordinal=2,
            content_id="vid-partial",
            evidence=evidence("vid-partial"),
            evidence_ready=False,
            collection_error="captions_permission_required",
        )
        final = state.finalize(
            "listen-youtube-1",
            reasons=["source_exhausted", "quota_exhausted", "source_exhausted"],
        )
        assert final["status"] == "collection_incomplete"
        assert final["status_summary"] == "collection_incomplete: 1/3"
        assert final["checkpointed"] == 2
        assert final["evidence_ready"] == 1
        assert final["not_ready"] == 1
        assert final["pending"] == 2
        assert final["reasons"] == ["source_exhausted", "quota_exhausted"]
        with pytest.raises(ImmutableScopeConflict, match="reasons"):
            state.finalize("listen-youtube-1", reasons=["different_reason"])


def test_database_contains_collection_only_tables_and_immutable_rows(tmp_path):
    database = tmp_path / "state.sqlite"
    with SocialMusicState(database) as state:
        create_run(state, requested_count=1)
        state.checkpoint_record(
            "listen-youtube-1",
            ordinal=1,
            content_id="vid-1",
            evidence=evidence("vid-1"),
        )

    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {
            "social_music_runs",
            "social_music_records",
            "social_music_known_content",
        }
        assert not any(
            fragment in name
            for name in tables
            for fragment in ("analysis", "draft", "approval", "publication")
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE social_music_records SET evidence_json=?",
                (json.dumps({"tampered": True}),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM social_music_records")
    finally:
        connection.close()
