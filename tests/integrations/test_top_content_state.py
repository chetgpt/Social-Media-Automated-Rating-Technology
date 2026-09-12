from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tiktok_scraper.top_content_state import (
    InvalidTopContentInput,
    TopContentLinkState,
    TopContentStateConflict,
    TopContentStateIntegrityError,
    canonical_sha256,
)


SOURCE_URL = (
    "https://ads.tiktok.com/creative/forpartners/creator/top-content?region=row"
)


def create_run(
    state: TopContentLinkState,
    *,
    run_id: str = "top-run",
    requested_count: int = 2,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "source_url": SOURCE_URL,
        "requested_count": requested_count,
        "scope": {"page": "top_content", "region": "row"},
        "filter_snapshot": {"industry": "all", "period": "7d"},
        "list_snapshot": {"observed_at": "2026-08-22T00:00:00+00:00"},
        "account_binding": {
            "browser_profile": "Profile 7",
            "account": "creator-marketplace-account",
        },
        "ranking_order": {"field": "page_rank", "direction": "ascending"},
    }
    values.update(overrides)
    return state.create_run(run_id, **values)  # type: ignore[arg-type]


def checkpoint(
    state: TopContentLinkState,
    ordinal: int,
    *,
    run_id: str = "top-run",
    ranking_position: int | None = None,
) -> dict[str, object]:
    video_id = str(7100000000000000000 + ordinal)
    creator = f"creator{ordinal}"
    return state.checkpoint_link(
        run_id,
        ordinal=ordinal,
        video_id=video_id,
        canonical_url=(
            f"https://www.tiktok.com/@{creator}/video/{video_id}?lang=en#ignored"
        ),
        creator=f"@{creator.upper()}",
        ranking_key=f"visible-rank-{ordinal}",
        ranking_position=ranking_position or ordinal,
    )


def test_create_run_freezes_normalized_scope_and_has_no_count_ceiling(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        result = create_run(
            state,
            requested_count=1_001,
            source_url=(
                "https://ads.tiktok.com/creative/forpartners/creator/"
                "top-content?REGION=ROW"
            ),
        )

        assert result["requested_count"] == 1_001
        assert result["remaining_count"] == 1_001
        assert result["status"] == "collecting"
        assert result["source_url"] == (
            "https://ads.tiktok.com/creative/forpartners/creator/"
            "top-content?region=row"
        )
        assert len(str(result["immutable_scope_hash"])) == 64


@pytest.mark.parametrize(
    "source_url",
    [
        SOURCE_URL + "#section",
        SOURCE_URL + "&tracking=1",
        SOURCE_URL + "&region=us",
        "https://user@ads.tiktok.com/creative/forpartners/creator/top-content",
        "https://ads.tiktok.com:444/creative/forpartners/creator/top-content",
    ],
)
def test_source_url_rejects_scope_expansion(tmp_path: Path, source_url: str):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        with pytest.raises(InvalidTopContentInput):
            create_run(state, source_url=source_url)


def test_source_url_defaults_an_omitted_region_to_row(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        result = create_run(
            state,
            source_url=(
                "https://ads.tiktok.com/creative/forpartners/creator/top-content"
            ),
        )
        assert result["source_url"] == SOURCE_URL


@pytest.mark.parametrize("requested_count", [0, -1, True, "not-a-count"])
def test_requested_count_must_be_positive(tmp_path: Path, requested_count: object):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        with pytest.raises(InvalidTopContentInput):
            create_run(state, requested_count=requested_count)  # type: ignore[arg-type]


def test_run_id_is_idempotent_only_for_the_same_immutable_scope(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        first = create_run(state)
        repeated = create_run(state)
        assert repeated["immutable_scope_hash"] == first["immutable_scope_hash"]

        with pytest.raises(TopContentStateConflict):
            create_run(state, filter_snapshot={"industry": "beauty"})


def test_new_only_scope_records_supplied_master_provenance_without_reading_it(
    tmp_path: Path,
):
    missing_master = tmp_path / "does-not-exist.sqlite3"
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        result = create_run(
            state,
            collection_policy="new_only_against_tiktok_master",
            master_database_identity="workspace-global-tiktok-master-v1",
            master_database_hash="a" * 64,
            master_database_path=str(missing_master),
            known_excluded_count=17,
            known_excluded_provenance={
                "candidate_ids_hash": "b" * 64,
                "checked_at": "2026-08-22T00:00:00+00:00",
            },
        )

        assert not missing_master.exists()
        assert result["collection_policy"] == "new_only_against_tiktok_master"
        assert result["known_excluded"]["count"] == 17  # type: ignore[index]
        assert result["master_database"]["sha256"] == "a" * 64  # type: ignore[index]


def test_all_links_cannot_claim_master_exclusions(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        with pytest.raises(InvalidTopContentInput):
            create_run(
                state,
                collection_policy="all_links",
                known_excluded_count=1,
            )


def test_checkpoint_is_canonical_idempotent_and_deduplicated(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        first = checkpoint(state, 1)
        assert first["canonical_url"] == (
            "https://www.tiktok.com/@creator1/video/7100000000000000001"
        )
        assert first["creator"] == "creator1"
        assert checkpoint(state, 1)["link_hash"] == first["link_hash"]
        assert state.status("top-run")["link_count"] == 1

        with pytest.raises(TopContentStateConflict):
            state.checkpoint_link(
                "top-run",
                ordinal=2,
                video_id="7100000000000000001",
                canonical_url=(
                    "https://www.tiktok.com/@creator1/video/7100000000000000001"
                ),
                creator="creator1",
                ranking_key="different",
                ranking_position=2,
            )


def test_checkpoint_rejects_url_identity_and_duplicate_rank_key_position(
    tmp_path: Path,
):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state, requested_count=3)
        checkpoint(state, 1, ranking_position=10)
        with pytest.raises(InvalidTopContentInput):
            state.checkpoint_link(
                "top-run",
                ordinal=2,
                video_id="7100000000000000002",
                canonical_url=(
                    "https://www.tiktok.com/@someone-else/video/7100000000000000002"
                ),
                creator="creator2",
                ranking_key="visible-rank-2",
                ranking_position=20,
            )
        with pytest.raises(TopContentStateConflict):
            state.checkpoint_link(
                "top-run",
                ordinal=2,
                video_id="7100000000000000002",
                canonical_url=(
                    "https://www.tiktok.com/@creator2/video/7100000000000000002"
                ),
                creator="creator2",
                ranking_key="visible-rank-1",
                ranking_position=10,
            )


def test_more_than_one_hundred_links_can_span_rankings_that_restart_at_one():
    with TopContentLinkState(":memory:") as state:
        create_run(state, requested_count=105)
        for ordinal in range(1, 106):
            ranking_key = "video_views" if ordinal <= 100 else "engagement"
            ranking_position = ordinal if ordinal <= 100 else ordinal - 100
            video_id = str(7100000000000000000 + ordinal)
            creator = f"creator{ordinal}"
            state.checkpoint_link(
                "top-run",
                ordinal=ordinal,
                video_id=video_id,
                canonical_url=(f"https://www.tiktok.com/@{creator}/video/{video_id}"),
                creator=creator,
                ranking_key=ranking_key,
                ranking_position=ranking_position,
            )

        final = state.finalize("top-run")
        assert final["status_summary"] == "links_complete: 105/105"
        assert state.links("top-run")[100]["ranking_position"] == 1


def test_staged_checkpoints_survive_connection_loss(tmp_path: Path):
    database = tmp_path / "links.sqlite3"
    state = TopContentLinkState(database)
    create_run(state)
    checkpoint(state, 1)
    state.close()

    with TopContentLinkState(database) as reopened:
        assert reopened.status("top-run")["status_summary"] == "collecting: 1/2"
        assert [row["video_id"] for row in reopened.links("top-run")] == [
            "7100000000000000001"
        ]


def test_finalize_requires_exact_count_and_exports_urls_only(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        checkpoint(state, 1)
        checkpoint(state, 2)
        final = state.finalize("top-run")

        assert final["status"] == "links_complete"
        assert final["status_summary"] == "links_complete: 2/2"
        exported = state.export_links("top-run")
        assert set(exported) == {"schema_version", "canonical_urls", "provenance"}
        assert exported["canonical_urls"] == [
            "https://www.tiktok.com/@creator1/video/7100000000000000001",
            "https://www.tiktok.com/@creator2/video/7100000000000000002",
        ]
        assert "creator" not in exported
        assert "caption" not in str(exported).casefold()
        with pytest.raises(TopContentStateConflict):
            checkpoint(state, 1)
        with pytest.raises(TopContentStateConflict):
            state.resume("top-run")


def test_incomplete_status_records_x_over_n_and_resume_keeps_links(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state, requested_count=3)
        checkpoint(state, 1)
        final = state.finalize("top-run", ["verified terminal frontier"])
        assert final["status"] == "links_incomplete"
        assert final["status_summary"] == "links_incomplete: 1/3"
        assert final["incomplete_reasons"] == ["verified terminal frontier"]
        with pytest.raises(TopContentStateConflict):
            state.checkpoint_link(
                "top-run",
                ordinal=2,
                video_id="7100000000000000002",
                canonical_url=(
                    "https://www.tiktok.com/@creator2/video/7100000000000000002"
                ),
                creator="creator2",
                ranking_key="visible-rank-2",
                ranking_position=2,
            )

        resumed = state.resume(
            "top-run",
            expected_scope_hash=str(final["immutable_scope_hash"]),
            requested_count=3,
            filter_snapshot={"industry": "all", "period": "7d"},
        )
        assert resumed["status"] == "collecting"
        assert resumed["link_count"] == 1
        assert resumed["incomplete_reasons"] == []
        checkpoint(state, 2)


def test_resume_rejects_scope_drift(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        state.finalize("top-run", ["browser unavailable"])
        with pytest.raises(TopContentStateConflict):
            state.resume("top-run", requested_count=50)
        with pytest.raises(TopContentStateConflict):
            state.resume("top-run", account_binding={"browser_profile": "Default"})


def test_recorded_discovery_failures_can_supply_incomplete_reasons(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        failure = state.record_discovery_failure(
            "top-run",
            "pagination stopped before a terminal frontier",
            stage="pagination",
            context={"page": 4},
        )
        assert failure["sequence"] == 1
        final = state.finalize("top-run")
        assert final["incomplete_reasons"] == [
            "pagination stopped before a terminal frontier"
        ]
        assert final["failure_count"] == 1


def test_incomplete_run_without_any_reason_cannot_finalize(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        with pytest.raises(InvalidTopContentInput):
            state.finalize("top-run")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "UPDATE top_content_link_runs SET list_snapshot_json='{}' "
            "WHERE run_id=?",
            ("top-run",),
        ),
        (
            "UPDATE top_content_links SET creator='tampered' "
            "WHERE run_id=? AND ordinal=1",
            ("top-run",),
        ),
        (
            "UPDATE top_content_link_runs SET status='links_complete' "
            "WHERE run_id=?",
            ("top-run",),
        ),
    ],
)
def test_status_fails_closed_on_stored_state_tampering(
    tmp_path: Path, sql: str, params: tuple[str, ...]
):
    database = tmp_path / "links.sqlite3"
    with TopContentLinkState(database) as state:
        create_run(state)
        checkpoint(state, 1)
    with sqlite3.connect(database) as connection:
        connection.execute(sql, params)
    with TopContentLinkState(database) as state:
        with pytest.raises(TopContentStateIntegrityError):
            state.status("top-run")


def test_listen_jobs_are_tracking_only_and_require_complete_links(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        with pytest.raises(TopContentStateConflict):
            state.initialize_listen_jobs("top-run", "project-a")

        checkpoint(state, 1)
        checkpoint(state, 2)
        state.finalize("top-run")
        settings = {
            "resolved_master_database": "comments_data/tiktok-master.sqlite3",
            "child_database": "comments_data/project-a.sqlite3",
            "max_comments": 100,
            "music_catalogs": ["musicbrainz"],
            "social_browser_state": "comments_data/social-browser.json",
            "startup_timeout": 60,
            "expected_account": "publisher_account",
        }
        jobs = state.initialize_listen_jobs("top-run", "project-a", settings)
        assert [job["status"] for job in jobs] == ["pending", "pending"]
        assert len(state.pending_listen_jobs("top-run")) == 2
        assert state.listen_jobs("top-run") == jobs
        status = state.status("top-run")
        assert status["listen_jobs"]["settings"] == settings  # type: ignore[index]
        assert (
            state.initialize_listen_jobs(
                "top-run", "project-a", dict(reversed(list(settings.items())))
            )
            == jobs
        )
        with pytest.raises(TopContentStateConflict):
            state.initialize_listen_jobs("top-run", "another-project", settings)
        with pytest.raises(TopContentStateConflict):
            state.initialize_listen_jobs(
                "top-run", "project-a", {**settings, "max_comments": 50}
            )


def test_listen_jobs_distinguish_new_evidence_from_known_skips(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state)
        checkpoint(state, 1)
        checkpoint(state, 2)
        state.finalize("top-run")
        state.initialize_listen_jobs("top-run", "project-a")

        skipped = state.update_listen_job(
            "top-run",
            "7100000000000000001",
            status="skipped_known",
            reason="known in TikTok master at preflight; no child launched",
        )
        assert skipped["child_run_id"] == ""
        state.update_listen_job(
            "top-run",
            ordinal=2,
            status="running",
            child_run_id="listen-child-2",
            child_database="comments_data/project-a.sqlite3",
        )
        completed = state.update_listen_job(
            "top-run",
            ordinal=2,
            status="complete",
            child_run_id="listen-child-2",
            child_database="comments_data/project-a.sqlite3",
        )
        assert completed["status"] == "complete"
        status = state.status("top-run")
        assert status["links_discovered"] == 2
        assert (
            status["listen_jobs"]["new_listen_evidence_complete"] == 1  # type: ignore[index]
        )
        assert status["listen_jobs"]["skipped_known"] == 1  # type: ignore[index]
        assert state.pending_listen_jobs("top-run") == []

        with pytest.raises(TopContentStateConflict):
            state.update_listen_job(
                "top-run",
                ordinal=1,
                status="running",
                child_run_id="late-child",
                child_database="late.sqlite3",
            )


def test_job_status_validation_requires_correct_child_identity(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state, requested_count=1)
        checkpoint(state, 1)
        state.finalize("top-run")
        state.initialize_listen_jobs("top-run", "project-a")

        with pytest.raises(InvalidTopContentInput):
            state.update_listen_job(
                "top-run",
                ordinal=1,
                status="complete",
                child_run_id="child-without-database",
            )
        with pytest.raises(InvalidTopContentInput):
            state.update_listen_job("top-run", ordinal=1, status="skipped_known")


def test_running_job_fences_database_before_child_run_id_is_known(tmp_path: Path):
    with TopContentLinkState(tmp_path / "links.sqlite3") as state:
        create_run(state, requested_count=1)
        checkpoint(state, 1)
        state.finalize("top-run")
        state.initialize_listen_jobs(
            "top-run",
            "project-a",
            {"child_database": "comments_data/project-a.sqlite3"},
        )

        dispatching = state.update_listen_job(
            "top-run",
            ordinal=1,
            status="running",
            child_database="comments_data/project-a.sqlite3",
        )
        assert dispatching["child_run_id"] == ""
        assert state.listen_jobs("top-run", ["running"]) == [dispatching]

        bound = state.update_listen_job(
            "top-run",
            ordinal=1,
            status="running",
            child_run_id="generated-listen-run",
            child_database="comments_data/project-a.sqlite3",
        )
        assert bound["child_run_id"] == "generated-listen-run"
        with pytest.raises(TopContentStateConflict):
            state.update_listen_job(
                "top-run",
                ordinal=1,
                status="running",
                child_run_id="different-run",
                child_database="comments_data/project-a.sqlite3",
            )


def test_listen_settings_tampering_fails_closed(tmp_path: Path):
    database = tmp_path / "links.sqlite3"
    with TopContentLinkState(database) as state:
        create_run(state, requested_count=1)
        checkpoint(state, 1)
        state.finalize("top-run")
        state.initialize_listen_jobs("top-run", "project-a", {"max_comments": 100})
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE top_content_link_runs SET listen_settings_json='{}'
            WHERE run_id='top-run'
            """
        )
    with TopContentLinkState(database) as state:
        with pytest.raises(TopContentStateIntegrityError):
            state.status("top-run")


def test_hash_helpers_are_deterministic():
    assert canonical_sha256({"b": 2, "a": [1]}) == canonical_sha256({"a": [1], "b": 2})
