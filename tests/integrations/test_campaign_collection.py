import asyncio
import datetime as dt
import json
import sqlite3
import sys

import pytest

from campaign_spec import CampaignSpecError, expand_campaign_sources, normalize_campaign_spec
from incremental_project import (
    collect_run_candidates,
    build_metric_coverage_report,
    compile_outputs,
    compile_coverage_report,
    export_direct_refresh_candidates,
    ingest_videos,
    init_db,
    main as incremental_main,
    max_x_post_id,
    record_discovery_run,
    register_source_plan,
    update_platform_state,
    write_campaign_state,
)
from run_scraper import load_candidate_file
from tiktok_scraper.api_integration import TikTokAPIIntegration, TikTokSearchSessionError
from tiktok_scraper.date_filter import filter_candidates_by_date, normalize_windows, parse_datetime
from tiktok_scraper.known_content import candidate_content_keys, filter_new_candidates
from tiktok_scraper.relevance import filter_candidates, load_topic_profile, score_candidate
from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper
from tiktok_scraper.scrapers.youtube_scraper import YouTubeScraper


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def campaign_payload():
    return {
        "schema_version": 1,
        "name": "Reusable Campaign",
        "project": "reusable_campaign",
        "platforms": ["youtube", "tiktok", "instagram", "facebook"],
        "keywords": {
            "core": ["Exact Campaign"],
            "hashtags": ["ExactCampaign"],
            "entities": ["Lead Actor"],
            "optional": ["trailer"],
            "campaign": ["Activation Phrase"],
            "exclusions": [],
        },
        "queries": {"all": ["official Exact Campaign"]},
        "accounts": {
            "youtube": [{"handle": "official.channel"}],
            "tiktok": [{"handle": "official.account"}],
            "instagram": [{"handle": "official.account"}],
            "facebook": [{"handle": "official.page"}],
        },
        "date_windows": [{
            "name": "campaign",
            "start": "2026-07-01T00:00:00+07:00",
            "end": "2026-07-31T23:59:59+07:00",
        }],
    }


def test_campaign_sources_are_deterministic_anchored_and_account_aware():
    spec = normalize_campaign_spec(campaign_payload())
    first = expand_campaign_sources(spec)
    second = expand_campaign_sources(spec)

    assert first == second
    assert any(source["value"] == "Lead Actor Exact Campaign" for source in first)
    assert any(source["value"] == "trailer Exact Campaign" for source in first)
    account_urls = {
        source["platform"]: source["value"]
        for source in first
        if source["kind"] == "account"
    }
    assert account_urls == {
        "youtube": "https://www.youtube.com/@official.channel",
        "tiktok": "https://www.tiktok.com/@official.account",
        "instagram": "https://www.instagram.com/official.account/",
        "facebook": "https://www.facebook.com/official.page/",
    }
    assert all(source["target_mode"] == "url" for source in first if source["kind"] == "account")


def test_campaign_supports_x_and_twitter_aliases():
    payload = campaign_payload()
    payload["platforms"].extend(["twitter", "x"])
    payload["queries"]["twitter"] = ["campaign lang:id"]
    payload["accounts"]["twitter"] = [{"handle": "official_x", "role": "owned"}]

    spec = normalize_campaign_spec(payload)
    sources = expand_campaign_sources(spec, selected_platforms=["x"])

    assert spec["platforms"].count("x") == 1
    assert "campaign lang:id" in spec["queries"]["x"]
    assert any(source["value"] == "https://x.com/official_x" for source in sources)


def test_campaign_spec_rejects_inverted_relevance_thresholds():
    payload = campaign_payload()
    payload["relevance"] = {"accept_threshold": 30, "review_threshold": 60}
    with pytest.raises(CampaignSpecError, match="cannot exceed"):
        normalize_campaign_spec(payload)


def test_campaign_profile_uses_source_query_and_trusted_account_as_review_evidence(tmp_path):
    spec = normalize_campaign_spec(campaign_payload())
    state = tmp_path / "state"
    state.mkdir()
    _, profile_path = write_campaign_state(spec, {"state": state})
    profile = load_topic_profile(str(profile_path))

    anchored_entity = score_candidate({
        "title": "Lead Actor interview",
        "username": "publisher",
        "matched_keywords": ["Lead Actor Exact Campaign"],
    }, profile, platform="youtube")
    trusted_teaser = score_candidate({
        "title": "Coming soon",
        "username": "official.account",
    }, profile, platform="instagram")

    assert anchored_entity["decision"] == "review"
    assert "Exact Campaign" in anchored_entity["matched"]["query"]
    assert trusted_teaser["decision"] == "review"
    assert trusted_teaser["matched"]["trusted_source"] == ["official.account"]
    assert trusted_teaser["needs_metadata_hydration"] is True


def test_campaign_profile_requires_content_anchor_before_comment_scrape(tmp_path):
    payload = campaign_payload()
    payload["keywords"]["core"].append("ECP")
    payload["relevance"] = {
        "mode": "filter",
        "review_action": "skip",
        "accept_threshold": 60,
        "review_threshold": 30,
        "require_anchor": True,
        "anchor_terms": ["Exact Campaign"],
        "ambiguous_terms": ["ECP"],
        "context_terms": ["launch", "trailer"],
    }
    spec = normalize_campaign_spec(payload)
    state = tmp_path / "state"
    state.mkdir()
    _, profile_path = write_campaign_state(spec, {"state": state})
    profile = load_topic_profile(str(profile_path))

    candidates = [
        {
            "video_id": "anchored",
            "title": "Exact Campaign official trailer",
            "username": "publisher",
            "matched_keywords": ["Exact Campaign"],
        },
        {
            "video_id": "acronym-collision",
            "title": "Electric coupe ECP custom build",
            "username": "cars",
            "matched_keywords": ["ECP"],
        },
        {
            "video_id": "activation-only",
            "title": "Activation Phrase tutorial",
            "username": "tutorials",
            "matched_keywords": ["Activation Phrase"],
        },
        {
            "video_id": "trusted-weak",
            "title": "6.7M",
            "username": "official.account",
        },
    ]

    accepted, review, rejected, scored = filter_candidates(
        candidates,
        profile,
        platform="tiktok",
        mode="filter",
        review_action="skip",
    )

    assert [item["video_id"] for item in accepted] == ["anchored"]
    assert [item["video_id"] for item in review] == ["trusted-weak"]
    assert {item["video_id"] for item in rejected} == {"acronym-collision", "activation-only"}
    trusted_result = next(item for item in scored if item["candidate"]["video_id"] == "trusted-weak")
    assert trusted_result["decision"] == "review"
    assert trusted_result["needs_metadata_hydration"] is True


def test_operasi_pesta_copet_false_positives_do_not_qualify(tmp_path):
    payload = campaign_payload()
    payload["name"] = "operasi_pesta_copet"
    payload["keywords"] = {
        "core": ["Operasi Pesta Copet", "OPC"],
        "hashtags": ["OperasiPestaCopet"],
        "entities": ["Imajinari", "Iqbaal Ramadhan"],
        "optional": ["copet", "pesta pora"],
        "campaign": ["bungkus gorengan", "buron wrap"],
        "exclusions": ["opel astra"],
    }
    payload["accounts"] = {"tiktok": [{"handle": "imajinari.id"}]}
    payload["relevance"] = {
        "mode": "filter",
        "review_action": "skip",
        "accept_threshold": 60,
        "review_threshold": 30,
        "require_anchor": True,
        "anchor_terms": ["Operasi Pesta Copet", "OperasiPestaCopet"],
        "ambiguous_terms": ["OPC"],
        "context_terms": ["film", "trailer", "bioskop", "pencopet"],
    }
    spec = normalize_campaign_spec(payload)
    state = tmp_path / "state"
    state.mkdir()
    _, profile_path = write_campaign_state(spec, {"state": state})
    profile = load_topic_profile(str(profile_path))

    movie = score_candidate({
        "title": "Operasi Pesta Copet official trailer",
        "username": "publisher",
    }, profile, platform="youtube")
    car = score_candidate({
        "title": "Opel Astra OPC custom build",
        "username": "cars",
        "matched_keywords": ["OPC"],
    }, profile, platform="tiktok")
    food = score_candidate({
        "title": "Tutorial bungkus gorengan dengan kertas",
        "username": "cooking",
        "matched_keywords": ["bungkus gorengan"],
    }, profile, platform="tiktok")
    weak_account_post = score_candidate({
        "title": "3M",
        "username": "imajinari.id",
    }, profile, platform="tiktok")

    assert movie["decision"] == "accept"
    assert car["decision"] == "reject"
    assert car["hard_exclusion"] is True
    assert food["decision"] == "reject"
    assert weak_account_post["decision"] == "review"
    assert weak_account_post["needs_metadata_hydration"] is True


def test_tiktok_oembed_preflight_replaces_metric_only_card_metadata():
    candidate = {
        "url": "https://www.tiktok.com/@imajinari.id/video/123",
        "title": "6.7M",
        "caption": "6.7M",
        "username": "imajinari.id",
    }

    merged = TikTokAPIIntegration._merge_oembed_metadata(candidate, {
        "title": "Operasi Pesta Copet official trailer",
        "author_name": "Imajinari",
        "author_url": "https://www.tiktok.com/@imajinari.id",
        "thumbnail_url": "https://example.test/cover.jpg",
    })

    assert merged is True
    assert candidate["title"] == "Operasi Pesta Copet official trailer"
    assert candidate["caption"] == "Operasi Pesta Copet official trailer"
    assert candidate["username"] == "imajinari.id"
    assert candidate["creator_display_name"] == "Imajinari"
    assert candidate["metadata_method"] == "tiktok_oembed"


def test_tiktok_ai_tools_search_variants_cover_related_subtopics():
    variants = TikTokAPIIntegration(enable_api=False)._build_search_variants(
        "AI tools"
    )

    assert variants[0] == "AI tools"
    assert len(variants) == len(set(item.casefold() for item in variants))
    assert {
        f"AI tools {dt.datetime.now().year}",
        "free AI tools",
        "AI productivity tools",
        "AI video tools",
        "AI coding tools",
        "AI automation tools",
        "AI tools for business",
    }.issubset(set(variants))


def test_generic_tiktok_search_expands_past_one_twelve_result_query():
    class MultiQueryIntegration(TikTokAPIIntegration):
        def __init__(self):
            super().__init__(enable_api=False)
            self.queries = []

        async def _capture_search_api_pages(self, page, keyword, max_pages):
            query_index = len(self.queries)
            self.queries.append(keyword)
            start = query_index * 12 + 1
            return [{
                "url": (
                    "https://www.tiktok.com/api/search/item/full/"
                    f"?keyword={keyword}"
                ),
                "http": 200,
                "bodyLen": 1000,
                "data": {
                    "status_code": 0,
                    "has_more": False,
                    "cursor": 12,
                    "item_list": [
                        {
                            "id": str(post_id),
                            "desc": f"{keyword} post {post_id}",
                            "author": {
                                "unique_id": f"creator_{post_id}",
                            },
                            "stats": {
                                "playCount": 1000,
                                "diggCount": 100,
                                "commentCount": 2,
                                "shareCount": 1,
                            },
                        }
                        for post_id in range(start, start + 12)
                    ],
                },
            }]

        async def _get_search_template_url(self, page, keyword):
            raise AssertionError(
                "signed replay must not run after valid live captures"
            )

    integration = MultiQueryIntegration()
    videos = asyncio.run(
        integration.discover_search_videos(
            object(),
            "creator economy",
            max_offsets=1,
            include_related_queries=True,
            target_count=50,
        )
    )

    assert integration.queries[0] == "creator economy"
    assert len(integration.queries) >= 5
    assert len(videos) >= 50
    assert len({video["id"] for video in videos}) == len(videos)
    assert integration.last_search_diagnostics["candidate_target"] == 50
    assert integration.last_search_diagnostics["candidate_count"] >= 50
    assert (
        integration.last_search_diagnostics["stop_reason"]
        == "candidate_target_reached"
    )


def test_tiktok_search_variant_plan_has_no_fixed_fifty_or_sixty_four_ceiling():
    target_count = 900
    variants = TikTokAPIIntegration(enable_api=False)._build_search_variants(
        "3D printing",
        target_count=target_count,
    )

    expected_query_goal = ((target_count + 5) // 6) + 4
    assert len(variants) == expected_query_goal
    assert len(variants) > 64
    assert len(variants) == len(set(item.casefold() for item in variants))
    assert all("3d printing" in item.casefold() for item in variants)


def test_related_tiktok_queries_deduplicate_before_target_counting():
    class DuplicateQueryIntegration(TikTokAPIIntegration):
        def __init__(self):
            super().__init__(enable_api=False)
            self.query_index = 0

        def _build_search_variants(self, keyword, *, target_count=0):
            return [f"{keyword} variant {index}" for index in range(1, 8)]

        async def _capture_search_api_pages(self, page, keyword, max_pages):
            result_sets = [
                range(1, 13),
                range(1, 13),
                range(13, 25),
                range(25, 37),
                range(37, 49),
                range(49, 61),
                range(61, 73),
            ]
            post_ids = result_sets[self.query_index]
            query_index = self.query_index
            self.query_index += 1
            return [{
                "url": (
                    "https://www.tiktok.com/api/search/item/full/"
                    f"?keyword={keyword}"
                ),
                "http": 200,
                "bodyLen": 1000,
                "data": {
                    "status_code": 0,
                    "has_more": False,
                    "item_list": [
                        {
                            "id": str(post_id),
                            "desc": (
                                f"first metadata {post_id}"
                                if query_index == 0
                                else f"later metadata {post_id}"
                            ),
                            "author": {
                                "unique_id": f"creator_{post_id}",
                            },
                            "stats": {"commentCount": 2},
                        }
                        for post_id in post_ids
                    ],
                },
            }]

    integration = DuplicateQueryIntegration()
    videos = asyncio.run(
        integration.discover_search_videos(
            object(),
            "creator economy",
            max_offsets=1,
            include_related_queries=True,
            target_count=50,
        )
    )

    ids = [video["id"] for video in videos]
    assert integration.query_index == 6
    assert len(ids) == 60
    assert len(ids) == len(set(ids))
    assert ids.count("1") == 1
    assert next(video for video in videos if video["id"] == "1")[
        "caption"
    ] == "first metadata 1"
    assert integration.last_search_diagnostics["candidate_count"] == 60


def test_tiktok_empty_http_200_is_reported_as_bad_session_not_zero_results():
    class FakeContext:
        async def cookies(self, urls=None):
            return []

    class FakeLocator:
        async def inner_text(self, timeout=None):
            return "No results found. Log in"

    class FakePage:
        context = FakeContext()

        async def evaluate(self, script, arg=None):
            if arg is None:
                return [
                    "https://www.tiktok.com/api/search/item/full/"
                    "?keyword=Operasi+Pesta+Copet&offset=0&count=12"
                ]
            return {"http": 200, "bodyLen": 0, "data": None}

        async def wait_for_timeout(self, timeout):
            return None

        def locator(self, selector):
            return FakeLocator()

    integration = TikTokAPIIntegration(enable_api=False)
    with pytest.raises(TikTokSearchSessionError, match="not a confirmed zero-result"):
        asyncio.run(
            integration.discover_search_videos(
                FakePage(),
                "Operasi Pesta Copet",
                max_offsets=1,
            )
        )


def test_tiktok_live_search_capture_is_used_before_signed_replay():
    class LiveCaptureIntegration(TikTokAPIIntegration):
        async def _capture_search_api_pages(self, page, keyword, max_pages):
            return [{
                "url": "https://www.tiktok.com/api/search/general/full/?keyword=pharmacy",
                "http": 200,
                "bodyLen": 250,
                "data": {
                    "status_code": 0,
                    "has_more": False,
                    "data": [{
                        "item": {
                            "id": "123456789",
                            "desc": "Pharmacy education",
                            "author": {"unique_id": "pharmacy_id"},
                            "stats": {"commentCount": 4},
                        }
                    }],
                },
            }]

        async def _get_search_template_url(self, page, keyword):
            raise AssertionError("signed replay fallback must not run after a valid live capture")

    videos = asyncio.run(
        LiveCaptureIntegration(enable_api=False).discover_search_videos(
            object(),
            "pharmacy",
            max_offsets=2,
        )
    )

    assert [video["id"] for video in videos] == ["123456789"]
    assert videos[0]["caption"] == "Pharmacy education"
    assert videos[0]["comment_count"] == 4
    assert videos[0]["discovery_method"] == "tiktok_search_api_live_capture"


def test_tiktok_live_search_accepts_result_bearing_nonzero_status():
    class LiveCaptureIntegration(TikTokAPIIntegration):
        async def _capture_search_api_pages(self, page, keyword, max_pages):
            return [{
                "url": "https://www.tiktok.com/api/search/item/full/?keyword=AI+video+editing",
                "http": 200,
                "bodyLen": 282730,
                "data": {
                    "status_code": 403,
                    "has_more": False,
                    "cursor": 12,
                    "item_list": [{
                        "id": "7663593751851437320",
                        "desc": "AI video editing tip",
                        "author": {"unique_id": "ai_editor"},
                        "stats": {
                            "playCount": 1000,
                            "diggCount": 100,
                            "commentCount": 12,
                        },
                    }],
                },
            }]

        async def _get_search_template_url(self, page, keyword):
            raise AssertionError("signed replay fallback must not run after a result-bearing capture")

    integration = LiveCaptureIntegration(enable_api=False)
    videos = asyncio.run(
        integration.discover_search_videos(
            object(),
            "AI video editing",
            max_offsets=2,
        )
    )

    assert [video["id"] for video in videos] == ["7663593751851437320"]
    assert videos[0]["caption"] == "AI video editing tip"
    assert videos[0]["username"] == "ai_editor"
    assert videos[0]["url"] == "https://www.tiktok.com/@ai_editor/video/7663593751851437320"
    assert videos[0]["comment_count"] == 12
    assert videos[0]["discovery_method"] == "tiktok_search_api_live_capture"
    assert integration.last_search_diagnostics["method"] == "live_capture"
    assert integration.last_search_diagnostics["pages_received"] == 1
    assert integration.last_search_diagnostics["candidate_count"] == 1
    assert integration.last_search_diagnostics["stop_reason"] == "source_exhausted"


def test_tiktok_live_search_rejects_nonzero_status_without_extractable_video():
    class InvalidCaptureIntegration(TikTokAPIIntegration):
        async def _capture_search_api_pages(self, page, keyword, max_pages):
            return [{
                "url": "https://www.tiktok.com/api/search/item/full/?keyword=AI+video+editing",
                "http": 200,
                "bodyLen": 120,
                "data": {
                    "status_code": 403,
                    "has_more": False,
                    "item_list": [{"error": "blocked"}],
                },
            }]

        async def _search_session_diagnostics(self, page):
            return {
                "authenticated": True,
                "page_no_results": False,
                "login_prompt": False,
            }

    integration = InvalidCaptureIntegration(enable_api=False)
    with pytest.raises(TikTokSearchSessionError, match="not a confirmed zero-result"):
        asyncio.run(
            integration.discover_search_videos(
                object(),
                "AI video editing",
                max_offsets=2,
            )
        )


def test_instagram_cdp_url_uses_shared_browser_setting(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_CDP_URL", "http://127.0.0.1:9223")
    monkeypatch.setenv("INSTAGRAM_USER_DATA_DIR", "ignored-when-cdp-is-selected")

    assert InstagramScraper()._cdp_url() == "http://127.0.0.1:9223"


def test_date_filter_skips_known_outside_dates_but_keeps_unknown_dates():
    windows = normalize_windows([{
        "name": "campaign",
        "start": "2026-07-01T00:00:00+07:00",
        "end": "2026-07-31T23:59:59+07:00",
    }])
    candidates = [
        {"video_id": "inside", "published_at": "2026-07-15T12:00:00+07:00"},
        {"video_id": "outside", "published_at": "2026-06-30T23:59:59+07:00"},
        {"video_id": "unknown", "title": "No date exposed by discovery"},
    ]

    kept, audit = filter_candidates_by_date(candidates, windows)

    assert [item["video_id"] for item in kept] == ["inside", "unknown"]
    assert [item["decision"] for item in audit] == ["within_date", "outside_date", "unknown_date"]
    fixed_now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.timezone.utc)
    assert parse_datetime("2 days ago", now=fixed_now) == fixed_now - dt.timedelta(days=2)
    assert parse_datetime("4d ago", now=fixed_now) == fixed_now - dt.timedelta(days=4)
    assert parse_datetime("12 h lalu", now=fixed_now) == fixed_now - dt.timedelta(days=12)
    assert parse_datetime("12h ago", now=fixed_now) == fixed_now - dt.timedelta(hours=12)


def test_date_filter_extracts_embedded_platform_card_dates():
    windows = normalize_windows([{
        "name": "campaign",
        "start": "2026-07-01T00:00:00+07:00",
        "end": "2026-07-31T23:59:59+07:00",
    }])
    candidates = [
        {"video_id": "facebook-old", "title": "Allama Iqbal\n12 Sep 2009 - 1.8K views"},
        {"video_id": "tiktok-old", "caption": "Pestapora highlights\n2025-9-8"},
        {"video_id": "inside", "caption": "Campaign trailer\n2026-7-10"},
        {"video_id": "unknown", "caption": "No date metadata"},
    ]

    kept, audit = filter_candidates_by_date(candidates, windows)

    assert [item["video_id"] for item in kept] == ["inside", "unknown"]
    assert [item["decision"] for item in audit] == [
        "outside_date",
        "outside_date",
        "within_date",
        "unknown_date",
    ]
    assert candidates[2]["published_at"].startswith("2026-07-10T00:00:00")
    assert parse_datetime("3M") is None


def test_candidate_batch_deduplicates_canonical_input(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({
        "videos": [
            {"platform": "youtube", "video_id": "same", "url": "https://www.youtube.com/watch?v=same"},
            {"platform": "youtube", "id": "same", "url": "https://youtu.be/same"},
            {"platform": "tiktok", "video_id": "other", "url": "https://www.tiktok.com/@x/video/other"},
        ]
    }), encoding="utf-8")

    candidates = load_candidate_file(path, "youtube")

    assert len(candidates) == 1
    assert candidates[0]["video_id"] == "same"


def test_platform_url_id_precedes_inconsistent_api_ids_for_deduplication():
    instagram_url = "https://www.instagram.com/reel/ShortCode123/?utm_source=test"
    first = candidate_content_keys("instagram", {
        "video_id": "ShortCode123",
        "media_id": "111222333",
        "url": instagram_url,
    })
    second = candidate_content_keys("instagram", {
        "video_id": "111222333",
        "media_id": "111222333_999",
        "url": "https://www.instagram.com/reel/ShortCode123/",
    })

    assert first[0] == "ShortCode123"
    assert second[0] == "ShortCode123"


def test_query_string_content_ids_do_not_share_collection_url_keys():
    first_youtube = {
        "video_id": "first-video",
        "url": "https://www.youtube.com/watch?v=first-video&utm_source=test",
    }
    second_youtube = {
        "video_id": "second-video",
        "url": "https://www.youtube.com/watch?v=second-video",
    }
    first_keys = set(candidate_content_keys("youtube", first_youtube))
    second_keys = set(candidate_content_keys("youtube", second_youtube))

    assert "https://www.youtube.com/watch" not in first_keys
    assert "https://www.youtube.com/watch" not in second_keys
    assert first_keys.isdisjoint(second_keys)
    fresh, skipped, _ = filter_new_candidates(
        [second_youtube],
        "youtube",
        first_keys,
    )
    assert fresh == [second_youtube]
    assert skipped == []

    facebook_keys = candidate_content_keys("facebook", {
        "url": "https://www.facebook.com/story.php?story_fbid=111&id=222",
    })
    assert facebook_keys[0] == "111"
    assert "https://www.facebook.com/story.php" not in facebook_keys

    x_keys = candidate_content_keys("x", {
        "url": "https://twitter.com/official/status/1912345678901234567?s=20",
    })
    assert x_keys[0] == "1912345678901234567"


def test_x_source_watermark_keeps_newest_observed_post_id():
    conn = memory_db()
    update_platform_state(conn, "project", "x", "campaign", "run-1", "2026-07-14T10:00:00Z", since_id="100")
    update_platform_state(conn, "project", "x", "campaign", "run-2", "2026-07-14T11:00:00Z", since_id="99")
    row = conn.execute(
        "SELECT since_id FROM platform_state WHERE project = 'project' AND platform = 'x' AND keyword = 'campaign'"
    ).fetchone()

    assert row[0] == "100"
    assert max_x_post_id([{
        "content_key": "101",
        "candidate": {"url": "https://x.com/official/status/101"},
    }]) == "101"


def test_compiler_includes_x_posts_and_nested_replies(tmp_path):
    conn = memory_db()
    ingest_videos(
        conn,
        project="project",
        keyword="campaign",
        platform="x",
        videos=[{
            "video_id": "100",
            "url": "https://x.com/official/status/100",
            "caption": "Campaign",
            "content_creator": "official",
            "published_at": "2026-07-14T08:00:00Z",
            "like_count": 10,
            "reported_comment_count": 2,
            "discovery_method": "x_api_v2_recent_search",
            "metadata_method": "x_api_v2",
            "comment_method": "x_api_v2_conversation_search",
            "comments_exhausted": True,
            "comments": [{
                "comment_id": "201",
                "author": "reader",
                "text": "First",
                "is_reply": True,
                "x_graphql_tweet": {"legacy": {"full_text": "First"}},
                "x_graphql_author": {"legacy": {"screen_name": "reader"}},
                "replies": [{
                    "comment_id": "202",
                    "author": "second_reader",
                    "text": "Nested",
                    "is_reply": True,
                }],
            }],
        }],
        run_id="x-run",
        scraped_at="2026-07-14T10:00:00Z",
    )
    paths = {"latest": tmp_path / "latest", "reports": tmp_path / "reports"}

    result = compile_outputs(conn, "project", "campaign", paths)
    compiled_text = (paths["latest"] / "combined_all_platforms_comments.json").read_text(encoding="utf-8")
    compiled = json.loads(compiled_text)
    technical = json.loads((paths["latest"] / "technical_bundle.json").read_text(encoding="utf-8"))
    manifest = json.loads((paths["latest"] / "dataset_manifest.json").read_text(encoding="utf-8"))
    post_rows = (paths["latest"] / "posts.jsonl").read_text(encoding="utf-8").splitlines()
    comment_rows = (paths["latest"] / "comments.jsonl").read_text(encoding="utf-8").splitlines()

    assert result["summary_by_platform"]["x"]["videos"] == 1
    assert result["summary_by_platform"]["x"]["flat_comments"] == 2
    assert result["summary_by_platform"]["x"]["top_level_comments"] == 1
    assert [video["platform"] for video in compiled["videos"]] == ["x"]
    assert compiled["schema_version"] == "3.0"
    assert compiled_text.count("\n") > 1
    assert "x_graphql_tweet" not in compiled["videos"][0]["comments"][0]
    assert "x_graphql_author" not in compiled["videos"][0]["comments"][0]
    assert technical["videos"][0]["comments"][0]["x_graphql_tweet"]["legacy"]["full_text"] == "First"
    assert len(post_rows) == 1
    assert len(comment_rows) == 2
    assert manifest["record_counts"] == {"posts": 1, "comments": 2, "profiles": 3}
    assert manifest["artifacts"]["technical_bundle"]["file"] == "technical_bundle.json"
    assert compiled["videos"][0]["comments"][0]["thread_depth"] == 0
    assert compiled["videos"][0]["comments"][1]["thread_depth"] == 1
    assert result["technical_bundle_json"].endswith("technical_bundle.json")
    assert result["comments_jsonl"].endswith("comments.jsonl")
    assert (paths["latest"] / "metrics_coverage.json").exists()


def test_metric_coverage_separates_available_zero_and_first_party_metrics():
    report = build_metric_coverage_report(
        [
            {
                "platform": "tiktok",
                "metric_availability": {"views": "available", "likes": "missing_from_public_response"},
                "brief_evidence": {"engagement_snapshot": {"views": 0, "likes": None}},
                "collection_status": {"completion_status": "complete"},
                "comments": [{"comment_created_at": "2026-07-14T10:00:00+07:00"}],
            }
        ],
        project="project",
        compiled_at="2026-07-14T10:00:00+07:00",
    )

    tiktok = report["by_platform"]["tiktok"]["metrics"]
    assert tiktok["views"]["available_records"] == 1
    assert tiktok["views"]["records_with_nonzero_value"] == 0
    assert tiktok["likes"]["available_records"] == 0
    assert report["first_party_only"]["reach"]["status"] == "first_party_required"
    assert report["first_party_only"]["demographics"]["public_comment_inference_allowed"] is False


def test_candidate_ledger_preserves_date_rejection_and_cross_source_overlap(tmp_path):
    conn = memory_db()
    session = tmp_path / "raw"
    logs = session / "logs"
    logs.mkdir(parents=True)
    (logs / "date_youtube_candidates.json").write_text(json.dumps({
        "candidates": [{
            "decision": "outside_date",
            "published_at": "2026-06-01T00:00:00+07:00",
            "candidate": {
                "video_id": "shared-id",
                "url": "https://www.youtube.com/watch?v=shared-id",
                "title": "Observed but outside campaign",
            },
        }]
    }), encoding="utf-8")
    records = collect_run_candidates(session, "youtube", [])
    assert records[0]["extraction_status"] == "outside_date"
    assert "date_window" in records[0]["audit"]

    base_run = {
        "started_at": "2026-07-12T10:00:00",
        "finished_at": "2026-07-12T10:01:00",
        "status": "success",
        "error": "",
    }
    first = record_discovery_run(
        conn,
        project="project",
        platform="youtube",
        source={"source_key": "source-one", "kind": "query", "value": "one"},
        run_meta={**base_run, "run_id": "run-one"},
        candidates=records,
    )
    second = record_discovery_run(
        conn,
        project="project",
        platform="youtube",
        source={"source_key": "source-two", "kind": "query", "value": "two"},
        run_meta={**base_run, "run_id": "run-two"},
        candidates=records,
    )

    row = conn.execute("SELECT * FROM candidate_ledger").fetchone()
    assert first == {"observed_candidates": 1, "new_candidates": 1}
    assert second == {"observed_candidates": 1, "new_candidates": 0}
    assert json.loads(row["source_keys_json"]) == ["source-one", "source-two"]
    assert row["discovery_count"] == 2
    assert row["extraction_status"] == "outside_date"


def test_metrics_refresh_and_direct_refresh_batch_are_persisted(tmp_path):
    conn = memory_db()
    video = {
        "video_id": "metric-id",
        "url": "https://www.youtube.com/watch?v=metric-id",
        "caption": "Campaign video",
        "published_at": "2026-07-10T10:00:00+07:00",
        "view_count": "1.2K",
        "like_count": 100,
        "comments": [],
    }
    ingest_videos(
        conn,
        project="project",
        keyword="campaign",
        platform="youtube",
        videos=[video],
        run_id="run-one",
        scraped_at="2026-07-10T10:00:00",
    )
    video.update({"view_count": 2400, "like_count": 180})
    ingest_videos(
        conn,
        project="project",
        keyword="campaign",
        platform="youtube",
        videos=[video],
        run_id="run-two",
        scraped_at="2026-07-10T12:00:00",
    )

    current = conn.execute(
        "SELECT view_count, like_count, published_at FROM content_items"
    ).fetchone()
    assert dict(current) == {
        "view_count": 2400,
        "like_count": 180,
        "published_at": "2026-07-10T10:00:00+07:00",
    }
    assert conn.execute("SELECT COUNT(*) FROM content_metric_snapshots").fetchone()[0] == 2

    state = tmp_path / "state"
    state.mkdir()
    refresh_path, count = export_direct_refresh_candidates(
        conn,
        "project",
        "youtube",
        {"state": state},
        limit=10,
        after_hours=0,
    )
    payload = json.loads(refresh_path.read_text(encoding="utf-8"))
    assert count == 1
    assert payload["videos"][0]["video_id"] == "metric-id"
    assert payload["videos"][0]["url"].endswith("v=metric-id")


def test_compiled_reports_exclude_stored_content_outside_campaign_window(tmp_path):
    conn = memory_db()
    conn.execute(
        "INSERT INTO project_meta(key, value) VALUES ('date_windows', ?)",
        (json.dumps([{
            "name": "campaign",
            "start": "2026-07-01T00:00:00+07:00",
            "end": "2026-07-31T23:59:59+07:00",
        }]),),
    )
    for video_id, published_at in (
        ("inside", "2026-07-10T12:00:00+07:00"),
        ("outside", "2026-06-29T12:00:00+07:00"),
    ):
        ingest_videos(
            conn,
            project="project",
            keyword="campaign",
            platform="youtube",
            videos=[{
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "caption": video_id,
                "published_at": published_at,
                "comments": [],
            }],
            run_id=f"run-{video_id}",
            scraped_at="2026-07-12T10:00:00+07:00",
        )
    paths = {
        "latest": tmp_path / "latest",
        "reports": tmp_path / "reports",
    }

    result = compile_outputs(conn, "project", "campaign", paths)
    compiled = json.loads((paths["latest"] / "combined_all_platforms_comments.json").read_text(encoding="utf-8"))

    assert result["videos"] == 1
    assert result["outside_date_videos_excluded"] == 1
    assert [video["video_id"] for video in compiled["videos"]] == ["inside"]


def test_compiled_comments_export_recursive_evidence(tmp_path):
    conn = memory_db()
    ingest_videos(
        conn,
        project="project",
        keyword="campaign",
        platform="tiktok",
        videos=[{
            "video_id": "video-1",
            "url": "https://www.tiktok.com/@publisher/video/video-1",
            "caption": "Campaign",
            "content_creator": "publisher",
            "published_at": "2026-07-10T12:00:00+07:00",
            "comment_method": "tiktok_comment_api",
            "comments": [{
                "id": "root",
                "text": "Root",
                "create_time": 1704067200,
                "user": {"unique_id": "root_user"},
                "replies": [{
                    "id": "reply",
                    "text": "Reply",
                    "create_time": 1704067260,
                    "user": {"unique_id": "reply_user"},
                }],
            }],
        }],
        run_id="run-one",
        scraped_at="2026-07-12T10:00:00+07:00",
    )
    paths = {"latest": tmp_path / "latest", "reports": tmp_path / "reports"}

    compile_outputs(conn, "project", "campaign", paths)
    compiled = json.loads(
        (paths["latest"] / "combined_all_platforms_comments.json").read_text(encoding="utf-8")
    )
    comments = compiled["videos"][0]["comments"]
    comments_by_id = {comment["comment_id"]: comment for comment in comments}

    assert len(comments) == 2
    assert comments_by_id["root"]["collection_method"] == "tiktok_comment_api"
    assert comments_by_id["reply"]["parent_comment_id"] == "root"
    assert comments_by_id["reply"]["is_reply"] is True
    assert comments_by_id["reply"]["comment_evidence"]["identity"]["content_id"] == "video-1"
    assert comments_by_id["reply"]["comment_evidence"]["author"]["username_or_name"] == "reply_user"


def test_coverage_report_exposes_overlap_and_metadata_quality(tmp_path):
    conn = memory_db()
    candidate = {
        "content_key": "covered-id",
        "candidate": {
            "video_id": "covered-id",
            "url": "https://www.youtube.com/watch?v=covered-id",
            "title": "Covered campaign video",
            "published_at": "2026-07-10T10:00:00+07:00",
        },
        "decision": "accept",
        "score": 90,
        "extraction_status": "extracted",
        "audit": {},
    }
    run = {
        "started_at": "2026-07-12T10:00:00",
        "finished_at": "2026-07-12T10:01:00",
        "status": "success",
        "error": "",
    }
    for index in (1, 2):
        record_discovery_run(
            conn,
            project="project",
            platform="youtube",
            source={"source_key": f"source-{index}", "kind": "query", "value": str(index)},
            run_meta={**run, "run_id": f"run-{index}"},
            candidates=[candidate],
        )
    ingest_videos(
        conn,
        project="project",
        keyword="campaign",
        platform="youtube",
        videos=[{**candidate["candidate"], "view_count": 10, "comments": []}],
        run_id="run-2",
        scraped_at="2026-07-12T10:01:00",
    )
    latest = tmp_path / "latest"
    latest.mkdir()

    report = compile_coverage_report(conn, "project", {"latest": latest})
    youtube = report["platforms"]["youtube"]

    assert youtube["configured_sources_run"] == 2
    assert youtube["candidate_observations"] == 2
    assert youtube["unique_candidates"] == 1
    assert youtube["repeat_observations"] == 1
    assert youtube["multi_source_candidates"] == 1
    assert youtube["metadata_completeness_percent"]["published_at"] == 100.0
    assert (latest / "coverage.json").exists()


def test_coverage_report_includes_pending_source_plan_denominator(tmp_path):
    conn = memory_db()
    sources = [
        {
            "platform": "youtube",
            "source_key": "planned-one",
            "kind": "query",
            "value": "one",
            "label": "one",
            "role": "core",
        },
        {
            "platform": "youtube",
            "source_key": "planned-two",
            "kind": "query",
            "value": "two",
            "label": "two",
            "role": "core",
        },
    ]
    register_source_plan(conn, "project", sources, "digest")
    record_discovery_run(
        conn,
        project="project",
        platform="youtube",
        source=sources[0],
        run_meta={
            "run_id": "run-one",
            "started_at": "2026-07-12T10:00:00+07:00",
            "finished_at": "2026-07-12T10:01:00+07:00",
            "status": "success",
            "error": "",
        },
        candidates=[],
    )
    latest = tmp_path / "latest"
    latest.mkdir()

    report = compile_coverage_report(conn, "project", {"latest": latest})
    youtube = report["platforms"]["youtube"]

    assert youtube["configured_sources"] == 2
    assert youtube["configured_sources_run"] == 1
    assert youtube["configured_sources_successful"] == 1
    assert youtube["configured_sources_pending"] == 1
    assert youtube["source_completion_percent"] == 50.0


def test_partial_platform_source_plan_preserves_other_platforms():
    conn = memory_db()
    youtube = {
        "platform": "youtube",
        "source_key": "youtube-source",
        "kind": "query",
        "value": "youtube",
        "label": "youtube",
        "role": "core",
    }
    instagram = {
        "platform": "instagram",
        "source_key": "instagram-source",
        "kind": "query",
        "value": "instagram",
        "label": "instagram",
        "role": "core",
    }
    register_source_plan(conn, "project", [youtube, instagram], "digest-one")
    register_source_plan(conn, "project", [instagram], "digest-two")

    active = {
        row["platform"]: row["active"]
        for row in conn.execute("SELECT platform, active FROM discovery_sources")
    }
    assert active == {"youtube": 1, "instagram": 1}


def test_youtube_channel_urls_are_recognized_for_inventory_discovery():
    scraper = YouTubeScraper()
    assert scraper.is_channel_url("https://www.youtube.com/@GKPSTV/videos")
    assert scraper.is_channel_url("https://www.youtube.com/channel/UC1234567890")
    assert not scraper.is_channel_url("https://www.youtube.com/watch?v=abc")


def test_campaign_compile_only_cli_smoke(tmp_path, monkeypatch):
    spec_path = tmp_path / "campaign.json"
    spec_path.write_text(json.dumps(campaign_payload()), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCRAPER_TIMEZONE", "UTC")
    monkeypatch.setattr(sys, "argv", [
        "incremental_project.py",
        "--campaign-spec",
        str(spec_path),
        "--compile-only",
    ])

    incremental_main()

    project = tmp_path / "comments_data" / "project_reusable_campaign"
    assert (project / "state" / "scrape_state.sqlite").exists()
    assert (project / "compiled" / "latest" / "coverage.json").exists()
    assert (project / "state" / "campaign_relevance_profile.json").exists()
    coverage = json.loads((project / "compiled" / "latest" / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["generated_at"].endswith("+07:00")
