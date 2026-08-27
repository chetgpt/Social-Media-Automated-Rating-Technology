import asyncio
import csv
import json
import sqlite3

from campaign_spec import normalize_campaign_spec
from incremental_project import compile_outputs, ingest_videos, init_db
from tiktok_scraper.profile_contract import (
    extract_profile_candidate,
    normalize_profile_record,
)
from tiktok_scraper.profile_enrichment import (
    PublicProfileCollector,
    enqueue_profile_candidate,
    enrich_queued_profiles,
    profile_coverage,
    upsert_profile,
)


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_profile_contract_separates_declared_geo_from_audience_demographics():
    profile = normalize_profile_record(
        {
            "platform": "x",
            "user_id": "user-1",
            "username": "public_user",
            "display_name": "Public User",
            "bio": "Researcher, she/her, age 34",
            "location": "Jakarta, Indonesia",
            "follower_count": 0,
        },
        source="official_api",
        collection_method="x_api_v2_user_lookup",
        observed_at="2026-07-17T10:00:00+07:00",
    )

    assert profile["profile_key"] == "id:user-1"
    assert profile["declared_geography"]["raw"] == "Jakarta, Indonesia"
    assert profile["declared_geography"]["is_audience_geography"] is False
    assert profile["self_declared"]["pronouns"] == ["she/her"]
    assert profile["self_declared"]["age_statement"] == "age 34"
    assert profile["self_declared"]["gender_classification"] is None
    assert profile["audience_analytics"]["status"] == "first_party_required"
    assert profile["follower_count"] == 0


def test_profile_candidate_keeps_display_names_out_of_handle_fields():
    youtube = extract_profile_candidate(
        {
            "platform": "youtube",
            "creator": "Example Channel",
            "creator_id": "UC123",
        },
        platform="youtube",
        role="creator",
    )
    facebook = extract_profile_candidate(
        {
            "platform": "facebook",
            "creator": "Public Person",
            "creator_id": "10001",
        },
        platform="facebook",
        role="creator",
    )

    assert youtube["username"] == ""
    assert youtube["display_name"] == "Example Channel"
    assert youtube["profile_url"] == "https://www.youtube.com/channel/UC123/about"
    assert facebook["username"] == ""
    assert facebook["display_name"] == "Public Person"
    assert facebook["profile_url"] == "https://www.facebook.com/10001/about"


def test_ingestion_queues_profiles_and_compiler_joins_profile_references(tmp_path):
    conn = memory_db()
    ingest_videos(
        conn,
        project="profile_project",
        keyword="campaign",
        platform="x",
        run_id="run-1",
        scraped_at="2026-07-17T10:00:00+07:00",
        videos=[
            {
                "video_id": "post-1",
                "url": "https://x.com/creator/status/post-1",
                "caption": "Campaign",
                "username": "creator",
                "creator_id": "creator-1",
                "creator_profile_location": "Bandung",
                "x_api_author": {
                    "id": "creator-1",
                    "username": "creator",
                    "name": "Creator",
                    "description": "Based in Bandung",
                    "location": "Bandung",
                    "public_metrics": {"followers_count": 40},
                },
                "comments": [
                    {
                        "comment_id": "comment-1",
                        "author": "reader",
                        "author_id": "reader-1",
                        "text": "Hello",
                        "author_profile": {
                            "user_id": "reader-1",
                            "username": "reader",
                            "display_name": "Reader",
                            "location": {
                                "city": "Jakarta",
                                "country": "Indonesia",
                                "country_code": "ID",
                            },
                        },
                    }
                ],
            }
        ],
    )

    queue = conn.execute(
        """
        SELECT profile_key, source_roles_json
        FROM profile_enrichment_queue
        ORDER BY priority DESC
        """
    ).fetchall()
    assert [row["profile_key"] for row in queue] == ["id:creator-1", "id:reader-1"]
    assert json.loads(queue[0]["source_roles_json"]) == ["creator"]
    assert profile_coverage(conn, "profile_project")["totals"] == {
        "queued": 2,
        "profiles_available": 2,
        "profiles_with_declared_geography": 2,
        "profiles_with_profile_geography": 2,
        "profiles_with_self_declared_geography": 1,
        "profiles_with_platform_geography": 1,
        "profiles_with_country_code": 1,
        "profiles_with_multiple_geography_evidence": 0,
        "profile_geography_evidence_count": 2,
        "profiles_with_self_declared_demographic_text": 0,
        "profiles_with_pronouns": 0,
        "profiles_with_public_birthdate": 0,
        "profiles_with_public_gender_statement": 0,
    }

    paths = {"latest": tmp_path / "latest", "reports": tmp_path / "reports"}
    result = compile_outputs(conn, "profile_project", "campaign", paths)
    compiled = json.loads(
        (paths["latest"] / "combined_all_platforms_comments.json").read_text(
            encoding="utf-8"
        )
    )
    video = compiled["videos"][0]
    comment = video["comments"][0]
    assert video["creator_profile_reference"]["profile_key"] == "id:creator-1"
    assert video["creator_profile_reference"]["declared_geography"]["raw"] == "Bandung"
    assert comment["author_profile_reference"]["profile_key"] == "id:reader-1"
    assert comment["author_profile_reference"]["declared_geography"]["country_code"] == "ID"
    assert len(compiled["profiles"]) == 2
    assert result["profiles_available"] == 2

    with (paths["latest"] / "combined_all_platforms_comments_flat.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["creator_declared_location"] == "Bandung"
    assert row["author_country_code"] == "ID"


class FakeProfileCollector:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def collect(self, candidate):
        return normalize_profile_record(
            {
                "platform": candidate["platform"],
                "user_id": candidate["user_id"],
                "username": candidate["username"],
                "display_name": "Enriched User",
                "bio": "Based in Surabaya",
                "location": {
                    "city": "Surabaya",
                    "country": "Indonesia",
                    "country_code": "ID",
                },
                "follower_count": 120,
            },
            source="fake_api",
            collection_method="test_profile_api",
            observed_at="2026-07-17T12:00:00+07:00",
        )


def test_enrichment_runner_updates_cache_and_queue_status():
    conn = memory_db()
    enqueue_profile_candidate(
        conn,
        project="profile_project",
        platform="instagram",
        candidate={
            "platform": "instagram",
            "user_id": "ig-1",
            "username": "account",
        },
        source_role="creator",
        observed_at="2026-07-17T10:00:00+07:00",
        priority=100,
    )
    conn.commit()

    result = asyncio.run(
        enrich_queued_profiles(
            conn,
            "profile_project",
            limit=10,
            cache_days=7,
            collector=FakeProfileCollector(),
        )
    )

    assert result["enriched"] == 1
    queue = conn.execute(
        "SELECT status, attempt_count, last_error FROM profile_enrichment_queue"
    ).fetchone()
    assert dict(queue) == {
        "status": "complete",
        "attempt_count": 1,
        "last_error": "",
    }
    profile = json.loads(
        conn.execute("SELECT profile_json FROM user_profiles").fetchone()[0]
    )
    assert profile["declared_geography"]["city"] == "Surabaya"
    assert profile["audience_analytics"]["status"] == "first_party_required"


def test_profile_coverage_counts_country_name_without_raw_location():
    conn = memory_db()
    profile = normalize_profile_record(
        {
            "platform": "youtube",
            "user_id": "UC123",
            "display_name": "Channel",
            "country": "Indonesia",
            "geography_basis": "channel_country",
        },
        source="browser_session_api",
        collection_method="youtube_web_innertube_profile",
    )
    upsert_profile(
        conn,
        project="profile_project",
        profile=profile,
        mark_queue_complete=False,
    )
    conn.commit()

    totals = profile_coverage(conn, "profile_project")["totals"]
    assert totals["profiles_with_profile_geography"] == 1
    assert totals["profiles_with_platform_geography"] == 1
    assert totals["profiles_with_country_code"] == 0


def test_profile_upsert_removes_cached_facebook_neuter_gender_token():
    conn = memory_db()
    profile = normalize_profile_record(
        {
            "platform": "facebook",
            "user_id": "page-1",
            "display_name": "Page",
        },
        source="browser_session_api",
        collection_method="facebook_web_profile_graphql_or_structured_data",
    )
    profile["self_declared"]["gender_statement"] = "NEUTER"
    profile["availability"]["self_declared_demographic_text"] = "available"
    profile["field_provenance"]["self_declared_demographic_text"] = {
        "source": "legacy",
    }

    stored = upsert_profile(
        conn,
        project="profile_project",
        profile=profile,
        mark_queue_complete=False,
    )

    assert stored["self_declared"]["gender_statement"] == ""
    assert (
        stored["availability"]["self_declared_demographic_text"]
        == "not_observed"
    )
    assert "self_declared_demographic_text" not in stored["field_provenance"]


class RevealingIdCollector(FakeProfileCollector):
    async def collect(self, candidate):
        profile = await super().collect(candidate)
        profile["user_id"] = "revealed-id"
        profile["profile_key"] = "id:revealed-id"
        return profile


def test_enrichment_keeps_one_identity_when_api_reveals_a_user_id():
    conn = memory_db()
    enqueue_profile_candidate(
        conn,
        project="profile_project",
        platform="youtube",
        candidate={"platform": "youtube", "username": "channel_handle"},
        source_role="creator",
        observed_at="2026-07-17T10:00:00+07:00",
        priority=100,
    )
    conn.commit()

    asyncio.run(
        enrich_queued_profiles(
            conn,
            "profile_project",
            collector=RevealingIdCollector(),
        )
    )
    enqueue_profile_candidate(
        conn,
        project="profile_project",
        platform="youtube",
        candidate={
            "platform": "youtube",
            "user_id": "revealed-id",
            "username": "channel_handle",
        },
        source_role="comment_author",
        observed_at="2026-07-18T10:00:00+07:00",
        priority=20,
    )

    queue = conn.execute(
        "SELECT profile_key, user_id, status FROM profile_enrichment_queue"
    ).fetchall()
    profiles = conn.execute(
        "SELECT profile_key, user_id FROM user_profiles"
    ).fetchall()
    assert [tuple(row) for row in queue] == [
        ("username:channel_handle", "revealed-id", "complete")
    ]
    assert [tuple(row) for row in profiles] == [
        ("username:channel_handle", "revealed-id")
    ]


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "data": {
                "id": "x-1",
                "username": "account",
                "name": "Account",
                "description": "Public bio",
                "location": "Jakarta",
                "verified": True,
                "public_metrics": {
                    "followers_count": 50,
                    "following_count": 5,
                    "tweet_count": 20,
                },
            }
        }


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
        )
        return FakeResponse()


def test_x_official_profile_collector_requests_and_normalizes_public_fields(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token")
    session = FakeSession()
    collector = PublicProfileCollector(
        session=session,
        browser_fallback=False,
    )

    profile = asyncio.run(
        collector.collect(
            {
                "platform": "x",
                "profile_key": "username:account",
                "username": "account",
            }
        )
    )

    assert session.calls[0]["url"].endswith("/users/by/username/account")
    assert "location" in session.calls[0]["params"]["user.fields"]
    assert profile["profile_key"] == "id:x-1"
    assert profile["declared_geography"]["raw"] == "Jakarta"
    assert profile["follower_count"] == 50
    assert profile["collection_method"] == "x_api_v2_user_lookup"


def test_campaign_spec_normalizes_profile_enrichment_settings():
    spec = normalize_campaign_spec(
        {
            "schema_version": 1,
            "name": "Profile Campaign",
            "platforms": ["x"],
            "keywords": {"core": ["Campaign"]},
            "profiles": {
                "enabled": True,
                "limit_per_run": 250,
                "cache_days": 30,
                "browser_fallback": False,
            },
        }
    )

    assert spec["profiles"] == {
        "enabled": True,
        "limit_per_run": 250,
        "cache_days": 30,
        "browser_fallback": False,
    }
