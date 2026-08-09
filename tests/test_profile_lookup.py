import json

import pytest

from profile_lookup import (
    candidate_from_mapping,
    candidate_from_target,
    candidate_from_url,
    candidates_from_file,
    dedupe_candidates,
)


@pytest.mark.parametrize(
    ("target", "platform", "profile_key", "profile_url"),
    [
        (
            "tiktok:@public_user",
            "tiktok",
            "username:public_user",
            "https://www.tiktok.com/@public_user",
        ),
        (
            "instagram:studio",
            "instagram",
            "username:studio",
            "https://www.instagram.com/studio/",
        ),
        (
            "twitter:account",
            "x",
            "username:account",
            "https://x.com/account",
        ),
        (
            "youtube:UC123",
            "youtube",
            "id:UC123",
            "https://www.youtube.com/channel/UC123/about",
        ),
        (
            "facebook:10001",
            "facebook",
            "id:10001",
            "https://www.facebook.com/10001/about",
        ),
    ],
)
def test_candidate_from_target_builds_canonical_navigation(
    target,
    platform,
    profile_key,
    profile_url,
):
    candidate = candidate_from_target(target)

    assert candidate["platform"] == platform
    assert candidate["profile_key"] == profile_key
    assert candidate["profile_url"] == profile_url


def test_candidate_from_url_understands_channel_and_profile_id_urls():
    youtube = candidate_from_url(
        "https://www.youtube.com/channel/UC123/videos"
    )
    facebook = candidate_from_url(
        "https://www.facebook.com/profile.php?id=10001"
    )

    assert youtube["user_id"] == "UC123"
    assert youtube["profile_url"].endswith("/channel/UC123/about")
    assert facebook["user_id"] == "10001"
    assert facebook["profile_url"].endswith("/10001/about")


def test_target_url_rejects_content_routes():
    with pytest.raises(ValueError):
        candidate_from_url("https://www.instagram.com/reel/ABC/")


def test_target_file_accepts_strings_and_identity_objects(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            [
                "x:account",
                {
                    "platform": "youtube",
                    "user_id": "UC123",
                    "display_name": "Channel",
                },
            ]
        ),
        encoding="utf-8",
    )

    candidates = candidates_from_file(path)

    assert [candidate["platform"] for candidate in candidates] == [
        "x",
        "youtube",
    ]
    assert candidates[1]["display_name"] == "Channel"


def test_mapping_url_platform_must_match():
    with pytest.raises(ValueError):
        candidate_from_mapping(
            {
                "platform": "x",
                "profile_url": "https://www.instagram.com/studio/",
            }
        )


def test_candidate_deduplication_uses_platform_and_profile_key():
    candidate = candidate_from_target("x:account")
    assert dedupe_candidates([candidate, dict(candidate)]) == [candidate]
