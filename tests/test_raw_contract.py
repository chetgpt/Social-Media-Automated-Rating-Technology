import json

from campaign_spec import normalize_campaign_spec, expand_campaign_sources
from tiktok_scraper.raw_contract import (
    RAW_SCHEMA_VERSION,
    finalize_content_record,
    finalize_payload,
)
from tiktok_scraper.scrapers.facebook_scraper import FacebookScraper
from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper
from tiktok_scraper.scrapers.youtube_scraper import YouTubeScraper


def test_brief_contract_records_provenance_truncation_and_first_party_limits():
    record = {
        "platform": "instagram",
        "video_id": "ABC123",
        "url": "https://www.instagram.com/p/ABC123/",
        "caption": "Official #Campaign trailer with @talent",
        "content_creator": "publisher.id",
        "published_at": "2026-07-10T10:00:00+07:00",
        "like_count": 150,
        "reported_comment_count": 250,
        "discovery_method": "instagram_graphql_response",
        "metadata_method": "instagram_graphql_api",
        "comment_method": "instagram_direct_api",
        "comments_seen_in_response": 15,
        "comments": [
            {"comment_id": "c1", "text": "Excited!", "author": "one"},
            {"comment_id": "c2", "text": "Looks fun", "author": "two"},
            {"comment_id": "c3", "text": "Cannot wait", "author": "three"},
        ],
    }
    result = finalize_content_record(
        record,
        source_context={
            "source_key": "account:1",
            "kind": "account",
            "value": "publisher.id",
            "role": "publisher",
            "taxonomy": {"publisher_type": "entertainment"},
        },
        collection_context={
            "posts_per_source": 2,
            "discovery_candidates_per_source": 12,
            "comments_per_post": 3,
            "transport_mode": "api-first",
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert result["raw_schema_version"] == RAW_SCHEMA_VERSION
    assert result["source_provenance"]["source_role"] == "publisher"
    assert result["source_provenance"]["taxonomy"]["publisher_type"] == "entertainment"
    assert result["transport_provenance"]["api_data_used"] is True
    assert result["transport_provenance"]["dom_used"] is False
    assert result["collection_status"]["candidate_limit_per_source"] == 12
    assert result["collection_status"]["completion_status"] == "truncated"
    assert result["collection_status"]["comments_complete"] is False
    assert result["brief_evidence"]["text"]["hashtags"] == ["Campaign"]
    assert result["brief_evidence"]["text"]["mentions"] == ["talent"]
    assert result["analysis_readiness"]["sentiment"] == "ready_for_classification"
    assert result["analysis_readiness"]["monthly_reporting"] == "ready"
    assert result["field_availability"]["geography"] == "first_party_required"
    assert result["comments"][0]["collection_method"] == "instagram_direct_api"


def test_brief_contract_distinguishes_unknown_date_and_comment_completeness():
    result = finalize_content_record(
        {
            "platform": "facebook",
            "video_id": "42",
            "url": "https://www.facebook.com/watch/?v=42",
            "caption": "Campaign post",
            "content_creator": "Page",
            "comment_method": "facebook_browser_graphql",
            "comments": [],
        },
        collection_context={"transport_mode": "api-first"},
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert "unknown_publication_time" in result["quality_flags"]
    assert "comment_completeness_unknown" in result["quality_flags"]
    assert result["analysis_readiness"]["monthly_reporting"] == "observation_time_only"
    assert result["field_availability"]["reach"] == "first_party_required"


def test_payload_has_run_level_quality_summary():
    payload = finalize_payload(
        {
            "videos": [
                {
                    "video_id": "one",
                    "caption": "Text",
                    "content_creator": "creator",
                    "published_at": "2026-07-01",
                    "discovery_method": "youtube_innertube_api",
                    "comment_method": "youtube_internal_api",
                    "comments_exhausted": True,
                    "comments": [],
                },
                {
                    "video_id": "two",
                    "caption": "Other",
                    "content_creator": "creator",
                    "discovery_method": "browser_dom",
                    "comment_method": "instagram_direct_api",
                    "comments": [],
                },
            ]
        },
        platform="youtube",
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert payload["quality_summary"]["records"] == 2
    assert payload["quality_summary"]["records_using_api_data"] == 2
    assert payload["quality_summary"]["records_using_dom"] == 1
    assert payload["quality_summary"]["records_with_unknown_publication_time"] == 1


def test_contract_preserves_nested_reply_counts_and_existing_evidence():
    first = finalize_content_record(
        {
            "platform": "tiktok",
            "video_id": "123",
            "caption": "Campaign post",
            "content_creator": "publisher.id",
            "discovery_method": "browser_dom",
            "comment_method": "tiktok_comment_api",
            "comments": [
                {
                    "id": "root",
                    "text": "Top level",
                    "create_time": 1704067200,
                    "user": {"unique_id": "root_user", "nickname": "Root User"},
                    "replies": [
                        {
                            "id": "reply-1",
                            "text": "Reply one",
                            "create_time": 1704067260,
                            "user": {"unique_id": "reply_user", "nickname": "Reply User"},
                        },
                        {"id": "reply-2", "text": "Reply two"},
                    ],
                }
            ],
        },
        source_context={
            "source_key": "query:123",
            "kind": "query",
            "value": "Campaign",
            "role": "explicit",
        },
        collection_context={
            "comments_per_post": 2,
            "transport_mode": "api-first",
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert first["collection_status"]["top_level_comments_collected"] == 1
    assert first["collection_status"]["flat_comments_collected"] == 3
    assert first["collection_status"]["comment_limit_reached"] is False
    assert first["collection_status"]["completion_status"] == "unknown"
    assert first["transport_provenance"]["fallback_used"] is True
    root = first["comments"][0]
    reply = root["replies"][0]
    assert root["author"] == "root_user"
    assert root["comment_created_at"] == "2024-01-01T00:00:00+00:00"
    assert reply["parent_comment_id"] == "root"
    assert reply["is_reply"] is True
    assert reply["collection_method"] == "tiktok_comment_api"
    assert reply["comment_evidence"]["identity"]["content_id"] == "123"
    assert reply["comment_evidence"]["author"]["display_name"] == "Reply User"

    second = finalize_content_record(
        {**first, "reported_comment_count": 0},
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert second["source_provenance"]["source_key"] == "query:123"
    assert second["source_provenance"]["source_value"] == "Campaign"
    assert second["collection_status"]["comment_limit_per_post"] == 2
    assert second["collection_status"]["reported_comment_count"] is None
    assert second["collection_status"]["completion_status"] == "unknown"


def test_campaign_spec_preserves_transport_reporting_and_account_taxonomy():
    spec = normalize_campaign_spec({
        "schema_version": 1,
        "name": "Reusable Campaign",
        "keywords": {"core": ["Campaign"]},
        "platforms": ["instagram"],
        "accounts": {
            "instagram": [{
                "handle": "publisher.id",
                "role": "publisher",
                "taxonomy": {"publisher_type": "entertainment"},
            }],
        },
        "collection": {
            "transport_mode": "api-only",
            "discovery_candidates_per_source": 25,
        },
        "analysis": {"themes": [{"name": "activation", "terms": ["wrapper"]}]},
        "reporting": {"cadence": ["monthly"]},
        "storage": {
            "provider": "google-drive",
            "root_folder_name": "Client Projects",
            "archive_raw_runs": True,
            "raw_run_archive_timing": "final",
            "delete_local_after_upload": True,
            "chunk_size_mb": 32,
        },
    })
    sources = expand_campaign_sources(spec)
    account = next(source for source in sources if source["kind"] == "account")

    assert spec["collection"]["transport_mode"] == "api-only"
    assert spec["collection"]["discovery_candidates_per_source"] == 25
    assert spec["analysis"]["themes"][0]["name"] == "activation"
    assert spec["reporting"]["cadence"] == ["monthly"]
    assert spec["storage"]["provider"] == "google-drive"
    assert spec["storage"]["root_folder_name"] == "Client Projects"
    assert spec["storage"]["raw_run_archive_timing"] == "final"
    assert spec["storage"]["delete_local_after_upload"] is True
    assert spec["storage"]["chunk_size_mb"] == 32
    assert account["taxonomy"]["publisher_type"] == "entertainment"


def test_facebook_graphql_post_collector_uses_only_content_urls():
    scraper = FacebookScraper()
    posts = {}
    payload = {
        "data": {
            "story": {
                "__typename": "Story",
                "id": "story-node",
                "url": "https://www.facebook.com/watch/?v=123456789",
                "message": {"text": "Campaign trailer"},
                "actors": [{"id": "page-1", "name": "Publisher Page"}],
                "creation_time": 1783800000,
            },
            "navigation": {"url": "https://www.facebook.com/search/top/?q=campaign"},
        }
    }

    scraper._collect_posts_from_json(payload, posts)

    assert list(posts) == ["123456789"]
    assert posts["123456789"]["discovery_method"] == "facebook_graphql_response"
    assert posts["123456789"]["title"] == "Campaign trailer"


def test_contract_normalizes_relative_comment_time_and_marks_evidenced_partial_collection():
    result = finalize_content_record(
        {
            "platform": "instagram",
            "video_id": "post-one",
            "url": "https://www.instagram.com/reel/post-one/",
            "reported_comment_count": 5,
            "comments_exhausted": False,
            "comment_method": "instagram_direct_api",
            "comments": [
                {"comment_id": "one", "text": "First", "time": "3 hari yang lalu"},
                {"comment_id": "two", "text": "Second", "time": "1 day ago"},
            ],
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert result["collection_status"]["completion_status"] == "partial"
    assert result["collection_status"]["comments_complete"] is False
    assert result["comments"][0]["comment_created_at"] == "2026-07-09T12:00:00+07:00"
    assert result["comments"][1]["comment_created_at"] == "2026-07-11T12:00:00+07:00"


def test_contract_distinguishes_observed_zero_from_unavailable_metric():
    observed_zero = finalize_content_record(
        {
            "platform": "tiktok",
            "video_id": "zero",
            "view_count": 0,
            "metric_availability": {"views": "available"},
            "comments_exhausted": True,
            "comments": [],
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )
    unavailable = finalize_content_record(
        {
            "platform": "tiktok",
            "video_id": "missing",
            "view_count": 0,
            "comments_exhausted": True,
            "comments": [],
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert observed_zero["brief_evidence"]["engagement_snapshot"]["views"] == 0
    assert observed_zero["metric_availability"]["views"] == "available"
    assert unavailable["brief_evidence"]["engagement_snapshot"]["views"] is None
    assert unavailable["metric_availability"]["views"] == "missing_from_public_response"


def test_contract_counts_direct_post_replies_as_top_level_comments():
    result = finalize_content_record(
        {
            "platform": "x",
            "video_id": "root-post",
            "comments_exhausted": True,
            "comments": [
                {
                    "comment_id": "direct-reply",
                    "parent_comment_id": "root-post",
                    "is_reply": True,
                    "text": "Direct reply",
                },
                {
                    "comment_id": "nested-reply",
                    "parent_comment_id": "direct-reply",
                    "is_reply": True,
                    "text": "Nested reply",
                },
            ],
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )

    assert result["collection_status"]["top_level_comments_collected"] == 1
    assert result["collection_status"]["flat_comments_collected"] == 2

    youtube = finalize_content_record(
        {
            "platform": "youtube",
            "video_id": "video-root",
            "comment_method": "youtube_internal_api",
            "comments": [
                {"comment_id": "root", "text": "Root"},
                {"comment_id": "reply-without-parent", "is_reply": True, "text": "Reply"},
            ],
        },
        observed_at="2026-07-12T12:00:00+07:00",
    )
    assert youtube["collection_status"]["top_level_comments_collected"] == 1
    assert youtube["collection_status"]["completion_status"] == "complete"
    assert (
        youtube["collection_status"]["comments_exhaustion_evidence"]
        == "legacy_generator_completed_without_error_or_limit"
    )


def test_youtube_next_response_extracts_likes_and_subscribers():
    scraper = YouTubeScraper()
    payload = {
        "segmentedLikeDislikeButtonViewModel": {
            "likeButtonViewModel": {
                "title": "1.2K",
                "accessibilityText": "1.2K likes",
            }
        },
        "videoOwnerRenderer": {
            "subscriberCountText": {"simpleText": "34.5K subscribers"}
        },
    }

    metrics = scraper._extract_next_engagement(payload)

    assert metrics["like_count"] == 1200
    assert metrics["follower_count"] == 34500


def test_youtube_like_parser_ignores_api_versions_and_toggled_count():
    scraper = YouTubeScraper()
    payload = {
        "segmentedLikeDislikeButtonViewModel": {
            "likeButtonViewModel": {
                "likeButtonViewModel": {
                    "toggleButtonViewModel": {
                        "toggleButtonViewModel": {
                            "defaultButtonViewModel": {
                                "buttonViewModel": {
                                    "title": "155",
                                    "accessibilityText": "suka video ini bersama 155 orang lainnya",
                                    "onTap": {"apiUrl": "/youtubei/v1/like/like"},
                                }
                            },
                            "toggledButtonViewModel": {
                                "buttonViewModel": {"title": "156"}
                            },
                        }
                    }
                }
            }
        }
    }

    metrics = scraper._extract_next_engagement(payload)

    assert metrics["like_count"] == 155


def test_instagram_media_metrics_preserve_real_zero_and_applicability():
    scraper = InstagramScraper()
    metrics = scraper._media_metrics(
        {
            "media_type": 2,
            "play_count": 0,
            "like_count": 0,
            "user": {"follower_count": 0},
        }
    )
    photo = scraper._media_metrics({"media_type": 1, "like_count": 4, "user": {}})

    assert metrics["view_count"] == 0
    assert metrics["metric_availability"]["views"] == "available"
    assert metrics["metric_availability"]["likes"] == "available"
    assert metrics["metric_availability"]["followers"] == "available"
    assert photo["metric_availability"]["views"] == "not_applicable"


def test_facebook_group_photo_uses_post_identity_and_richer_metadata():
    scraper = FacebookScraper()
    posts = {}
    scraper._collect_posts_from_json(
        {
            "url": "https://www.facebook.com/photo.php?fbid=111&set=gm.999",
            "message": "Campaign update",
            "creation_time": 1710000000,
            "feedback": {
                "reaction_count": {"count": 12},
                "total_comment_count": 7,
                "share_count": {"count": 3},
            },
        },
        posts,
    )

    assert list(posts) == ["999"]
    assert posts["999"]["title"] == "Campaign update"
    assert posts["999"]["published_at"] == "2024-03-09T16:00:00+00:00"
    assert posts["999"]["like_count"] == 12
    assert posts["999"]["reported_comment_count"] == 7
    assert posts["999"]["share_count"] == 3
