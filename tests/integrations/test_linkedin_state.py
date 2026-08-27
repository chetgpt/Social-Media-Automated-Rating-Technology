import datetime as dt
import sqlite3

import pytest

from tiktok_scraper.linkedin_state import (
    COMMENT_RETENTION_HOURS,
    ORGANIZATION_POST_RETENTION_DAYS,
    InvalidLinkedInStateInput,
    LinkedInCollectionState,
    LinkedInStateConflict,
    LinkedInStateIntegrityError,
    canonical_sha256,
    iso,
)


ORGANIZATION_URN = "urn:li:organization:1001"
OTHER_ORGANIZATION_URN = "urn:li:organization:1002"
POST_URN_1 = "urn:li:share:7001"
POST_URN_2 = "urn:li:ugcPost:7002"


def create_organization_run(
    state: LinkedInCollectionState,
    *,
    run_id: str = "linkedin-listen-1",
    requested_count: int = 1,
    comments_per_post: int = 2,
    workflow: str = "listen",
) -> dict:
    return state.create_run(
        run_id,
        project="linkedin-project",
        workflow=workflow,
        source_mode="organization",
        organization_urn=ORGANIZATION_URN,
        requested_count=requested_count,
        api_version="202608",
        comments_per_post=comments_per_post,
    )


def comment(
    post_urn: str,
    comment_id: int,
    *,
    message: str = "Useful update",
) -> dict:
    root_id = post_urn.rsplit(":", 1)[-1]
    return {
        "comment_urn": f"urn:li:comment:(urn:li:activity:{root_id},{comment_id})",
        "actor_urn": "urn:li:person:member-redacted",
        "message": message,
        "likes": 2,
        "created_at": "2026-08-20T09:00:00+00:00",
    }


def observation(
    post_urn: str,
    *,
    observed_at: dt.datetime | None = None,
    comments: list[dict] | None = None,
    canonical_url: str | None = None,
) -> dict:
    observed = observed_at or dt.datetime(2026, 8, 21, 8, 0, tzinfo=dt.timezone.utc)
    return {
        "post_urn": post_urn,
        "organization_urn": ORGANIZATION_URN,
        "canonical_url": canonical_url
        or f"https://www.linkedin.com/feed/update/{post_urn}",
        "published_at": "2026-08-20T08:30:00+00:00",
        "commentary": "A product update grounded in collected evidence.",
        "content": {
            "media_category": "ARTICLE",
            "title": "Launch notes",
        },
        "metrics": {
            "reactions": 12,
            "comments": len(comments or []),
            "reposts": 1,
        },
        "collection": {
            "authority": "linkedin_official_api",
            "api_version": "202608",
            "post_status": "available",
            "social_metadata_status": "available",
            "comments_status": "complete",
            "comments_limit": 2,
            "total_comments_retrieved": len(comments or []),
            "terminal": True,
        },
        "comments": list(comments or []),
        "observed_at": iso(observed),
    }


def test_create_run_is_hash_bound_idempotent_and_publication_disabled(tmp_path):
    database = tmp_path / "linkedin.sqlite"
    with LinkedInCollectionState(database) as state:
        created = create_organization_run(state, workflow="engage")
        repeated = create_organization_run(state, workflow="engage")

        assert repeated["scope_hash"] == created["scope_hash"]
        assert created["platform"] == "linkedin"
        assert created["workflow"] == "engage"
        assert created["status"] == "collecting"
        assert created["publication_enabled"] is False
        assert created["external_ai_enabled"] is False
        assert created["retention"] == {
            "comment_hours": COMMENT_RETENTION_HOURS,
            "organization_post_days": ORGANIZATION_POST_RETENTION_DAYS,
        }

        with pytest.raises(LinkedInStateConflict, match="immutable scope"):
            create_organization_run(
                state,
                requested_count=2,
                workflow="engage",
            )

        raw = state.get_run("linkedin-listen-1")
        scope = {
            "schema_version": raw["schema_version"],
            "run_id": raw["run_id"],
            "project": raw["project"],
            "platform": "linkedin",
            "workflow": raw["workflow"],
            "source_mode": raw["source_mode"],
            "organization_urn": raw["organization_urn"],
            "exact_post_urn": raw["exact_post_urn"],
            "requested_count": raw["requested_count"],
            "api_version": raw["api_version"],
            "comments_per_post": raw["comments_per_post"],
            "retention": {
                "comment_hours": COMMENT_RETENTION_HOURS,
                "organization_post_days": ORGANIZATION_POST_RETENTION_DAYS,
            },
            "publication_enabled": False,
            "external_ai_enabled": False,
        }
        assert raw["scope_hash"] == canonical_sha256(scope)


def test_exact_post_scope_and_inventory_are_immutable(tmp_path):
    with LinkedInCollectionState(tmp_path / "linkedin.sqlite") as state:
        with pytest.raises(InvalidLinkedInStateInput, match="requested_count=1"):
            state.create_run(
                "bad-direct",
                project="linkedin-project",
                workflow="listen",
                source_mode="post",
                organization_urn=ORGANIZATION_URN,
                exact_post_urn=POST_URN_1,
                requested_count=2,
                api_version="202608",
                comments_per_post=1,
            )

        state.create_run(
            "direct",
            project="linkedin-project",
            workflow="listen",
            source_mode="post",
            organization_urn=ORGANIZATION_URN,
            exact_post_urn=POST_URN_1,
            requested_count=1,
            api_version="202608",
            comments_per_post=1,
        )
        with pytest.raises(LinkedInStateConflict, match="exact-post"):
            state.freeze_inventory("direct", [POST_URN_2])

        first = state.freeze_inventory("direct", [POST_URN_1])
        repeated = state.freeze_inventory("direct", [POST_URN_1])
        assert repeated["inventory_hash"] == first["inventory_hash"]
        assert state.inventory("direct") == [POST_URN_1]

        with pytest.raises(LinkedInStateConflict, match="immutable"):
            state.freeze_inventory("direct", [POST_URN_1, POST_URN_2])


def test_exact_collection_known_post_fence_and_terminal_status(tmp_path):
    with LinkedInCollectionState(tmp_path / "linkedin.sqlite") as state:
        create_organization_run(state, requested_count=1)
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1])
        checkpoint = state.checkpoint_post(
            "linkedin-listen-1",
            ordinal=1,
            observation=observation(
                POST_URN_1,
                comments=[comment(POST_URN_1, 8001)],
            ),
        )
        assert checkpoint["evidence_ready"] == 1
        assert len(checkpoint["evidence_hash"]) == 64

        completed = state.finalize("linkedin-listen-1")
        assert completed["status"] == "collection_complete"
        assert completed["status_summary"] == "collection_complete: 1/1"
        assert completed["reasons"] == []
        assert state.known_post_urns(ORGANIZATION_URN) == {POST_URN_1}

        create_organization_run(state, run_id="second-run")
        state.freeze_inventory("second-run", [POST_URN_1])
        with pytest.raises(LinkedInStateConflict, match="already known"):
            state.checkpoint_post(
                "second-run",
                ordinal=1,
                observation=observation(POST_URN_1),
            )


def test_failure_cannot_downgrade_an_evidence_ready_checkpoint(tmp_path):
    with LinkedInCollectionState(tmp_path / "linkedin.sqlite") as state:
        create_organization_run(state, requested_count=2)
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1, POST_URN_2])
        state.checkpoint_post(
            "linkedin-listen-1",
            ordinal=1,
            observation=observation(POST_URN_1),
        )

        with pytest.raises(LinkedInStateConflict, match="evidence-ready"):
            state.record_failure(
                "linkedin-listen-1",
                ordinal=1,
                post_urn=POST_URN_1,
                error_code="late_transport_error",
            )

        row = state.post_rows("linkedin-listen-1")[0]
        assert row["evidence_ready"] == 1
        assert row["error_code"] == ""


def test_comment_scope_is_capped_and_hash_bound_to_ready_evidence(tmp_path):
    with LinkedInCollectionState(tmp_path / "linkedin.sqlite") as state:
        create_organization_run(
            state,
            requested_count=2,
            comments_per_post=1,
        )
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1, POST_URN_2])

        too_many = observation(
            POST_URN_1,
            comments=[
                comment(POST_URN_1, 8101),
                comment(POST_URN_1, 8102),
            ],
        )
        too_many["collection"]["comments_limit"] = 1
        with pytest.raises(InvalidLinkedInStateInput, match="comments_per_post"):
            state.checkpoint_post(
                "linkedin-listen-1",
                ordinal=1,
                observation=too_many,
            )
        assert state.post_rows("linkedin-listen-1") == []

        initial = observation(
            POST_URN_1,
            comments=[comment(POST_URN_1, 8101, message="Original")],
        )
        initial["collection"]["comments_limit"] = 1
        state.checkpoint_post(
            "linkedin-listen-1",
            ordinal=1,
            observation=initial,
        )
        replay = state.checkpoint_post(
            "linkedin-listen-1",
            ordinal=1,
            observation=initial,
        )
        assert replay["evidence_ready"] == 1

        changed_comments = observation(
            POST_URN_1,
            comments=[comment(POST_URN_1, 8101, message="Changed")],
        )
        changed_comments["collection"]["comments_limit"] = 1
        with pytest.raises(LinkedInStateConflict, match="immutable"):
            state.checkpoint_post(
                "linkedin-listen-1",
                ordinal=1,
                observation=changed_comments,
            )


def test_canonical_url_requires_the_exact_linkedin_hostname(tmp_path):
    with LinkedInCollectionState(tmp_path / "linkedin.sqlite") as state:
        create_organization_run(state)
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1])

        with pytest.raises(InvalidLinkedInStateInput, match="LinkedIn HTTPS URL"):
            state.checkpoint_post(
                "linkedin-listen-1",
                ordinal=1,
                observation=observation(
                    POST_URN_1,
                    canonical_url=(
                        "https://www.linkedin.com.evil.example/"
                        f"feed/update/{POST_URN_1}"
                    ),
                ),
            )


def test_status_rejects_frozen_inventory_hash_tampering(tmp_path):
    database = tmp_path / "linkedin.sqlite"
    with LinkedInCollectionState(database) as state:
        create_organization_run(state)
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1])

        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE linkedin_runs SET inventory_json=? WHERE run_id=?",
                ('["urn:li:share:9999"]', "linkedin-listen-1"),
            )

        with pytest.raises(LinkedInStateIntegrityError, match="inventory hash"):
            state.status("linkedin-listen-1")


def test_retention_purges_text_but_preserves_urn_tombstones(tmp_path):
    database = tmp_path / "linkedin.sqlite"
    observed = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    with LinkedInCollectionState(database) as state:
        create_organization_run(state)
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1])
        state.checkpoint_post(
            "linkedin-listen-1",
            ordinal=1,
            observation=observation(
                POST_URN_1,
                observed_at=observed,
                comments=[comment(POST_URN_1, 8201)],
            ),
        )
        assert state.finalize("linkedin-listen-1")["status"] == "collection_complete"

        comment_purge = state.purge_expired(
            now=observed + dt.timedelta(hours=COMMENT_RETENTION_HOURS + 1)
        )
        assert comment_purge == {"comments_purged": 1, "posts_purged": 0}

        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM linkedin_comments"
            ).fetchone()[0] == 0
            tombstone = connection.execute(
                "SELECT post_urn, comment_urn FROM linkedin_comment_tombstones"
            ).fetchone()
        assert tombstone == (
            POST_URN_1,
            "urn:li:comment:(urn:li:activity:7001,8201)",
        )

        post_purge = state.purge_expired(
            now=observed + dt.timedelta(days=ORGANIZATION_POST_RETENTION_DAYS + 1)
        )
        assert post_purge == {"comments_purged": 0, "posts_purged": 1}
        row = state.post_rows("linkedin-listen-1")[0]
        assert row["post_urn"] == POST_URN_1
        assert row["organization_urn"] == ORGANIZATION_URN
        assert row["canonical_url"] == ""
        assert row["published_at"] == ""
        assert row["commentary"] == ""
        assert row["content_json"] == "{}"
        assert row["metrics_json"] == "{}"
        assert row["collection_json"] == "{}"
        assert row["evidence_hash"] == ""
        assert row["evidence_ready"] == 0
        assert row["observed_at"] == ""
        assert row["expires_at"] == ""
        assert row["purged_at"]
        assert state.known_post_urns(ORGANIZATION_URN) == {POST_URN_1}

        status = state.status("linkedin-listen-1")
        assert status["status"] == "collection_complete"
        assert status["evidence_ready"] == 0
        assert status["purged_post_rows"] == 1

        for path in (database, database.with_name(database.name + "-wal")):
            if path.exists():
                retained_bytes = path.read_bytes()
                assert b"Useful update" not in retained_bytes
                assert b"A product update grounded" not in retained_bytes
                assert b"Launch notes" not in retained_bytes


def test_incomplete_run_records_deduplicated_terminal_reasons(tmp_path):
    with LinkedInCollectionState(tmp_path / "linkedin.sqlite") as state:
        create_organization_run(state)
        state.freeze_inventory("linkedin-listen-1", [POST_URN_1])
        state.record_failure(
            "linkedin-listen-1",
            ordinal=1,
            post_urn=POST_URN_1,
            error_code="api_access_denied",
        )
        result = state.finalize(
            "linkedin-listen-1",
            reasons=["api_access_denied", "api_access_denied", ""],
        )

        assert result["status"] == "collection_incomplete"
        assert result["status_summary"] == "collection_incomplete: 0/1"
        assert result["reasons"] == ["api_access_denied"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workflow", "audit", "workflow"),
        ("source_mode", "topic", "source_mode"),
        ("organization_urn", "urn:li:person:1001", "organization_urn"),
        ("api_version", "v202608", "api_version"),
        ("comments_per_post", 0, "comments_per_post"),
    ],
)
def test_create_run_rejects_unsupported_or_unbounded_scope(
    tmp_path,
    field,
    value,
    message,
):
    payload = {
        "project": "linkedin-project",
        "workflow": "listen",
        "source_mode": "organization",
        "organization_urn": ORGANIZATION_URN,
        "requested_count": 1,
        "api_version": "202608",
        "comments_per_post": 1,
    }
    payload[field] = value
    with LinkedInCollectionState(tmp_path / f"{field}.sqlite") as state:
        with pytest.raises(InvalidLinkedInStateInput, match=message):
            state.create_run("invalid", **payload)
