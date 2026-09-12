import copy
import datetime as dt
import json
import sqlite3

import pytest

import linkedin_workflow
from tiktok_scraper.linkedin_api import (
    LinkedInRateLimitedError,
    LinkedInUnavailableError,
)
from tiktok_scraper.linkedin_state import LinkedInCollectionState


ORGANIZATION_URN = "urn:li:organization:24680"
POST_URN_1 = "urn:li:share:9101"
POST_URN_2 = "urn:li:ugcPost:9102"
POST_URN_3 = "urn:li:share:9103"


def _comment(post_number: int, comment_number: int = 1) -> dict:
    return {
        "comment_urn": (
            f"urn:li:comment:(urn:li:activity:{post_number},{comment_number})"
        ),
        "parent_comment_urn": "",
        "actor_urn": "urn:li:person:member-safe-id",
        "message": "A bounded member comment",
        "likes": 2,
        "created_at": "2026-08-21T07:00:00+00:00",
    }


def _observation(
    post_urn: str,
    *,
    comments: list[dict] | None = None,
    organization_urn: str = ORGANIZATION_URN,
) -> dict:
    comment_rows = list(comments or [])
    observed_at = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat()
    return {
        "post_urn": post_urn,
        "organization_urn": organization_urn,
        "canonical_url": f"https://www.linkedin.com/feed/update/{post_urn}/",
        "published_at": "2026-08-21T06:30:00+00:00",
        "commentary": f"Official Page update for {post_urn}",
        "content": {
            "type": "article",
            "title": "A safe normalized title",
            "asset_urns": [],
        },
        "metrics": {
            "comments": len(comment_rows),
            "top_level_comments": len(comment_rows),
            "reactions": 4,
            "reaction_counts": {"LIKE": 4},
        },
        "collection": {
            "authority": "linkedin_official_api",
            "api_version": "202608",
            "total_comments_retrieved": len(comment_rows),
        },
        "comments": comment_rows,
        "observed_at": observed_at,
    }


class FakeLinkedInClient:
    """Deterministic client double that never opens a network connection."""

    def __init__(self, inventory, responses=None):
        self.inventory = list(inventory)
        self.responses = dict(responses or {})
        self.list_calls = []
        self.collect_calls = []

    def list_author_post_urns(self, organization_urn):
        self.list_calls.append(organization_urn)
        return list(self.inventory)

    def collect_organization_post(
        self,
        post_urn,
        organization_urn,
        comments_limit=None,
    ):
        self.collect_calls.append(
            (post_urn, organization_urn, comments_limit)
        )
        response = self.responses.get(post_urn, _observation(post_urn))
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no fake response remains for {post_urn}")
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)


def _collect_args(
    database,
    *,
    workflow="listen",
    source="organization",
    posts=1,
    run_id="linkedin-run",
    post_urn=None,
    comments_per_post=5,
):
    args = [
        "--database",
        str(database),
        "collect",
        "--workflow",
        workflow,
        "--source",
        source,
        "--organization-urn",
        ORGANIZATION_URN,
        "--posts",
        str(posts),
        "--comments-per-post",
        str(comments_per_post),
        "--run-id",
        run_id,
    ]
    if post_urn is not None:
        args.extend(["--post-urn", post_urn])
    return args


def _output(capsys, *, error=False):
    captured = capsys.readouterr()
    raw = captured.err if error else captured.out
    assert raw.strip()
    return json.loads(raw)


@pytest.mark.parametrize("workflow", ["listen", "engage"])
def test_exact_organization_collection_for_listen_and_engage(
    tmp_path,
    monkeypatch,
    capsys,
    workflow,
):
    database = tmp_path / f"{workflow}.sqlite"
    fake = FakeLinkedInClient(
        [POST_URN_1, POST_URN_2, POST_URN_3],
        {
            POST_URN_1: _observation(
                POST_URN_1,
                comments=[_comment(9101)],
            ),
            POST_URN_2: _observation(POST_URN_2),
        },
    )
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: fake)

    result_code = linkedin_workflow.main(
        _collect_args(
            database,
            workflow=workflow,
            posts=2,
            run_id=f"{workflow}-exact",
            comments_per_post=7,
        )
    )
    result = _output(capsys)

    assert result_code == 0
    assert result["status"] == "collection_complete"
    assert result["status_summary"] == "collection_complete: 2/2"
    assert result["workflow"] == workflow
    assert result["inventory_count"] == 3
    assert result["evidence_ready"] == 2
    assert result["publication_enabled"] is False
    assert result["external_ai_enabled"] is False
    assert fake.list_calls == [ORGANIZATION_URN]
    assert fake.collect_calls == [
        (POST_URN_1, ORGANIZATION_URN, 7),
        (POST_URN_2, ORGANIZATION_URN, 7),
    ]

    with LinkedInCollectionState(database) as state:
        rows = state.post_rows(f"{workflow}-exact")
    assert [row["post_urn"] for row in rows] == [POST_URN_1, POST_URN_2]
    assert all(row["evidence_ready"] == 1 for row in rows)
    first_collection = json.loads(rows[0]["collection_json"])
    assert first_collection["terminal"] is True
    assert first_collection["comments_status"] == "complete"


def test_known_posts_are_excluded_before_freeze_and_replaced_to_exact_count(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "known-replacement.sqlite"
    first = FakeLinkedInClient([POST_URN_1])
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: first)
    assert linkedin_workflow.main(
        _collect_args(database, run_id="first", posts=1)
    ) == 0
    _output(capsys)

    replacement = FakeLinkedInClient([POST_URN_1, POST_URN_2, POST_URN_3])
    monkeypatch.setattr(
        linkedin_workflow,
        "_client",
        lambda *_args: replacement,
    )
    result_code = linkedin_workflow.main(
        _collect_args(database, run_id="second", posts=2)
    )
    result = _output(capsys)

    assert result_code == 0
    assert result["status_summary"] == "collection_complete: 2/2"
    assert result["inventory_count"] == 2
    assert replacement.collect_calls == [
        (POST_URN_2, ORGANIZATION_URN, 5),
        (POST_URN_3, ORGANIZATION_URN, 5),
    ]
    with LinkedInCollectionState(database) as state:
        assert state.inventory("second") == [POST_URN_2, POST_URN_3]
        assert state.known_post_urns(ORGANIZATION_URN) == {
            POST_URN_1,
            POST_URN_2,
            POST_URN_3,
        }


@pytest.mark.parametrize(
    "extra_args, expected_message",
    [
        (
            ["--source", "post", "--posts", "2", "--post-urn", POST_URN_1],
            "post source requires --post-urn and --posts 1",
        ),
        (
            ["--source", "post", "--posts", "1"],
            "post source requires --post-urn and --posts 1",
        ),
        (
            [
                "--source",
                "organization",
                "--posts",
                "1",
                "--post-urn",
                POST_URN_1,
            ],
            "--post-urn is valid only with --source post",
        ),
    ],
)
def test_exact_post_scope_rejects_ambiguous_or_non_unit_cardinality(
    tmp_path,
    monkeypatch,
    capsys,
    extra_args,
    expected_message,
):
    calls = []

    def forbidden_factory(*args):
        calls.append(args)
        raise AssertionError("invalid direct scope must fail before client creation")

    monkeypatch.setattr(linkedin_workflow, "_client", forbidden_factory)
    args = [
        "--database",
        str(tmp_path / "invalid-direct.sqlite"),
        "collect",
        "--workflow",
        "listen",
        "--organization-urn",
        ORGANIZATION_URN,
        "--run-id",
        "invalid-direct",
    ]
    args.extend(extra_args)

    assert linkedin_workflow.main(args) == 1
    result = _output(capsys, error=True)
    assert result["status"] == "error"
    assert expected_message in result["error"]
    assert calls == []


def test_exact_post_collects_only_bound_urn_without_inventory_discovery(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "exact-post.sqlite"
    fake = FakeLinkedInClient([POST_URN_2, POST_URN_3])
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: fake)

    result_code = linkedin_workflow.main(
        _collect_args(
            database,
            source="post",
            posts=1,
            post_urn=POST_URN_1,
            run_id="exact-post",
        )
    )
    result = _output(capsys)

    assert result_code == 0
    assert result["status_summary"] == "collection_complete: 1/1"
    assert result["exact_post_urn"] == POST_URN_1
    assert fake.list_calls == []
    assert fake.collect_calls == [(POST_URN_1, ORGANIZATION_URN, 5)]
    with LinkedInCollectionState(database) as state:
        assert state.inventory("exact-post") == [POST_URN_1]


def test_exhausted_inventory_is_incomplete_and_returns_exit_two(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "incomplete.sqlite"
    fake = FakeLinkedInClient([POST_URN_1])
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: fake)

    result_code = linkedin_workflow.main(
        _collect_args(database, posts=2, run_id="short-inventory")
    )
    result = _output(capsys)

    assert result_code == 2
    assert result["status"] == "collection_incomplete"
    assert result["status_summary"] == "collection_incomplete: 1/2"
    assert result["pending"] == 1
    assert "authorized_organization_inventory_exhausted" in result["reasons"]


@pytest.mark.parametrize(
    "first_error, initial_exit, initial_status",
    [
        (
            LinkedInUnavailableError("collect_organization_post"),
            2,
            "collection_incomplete",
        ),
        (
            LinkedInRateLimitedError(
                "collect_organization_post",
                retry_after_seconds=1,
            ),
            1,
            "collecting",
        ),
    ],
)
def test_resume_retries_failed_frozen_row_without_rediscovery(
    tmp_path,
    monkeypatch,
    capsys,
    first_error,
    initial_exit,
    initial_status,
):
    database = tmp_path / f"resume-{initial_status}.sqlite"
    failing = FakeLinkedInClient(
        [POST_URN_1],
        {POST_URN_1: first_error},
    )
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: failing)

    assert linkedin_workflow.main(
        _collect_args(database, run_id="resume-me")
    ) == initial_exit
    _output(capsys, error=initial_exit == 1)
    with LinkedInCollectionState(database) as state:
        assert state.status("resume-me")["status"] == initial_status
        failed_row = state.post_rows("resume-me")[0]
        assert failed_row["evidence_ready"] == 0
        assert failed_row["error_code"].startswith("linkedin_api_")

    resumed = FakeLinkedInClient(
        [POST_URN_2, POST_URN_3],
        {POST_URN_1: _observation(POST_URN_1)},
    )
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: resumed)
    result_code = linkedin_workflow.main(
        [
            "--database",
            str(database),
            "resume",
            "--run-id",
            "resume-me",
        ]
    )
    result = _output(capsys)

    assert result_code == 0
    assert result["status_summary"] == "collection_complete: 1/1"
    assert resumed.list_calls == []
    assert resumed.collect_calls == [(POST_URN_1, ORGANIZATION_URN, 5)]


def test_missing_access_token_returns_sanitized_error_before_state_or_network(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "missing-token.sqlite"
    monkeypatch.delenv(linkedin_workflow.ACCESS_TOKEN_ENV, raising=False)
    constructed = []

    def forbidden_constructor(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("client constructor must not run without a token")

    monkeypatch.setattr(
        linkedin_workflow,
        "LinkedInAPIClient",
        forbidden_constructor,
    )

    assert linkedin_workflow.main(
        _collect_args(database, run_id="missing-token")
    ) == 1
    result = _output(capsys, error=True)

    assert result["error"] == (
        "LINKEDIN_ACCESS_TOKEN is required for LinkedIn collection"
    )
    assert result["platform"] == "linkedin"
    assert result["schema_version"] == linkedin_workflow.CLI_SCHEMA_VERSION
    assert result["status"] == "error"
    assert result.get("run_id", "missing-token") == "missing-token"
    assert constructed == []
    assert not database.exists()


def test_status_and_purge_are_network_free(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "offline-commands.sqlite"
    fake = FakeLinkedInClient(
        [POST_URN_1],
        {POST_URN_1: _observation(POST_URN_1, comments=[_comment(9101)])},
    )
    monkeypatch.setattr(linkedin_workflow, "_client", lambda *_args: fake)
    assert linkedin_workflow.main(
        _collect_args(database, run_id="offline")
    ) == 0
    _output(capsys)

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("offline command attempted to create an API client")

    monkeypatch.setattr(linkedin_workflow, "_client", network_forbidden)
    monkeypatch.setattr(
        linkedin_workflow,
        "LinkedInAPIClient",
        network_forbidden,
    )
    monkeypatch.delenv(linkedin_workflow.ACCESS_TOKEN_ENV, raising=False)

    assert linkedin_workflow.main(
        ["--database", str(database), "status", "--run-id", "offline"]
    ) == 0
    status = _output(capsys)
    assert status["status"] == "collection_complete"
    assert status["publication_enabled"] is False

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE linkedin_comments SET expires_at='2000-01-01T00:00:00+00:00'"
        )
        connection.execute(
            "UPDATE linkedin_posts SET expires_at='2000-01-01T00:00:00+00:00'"
        )

    assert linkedin_workflow.main(
        ["--database", str(database), "purge-expired"]
    ) == 0
    purge = _output(capsys)
    assert purge["status"] == "purged"
    assert purge["comments_purged"] == 1
    assert purge["posts_purged"] == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM linkedin_comment_tombstones"
        ).fetchone()[0] == 1


def test_capabilities_exposes_closed_collection_only_boundary(
    monkeypatch,
    capsys,
):
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("capabilities attempted to create an API client")

    monkeypatch.setattr(linkedin_workflow, "_client", network_forbidden)
    monkeypatch.delenv(linkedin_workflow.ACCESS_TOKEN_ENV, raising=False)

    assert linkedin_workflow.main(["capabilities"]) == 0
    result = _output(capsys)

    assert result["platform"] == "linkedin"
    assert result["api_authority"] == "linkedin_official_api"
    assert result["workflows"] == {
        "engage": "collection_only",
        "listen": "collection_only",
    }
    assert set(result["sources"]) == {"organization", "post"}
    assert result["publication_enabled"] is False
    assert result["external_ai_enabled"] is False
    assert result["access_requirements"] == {
        "product": "linkedin_community_management",
        "oauth": "three_legged",
        "organization_post_read_scope": "r_organization_social",
        "organization_comment_read_scope": "r_organization_social_feed",
        "page_role_required": True,
    }
    assert {
        "topic_or_hashtag_discovery",
        "arbitrary_member_collection",
        "browser_scraping",
        "cross_platform_export",
        "external_ai_processing",
        "drafting",
        "approval",
        "publication",
    }.issubset(result["unsupported"])


def test_token_and_extra_raw_payload_never_enter_output_or_database(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "sanitized.sqlite"
    secret = "test" + "-oauth-secret-value-should-never-persist"
    normalized = _observation(POST_URN_1)
    normalized["raw_payload"] = {
        "Authorization": f"Bearer {secret}",
        "access_token": secret,
    }
    fake = FakeLinkedInClient([POST_URN_1], {POST_URN_1: normalized})
    constructor_calls = []

    def fake_constructor(access_token, *, api_version, timeout):
        constructor_calls.append((access_token, api_version, timeout))
        return fake

    monkeypatch.setenv(linkedin_workflow.ACCESS_TOKEN_ENV, secret)
    monkeypatch.setattr(
        linkedin_workflow,
        "LinkedInAPIClient",
        fake_constructor,
    )

    assert linkedin_workflow.main(
        _collect_args(database, run_id="sanitized")
    ) == 0
    captured = capsys.readouterr()

    assert constructor_calls == [(secret, "202608", 30.0)]
    assert secret not in captured.out
    assert secret not in captured.err
    database_bytes = database.read_bytes()
    assert secret.encode() not in database_bytes
    assert b"raw_payload" not in database_bytes
