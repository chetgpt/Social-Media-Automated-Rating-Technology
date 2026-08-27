import asyncio

from tiktok_scraper.profile_contract import normalize_profile_record
from tiktok_scraper.profile_enrichment import PublicProfileCollector
from tiktok_scraper.profile_web_api import (
    decode_json_payloads,
    merge_profile_records,
    parse_facebook_about_text_blocks,
    parse_facebook_profile_payload,
    parse_instagram_profile_payload,
    parse_tiktok_profile_payload,
    parse_x_profile_payload,
    parse_youtube_profile_payload,
    profile_response_url_matches,
)


def test_tiktok_web_user_detail_parser_preserves_account_region_and_stats():
    record = parse_tiktok_profile_payload(
        {
            "userInfo": {
                "user": {
                    "id": "tt-1",
                    "uniqueId": "public_user",
                    "nickname": "Public User",
                    "signature": "Researcher, she/her",
                    "region": "ID",
                    "verified": False,
                    "bioLink": {"link": "https://example.com"},
                },
                "stats": {
                    "followerCount": 1200,
                    "followingCount": 50,
                    "videoCount": 20,
                    "heartCount": 8000,
                },
            }
        },
        expected_username="public_user",
    )

    assert record["user_id"] == "tt-1"
    assert record["country_code"] == "ID"
    assert record["geography_basis"] == "platform_account_region"
    assert record["follower_count"] == 1200
    assert record["_method"] == "tiktok_web_user_detail_api"


def test_instagram_web_profile_parser_gets_business_geo_and_structured_pronouns():
    record = parse_instagram_profile_payload(
        {
            "data": {
                "user": {
                    "id": "ig-1",
                    "username": "studio",
                    "full_name": "Studio",
                    "biography": "Public studio",
                    "pronouns": ["they/them"],
                    "is_professional_account": True,
                    "category_name": "Design",
                    "business_address_json": (
                        '{"street_address":"Jalan Merdeka","city_name":"Jakarta",'
                        '"country_name":"Indonesia","zip_code":"10110"}'
                    ),
                    "edge_followed_by": {"count": 300},
                    "edge_follow": {"count": 12},
                    "edge_owner_to_timeline_media": {"count": 40},
                }
            }
        },
        expected_username="studio",
    )

    assert record["location"]["city"] == "Jakarta"
    assert record["location"]["country"] == "Indonesia"
    assert record["geography_basis"] == "public_business_address"
    assert record["pronouns"] == ["they/them"]
    assert record["account_type"] == "professional"


def test_x_graphql_parser_selects_requested_user_not_viewer():
    payload = {
        "data": {
            "viewer": {
                "__typename": "User",
                "rest_id": "viewer-1",
                "legacy": {
                    "screen_name": "logged_in_viewer",
                    "name": "Viewer",
                    "followers_count": 999,
                },
            },
            "user": {
                "result": {
                    "__typename": "User",
                    "rest_id": "x-1",
                    "is_blue_verified": True,
                    "core": {
                        "screen_name": "target",
                        "name": "Target User",
                        "created_at": "2020-01-01",
                    },
                    "legacy": {
                        "screen_name": "target",
                        "name": "Target User",
                        "description": "Based in Bandung",
                        "location": "Bandung",
                        "followers_count": 75,
                        "friends_count": 8,
                        "statuses_count": 90,
                        "favourites_count": 15,
                    },
                    "professional": {
                        "professional_type": "Creator",
                        "category": [{"name": "Journalist"}],
                    },
                }
            },
        }
    }

    record = parse_x_profile_payload(payload, expected_username="target")
    assert record["user_id"] == "x-1"
    assert record["username"] == "target"
    assert record["location"] == "Bandung"
    assert record["category"] == "Journalist"
    assert record["follower_count"] == 75


def test_youtube_innertube_parser_combines_split_channel_metadata():
    payload = {
        "metadata": {
            "channelMetadataRenderer": {
                "externalId": "UC123",
                "title": "Example Channel",
                "description": "Channel description",
                "vanityChannelUrl": "https://www.youtube.com/@example",
                "avatar": {
                    "thumbnails": [
                        {"url": "https://img/small.jpg", "width": 88},
                        {"url": "https://img/large.jpg", "width": 800},
                    ]
                },
            }
        },
        "header": {
            "c4TabbedHeaderRenderer": {
                "channelId": "UC123",
                "subscriberCountText": {"simpleText": "1.2K subscribers"},
                "videosCountText": {"simpleText": "45 videos"},
            }
        },
        "aboutChannelViewModel": {
            "country": "Indonesia",
            "joinedDateText": {"content": "Joined Jan 1, 2020"},
            "viewCountText": {"content": "30,000 views"},
        },
    }

    record = parse_youtube_profile_payload(payload, expected_username="example")
    assert record["user_id"] == "UC123"
    assert record["username"] == "example"
    assert record["avatar_url"] == "https://img/large.jpg"
    assert record["follower_count"] == 1200
    assert record["content_count"] == 45
    assert record["view_count"] == 30000
    assert record["country"] == "Indonesia"
    assert record["geography_basis"] == "channel_country"


def test_facebook_parser_selects_target_page_and_public_address():
    payload = {
        "data": {
            "viewer": {
                "__typename": "User",
                "id": "viewer",
                "name": "Viewer",
                "url": "https://www.facebook.com/logged.in",
            },
            "page": {
                "__typename": "Page",
                "id": "page-1",
                "name": "Target Page",
                "url": "https://www.facebook.com/target.page",
                "about": "Public organization",
                "category_name": "Community",
                "followers_count": 500,
                "address": {
                    "city": "Surabaya",
                    "region": "East Java",
                    "country": "Indonesia",
                    "latitude": -7.25,
                    "longitude": 112.75,
                },
                "profile_picture": {"uri": "https://img/page.jpg"},
            },
        }
    }

    record = parse_facebook_profile_payload(payload, expected_username="target.page")
    assert record["user_id"] == "page-1"
    assert record["username"] == "target.page"
    assert record["location"]["city"] == "Surabaya"
    assert record["geography_basis"] == "public_business_address"
    assert record["follower_count"] == 500


def test_facebook_parser_accepts_exact_target_id_without_known_typename():
    record = parse_facebook_profile_payload(
        {
            "data": {
                "profile": {
                    "__typename": "ProfilePlus",
                    "id": "10001",
                    "name": "Target Profile",
                    "url": "https://www.facebook.com/target.profile",
                    "description": "Public profile",
                    "followers_count": 25,
                    "gender_text": "Custom public statement",
                }
            }
        },
        expected_user_id="10001",
    )

    assert record["user_id"] == "10001"
    assert record["username"] == "target.profile"
    assert record["display_name"] == "Target Profile"
    assert record["follower_count"] == 25
    assert record["gender_statement"] == "Custom public statement"


def test_facebook_parser_preserves_current_city_and_hometown_separately():
    record = parse_facebook_profile_payload(
        {
            "data": {
                "profile": {
                    "__typename": "User",
                    "id": "10001",
                    "username_for_profile": "target.profile",
                    "name": "Target Profile",
                    "current_city": {"name": "Jakarta"},
                    "hometown": {"name": "Bandung"},
                }
            }
        },
        expected_user_id="10001",
    )

    assert record["location"]["raw"] == "Jakarta"
    assert [
        evidence["kind"] for evidence in record["geography_evidence"]
    ] == ["current_city", "hometown"]
    profile = normalize_profile_record(
        record,
        source="browser_session_api",
        collection_method=record["_method"],
    )
    assert profile["profile_geography"]["raw"] == "Jakarta"
    assert [
        evidence["raw"]
        for evidence in profile["profile_geography_evidence"]
    ] == ["Jakarta", "Bandung"]


def test_facebook_about_dom_parser_requires_visible_labels():
    record = parse_facebook_about_text_blocks(
        [
            "Lives in Jakarta\nPublic",
            "Kota asal\nBandung\nPublik",
            "Kata ganti\nthey/them\nPublik",
            "Tanggal lahir\n3 April\nPublik",
            "Jenis kelamin\nNon-biner\nPublik",
            "MALE",
        ],
        section="about_contact_and_basic_info",
        expected_user_id="10001",
    )

    assert [
        evidence["kind"] for evidence in record["geography_evidence"]
    ] == ["current_city", "hometown"]
    assert record["pronouns"] == ["they/them"]
    assert record["birthdate"] == "3 April"
    assert record["gender_statement"] == "Non-biner"
    assert record["_method"] == "facebook_public_about_dom"


def test_facebook_header_gender_enum_is_not_treated_as_public_demographic_text():
    record = parse_facebook_profile_payload(
        {
            "data": {
                "profile": {
                    "__typename": "User",
                    "id": "10001",
                    "username_for_profile": "target.profile",
                    "name": "Target Profile",
                    "gender": "MALE",
                    "birthdate": {"year": 1990, "month": 4, "day": 3},
                }
            }
        },
        expected_user_id="10001",
    )

    assert record["gender_statement"] == ""
    assert record["birthdate"] == ""


def test_profile_contract_labels_geo_basis_and_keeps_explicit_demographics():
    profile = normalize_profile_record(
        {
            "platform": "tiktok",
            "user_id": "tt-1",
            "username": "public_user",
            "country_code": "ID",
            "geography_basis": "platform_account_region",
            "pronouns": ["she/her"],
            "birthdate": "1990-04-03",
            "gender_statement": "Woman",
            "view_count": 25,
        },
        source="browser_session_api",
        collection_method="tiktok_web_user_detail_api",
    )

    assert profile["profile_geography"]["country_code"] == "ID"
    assert profile["profile_geography_evidence"][0]["kind"] == "profile_location"
    assert profile["profile_geography"]["evidence_type"] == "platform_account_region"
    assert profile["profile_geography"]["is_self_declared"] is False
    assert profile["self_declared"]["pronouns"] == ["she/her"]
    assert profile["self_declared"]["birthdate_statement"] == "1990-04-03"
    assert profile["self_declared"]["gender_statement"] == "Woman"
    assert profile["self_declared"]["gender_classification"] is None
    assert profile["audience_analytics"]["status"] == "first_party_required"
    assert profile["view_count"] == 25


def test_facebook_neuter_enum_is_not_treated_as_demographic_gender():
    profile = normalize_profile_record(
        {
            "platform": "facebook",
            "user_id": "page-1",
            "display_name": "Public Page",
            "gender_statement": "NEUTER",
        },
        source="browser_session_api",
        collection_method="facebook_web_profile_graphql_or_structured_data",
    )

    assert profile["self_declared"]["gender_statement"] == ""
    assert (
        profile["availability"]["self_declared_demographic_text"]
        == "not_observed"
    )


def test_response_matching_guarded_json_and_priority_merge():
    assert profile_response_url_matches(
        "x", "https://x.com/i/api/graphql/abc/UserByScreenName?variables=x"
    )
    assert not profile_response_url_matches(
        "x", "https://x.com/i/api/graphql/abc/SearchTimeline?variables=x"
    )
    assert decode_json_payloads('for (;;);{"data":{"ok":true}}') == [
        {"data": {"ok": True}}
    ]
    merged = merge_profile_records(
        [
            {"username": "target", "verified": False, "follower_count": 0, "_method": "api"},
            {"username": "target", "verified": True, "bio": "Fallback", "_method": "dom"},
        ]
    )
    assert merged["verified"] is False
    assert merged["follower_count"] == 0
    assert merged["bio"] == "Fallback"
    assert merged["_method"] == "api"
    assert merged["_supporting_methods"] == ["dom"]


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def test_profile_collector_disconnects_without_closing_shared_browser():
    collector = PublicProfileCollector(cdp_url="http://127.0.0.1:9223")
    browser = FakeBrowser()
    playwright = FakePlaywright()
    collector._browser = browser
    collector._playwright = playwright

    asyncio.run(collector.__aexit__(None, None, None))

    assert browser.closed is False
    assert playwright.stopped is True
