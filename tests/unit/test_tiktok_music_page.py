import json
from pathlib import Path
import sqlite3

import pytest

import tiktok_music_page
from tiktok_scraper.music_page_locator import (
    TikTokMusicPageURLValueError,
    build_tiktok_music_page_locator,
    normalize_tiktok_music_page_url,
)


EXACT_URL = (
    "https://www.tiktok.com/music/"
    "France-Accordion-Swing-6817155068127610882"
)


def test_exact_music_page_url_normalizes_with_numeric_id_as_authority():
    target = normalize_tiktok_music_page_url(
        EXACT_URL.replace("www.tiktok.com", "tiktok.com") + "/"
    )

    assert target == {
        "schema_version": "tiktok-music-page-locator-v1",
        "status": "observed",
        "music_id": "6817155068127610882",
        "slug": "France-Accordion-Swing",
        "url": EXACT_URL,
        "source": "supplied_exact_url",
        "online_verification": "not_attempted",
        "reason": "exact_url_normalized",
    }


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.tiktok.com/music/", "exact_music_page_required"),
        ("http://www.tiktok.com/music/France-12345", "https_required"),
        ("https://example.com/music/France-12345", "tiktok_host_required"),
        (
            "https://user@www.tiktok.com/music/France-12345",
            "authority_not_allowed",
        ),
        (
            "https://www.tiktok.com:443/music/France-12345",
            "authority_not_allowed",
        ),
        (
            "https://www.tiktok.com/music/France-12345?lang=en",
            "query_or_fragment_not_allowed",
        ),
        (
            "https://www.tiktok.com/music/France-12345#videos",
            "query_or_fragment_not_allowed",
        ),
        (
            "https://www.tiktok.com/music/France-12345/posts",
            "invalid_music_page_path",
        ),
        (
            "https://www.tiktok.com/music/France-notnumeric",
            "numeric_music_id_required",
        ),
    ],
)
def test_invalid_music_page_urls_fail_with_stable_reason(url, reason):
    with pytest.raises(TikTokMusicPageURLValueError) as captured:
        normalize_tiktok_music_page_url(url)

    assert captured.value.code == reason


def test_derived_locator_is_explicitly_not_online_verified():
    locator = build_tiktok_music_page_locator(
        "6817155068127610882",
        "France Accordion Swing",
    )

    assert locator["status"] == "derived"
    assert locator["url"] == EXACT_URL
    assert locator["online_verification"] == "not_attempted"
    assert locator["reason"] == "derived_from_music_id_and_title"


def test_observed_url_must_bind_the_same_music_id():
    locator = build_tiktok_music_page_locator(
        "6817155068127610882",
        "France Accordion Swing",
        "https://www.tiktok.com/music/Other-6817155068127610999",
    )

    assert locator["status"] == "conflict"
    assert locator["url"] == ""
    assert locator["reason"] == "observed_url_music_id_mismatch"


def _master_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE tiktok_master_posts (
                post_id TEXT PRIMARY KEY,
                canonical_url TEXT,
                creator_handle TEXT,
                last_seen_at TEXT,
                latest_evidence_hash TEXT,
                latest_evidence_json TEXT
            )
            """
        )
        evidence = {
            "music_evidence": {
                "platform_music": {
                    "music_id": "6817155068127610882",
                    "title": "France Accordion Swing",
                    "author": "Declared Artist",
                    "signed_audio_url": "https://signed.example/audio?token=secret",
                }
            }
        }
        connection.execute(
            "INSERT INTO tiktok_master_posts VALUES (?, ?, ?, ?, ?, ?)",
            (
                "7650000000000000001",
                "https://www.tiktok.com/@maker/video/7650000000000000001",
                "maker",
                "2026-08-27T00:00:00+00:00",
                "a" * 64,
                json.dumps(evidence),
            ),
        )
        connection.execute(
            "INSERT INTO tiktok_master_posts VALUES (?, ?, ?, ?, ?, ?)",
            (
                "7650000000000000002",
                "https://www.tiktok.com/@other/video/7650000000000000002",
                "other",
                "2026-08-26T00:00:00+00:00",
                "b" * 64,
                json.dumps(
                    {
                        "music_evidence": {
                            "platform_music": {"music_id": "9999999999999999999"}
                        }
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_inspect_local_reads_only_safe_matching_master_projection(tmp_path):
    database = tmp_path / "master.sqlite"
    _master_database(database)

    result = tiktok_music_page.inspect_local_master(
        url=EXACT_URL,
        master_database=database,
        limit=10,
    )

    assert result["status"] == "matched"
    assert result["known_post_count"] == 1
    assert result["returned_post_count"] == 1
    assert result["online_lookup_performed"] is False
    assert result["master_write_performed"] is False
    assert result["posts"] == [
        {
            "post_id": "7650000000000000001",
            "canonical_url": (
                "https://www.tiktok.com/@maker/video/7650000000000000001"
            ),
            "creator": "maker",
            "last_seen_at": "2026-08-27T00:00:00+00:00",
            "evidence_hash": "a" * 64,
            "music_title": "France Accordion Swing",
            "music_author": "Declared Artist",
        }
    ]
    serialized = json.dumps(result)
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized
    assert len(result["lookup_hash"]) == 64


def test_cli_rejects_bare_music_root_before_opening_master(capsys):
    exit_code = tiktok_music_page.main(
        [
            "inspect-local",
            "--url",
            "https://www.tiktok.com/music/",
            "--master-database",
            "missing.sqlite",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["reason"] == "exact_music_page_required"
    assert payload["ai_actions"] == []
    assert payload["outbound_actions"] == []

