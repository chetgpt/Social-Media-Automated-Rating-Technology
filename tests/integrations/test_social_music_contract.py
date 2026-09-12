import copy
import json

import pytest

from tiktok_scraper.social_music_contract import (
    SUPPORTED_PLATFORMS,
    SocialMusicContractError,
    build_capability_manifest,
    build_social_music_evidence,
    canonical_sha256,
    capability_manifest,
    social_music_readiness_reasons,
    validate_capability_manifest,
    validate_social_music_evidence,
    verify_social_music_evidence_hash,
)


OBSERVED_AT = "2026-08-20T12:34:56+07:00"


def provenance(source="platform_api.music", method="authenticated_api"):
    return {
        "source": source,
        "method": method,
        "observed_at": OBSERVED_AT,
        "request_url": "https://signed.example/api?token=SECRET",
        "authorization": "Bearer SECRET",
    }


def rebind(document):
    payload = copy.deepcopy(document)
    payload.pop("evidence_hash", None)
    payload["evidence_hash"] = canonical_sha256(payload)
    return payload


def test_capability_manifests_are_closed_hash_bound_and_platform_specific():
    expected = {
        "tiktok": (True, True, True),
        "instagram": (True, False, False),
        "facebook": (False, False, False),
        "x": (False, False, False),
        "youtube": (False, True, True),
    }

    assert set(expected) == SUPPORTED_PLATFORMS
    for platform, flags in expected.items():
        manifest = capability_manifest(platform)
        capabilities = manifest["capabilities"]
        assert (
            capabilities["platform_music_declaration"]["supported"],
            capabilities["transcript"]["supported"],
            capabilities["subtitles"]["supported"],
        ) == flags
        assert capabilities["acoustic_verification"] == {
            "supported": False,
            "forced_status": "not_attempted",
        }
        assert capabilities["lyrics"] == {
            "supported": False,
            "forced_status": "not_attempted",
        }
        assert build_capability_manifest(platform) == manifest
        assert validate_capability_manifest(manifest)


def test_tiktok_builds_separate_safe_music_transcript_and_subtitle_evidence():
    evidence = build_social_music_evidence(
        "tiktok",
        observed_at=OBSERVED_AT,
        music_declaration={
            "status": "available",
            "music_id": "sound-123",
            "title": "Declared Song",
            "artist": "Declared Artist",
            "album": "Declared Album",
            "is_original": False,
            "duration_seconds": 40.25,
            "provenance": provenance("tiktok_item.music", "tiktok_item_api"),
            "play_url": "https://signed.example/audio?token=SECRET",
            "raw_payload": {"token": "SECRET", "lyrics": "do not retain"},
            "lyrics": "do not retain",
        },
        transcript={
            "status": "available",
            "text": "Spoken words from the selected caption track.",
            "language": "en",
            "language_name": "English",
            "is_auto_generated": True,
            "segment_count": 4,
            "provenance": provenance(
                "tiktok_subtitle.selected_track",
                "tiktok_item_api",
            ),
            "raw_segments": [{"text": "must not survive"}],
        },
        subtitles={
            "status": "available",
            "tracks": [
                {
                    "language_code": "en",
                    "language_name": "English",
                    "is_auto_generated": True,
                    "segment_count": 4,
                    "download_url": "https://signed.example/subtitle?token=SECRET",
                    "raw_payload": {"secret": "SECRET"},
                }
            ],
            "selected_track": {
                "language_code": "en",
                "language_name": "English",
                "is_auto_generated": True,
                "segment_count": 4,
                "url": "https://signed.example/subtitle",
            },
            "provenance": provenance("tiktok_subtitle_list", "tiktok_item_api"),
        },
    )

    assert evidence["music_declaration"]["status"] == "available"
    assert evidence["music_declaration"]["fields"] == {
        "music_id": "sound-123",
        "title": "Declared Song",
        "artist": "Declared Artist",
        "album": "Declared Album",
        "audio_type": "",
        "is_original": False,
        "duration_ms": 40_250,
    }
    assert evidence["music_declaration"]["identification_basis"] == (
        "tiktok_declared_metadata"
    )
    assert evidence["transcript"]["status"] == "available"
    assert evidence["transcript"]["text"].startswith("Spoken words")
    assert evidence["subtitles"]["status"] == "available"
    assert evidence["subtitles"]["track_count"] == 1
    assert evidence["subtitles"]["selected_track"] == evidence["subtitles"]["tracks"][0]
    assert evidence["acoustic_verification"] == {
        "status": "not_attempted",
        "verified": False,
    }
    assert evidence["lyrics"] == {"status": "not_attempted"}
    assert verify_social_music_evidence_hash(evidence)
    assert validate_social_music_evidence(evidence)

    serialized = json.dumps(evidence)
    assert "signed.example" not in serialized
    assert "SECRET" not in serialized
    assert "raw_payload" not in serialized
    assert "raw_segments" not in serialized
    assert "download_url" not in serialized
    assert "do not retain" not in serialized


def test_music_values_without_explicit_source_and_method_are_discarded():
    evidence = build_social_music_evidence(
        "instagram",
        observed_at=OBSERVED_AT,
        music_declaration={
            "music_id": "ig-audio-1",
            "title": "Unattributed Song",
            "artist": "Unattributed Artist",
            "provenance": {"source": "instagram_media.music_info"},
        },
    )

    declaration = evidence["music_declaration"]
    assert declaration["status"] == "unavailable"
    assert declaration["reason"] == "explicit_music_provenance_required"
    assert declaration["fields"] == {
        "music_id": "",
        "title": "",
        "artist": "",
        "album": "",
        "audio_type": "",
        "is_original": None,
        "duration_ms": None,
    }
    assert validate_social_music_evidence(evidence)


def test_instagram_preserves_only_explicit_platform_declaration():
    evidence = build_social_music_evidence(
        "instagram",
        observed_at=OBSERVED_AT,
        music_declaration={
            "music_id": "ig-audio-1",
            "title": "Instagram Song",
            "artist": "Instagram Artist",
            "provenance": provenance(
                "instagram_media.music_info",
                "instagram_graphql_api",
            ),
        },
        transcript={
            "status": "available",
            "text": "This unsupported value must be discarded.",
            "provenance": provenance(),
        },
        subtitles={
            "tracks": [{"language_code": "en"}],
            "provenance": provenance(),
        },
    )

    assert evidence["music_declaration"]["status"] == "available"
    assert evidence["music_declaration"]["fields"]["title"] == "Instagram Song"
    assert evidence["transcript"]["status"] == "unsupported"
    assert evidence["transcript"]["text"] == ""
    assert evidence["subtitles"]["status"] == "unsupported"
    assert evidence["subtitles"]["tracks"] == []
    assert validate_social_music_evidence(evidence)


def test_youtube_keeps_transcript_and_subtitles_but_not_music_claims():
    evidence = build_social_music_evidence(
        "youtube",
        observed_at=OBSERVED_AT,
        music_declaration={
            "music_id": "fabricated",
            "title": "Do Not Keep",
            "artist": "Do Not Keep",
            "provenance": provenance(),
        },
        transcript={
            "status": "ok",
            "text": "A platform caption transcript.",
            "language": "en",
            "segment_count": 2,
            "provenance": provenance(
                "youtube_caption_track",
                "innertube_get_transcript",
            ),
        },
        subtitles={
            "tracks": [
                {
                    "language_code": "en",
                    "language_name": "English",
                    "is_auto_generated": False,
                    "segment_count": 2,
                }
            ],
            "provenance": provenance(
                "youtube_caption_tracks",
                "youtube_watch_player_api",
            ),
        },
    )

    assert evidence["music_declaration"]["status"] == "unsupported"
    assert evidence["music_declaration"]["fields"]["music_id"] == ""
    assert evidence["transcript"]["status"] == "available"
    assert evidence["subtitles"]["status"] == "available"
    assert evidence["lyrics"]["status"] == "not_attempted"
    assert validate_social_music_evidence(evidence)


@pytest.mark.parametrize("platform", ["facebook", "x", "twitter"])
def test_platforms_without_current_surfaces_record_explicit_terminal_outcomes(platform):
    evidence = build_social_music_evidence(platform, observed_at=OBSERVED_AT)

    assert evidence["platform"] == ("x" if platform == "twitter" else platform)
    assert evidence["music_declaration"]["status"] == "unsupported"
    assert evidence["transcript"]["status"] == "unsupported"
    assert evidence["subtitles"]["status"] == "unsupported"
    assert validate_social_music_evidence(evidence)


def test_transcript_and_subtitles_have_independent_terminal_outcomes():
    evidence = build_social_music_evidence(
        "tiktok",
        observed_at=OBSERVED_AT,
        transcript={
            "status": "unavailable",
            "reason": "caption_response_unavailable",
            "provenance": provenance("tiktok_subtitle", "tiktok_item_api"),
        },
        subtitles={
            "status": "not_provided",
            "reason": "no_subtitle_tracks",
            "provenance": provenance("tiktok_subtitle", "tiktok_item_api"),
        },
    )

    assert evidence["transcript"]["status"] == "unavailable"
    assert evidence["subtitles"]["status"] == "not_provided"
    assert evidence["transcript"]["reason"] != evidence["subtitles"]["reason"]
    assert validate_social_music_evidence(evidence)


def test_hash_and_closed_shape_detect_tampering_even_after_rehash():
    evidence = build_social_music_evidence("tiktok", observed_at=OBSERVED_AT)
    tampered = copy.deepcopy(evidence)
    tampered["lyrics"]["status"] = "available"

    assert not verify_social_music_evidence_hash(tampered)
    assert not validate_social_music_evidence(tampered)

    rehashed = rebind(tampered)
    assert verify_social_music_evidence_hash(rehashed)
    assert not validate_social_music_evidence(rehashed)

    extra_field = rebind({**evidence, "raw_payload": {"secret": "SECRET"}})
    assert not validate_social_music_evidence(extra_field)


def test_urls_are_removed_even_when_injected_into_allowlisted_text_fields():
    evidence = build_social_music_evidence(
        "tiktok",
        observed_at=OBSERVED_AT,
        music_declaration={
            "title": "Song https://signed.example/audio?token=SECRET",
            "artist": "Artist",
            "provenance": provenance(),
        },
        transcript={
            "text": "Visit https://secret.example/path?token=SECRET now",
            "provenance": provenance(),
        },
    )

    serialized = json.dumps(evidence)
    assert "https://" not in serialized
    assert "signed.example" not in serialized
    assert "secret.example" not in serialized
    assert "SECRET" not in serialized
    assert validate_social_music_evidence(evidence)


def test_unknown_platform_and_non_mapping_inputs_fail_closed():
    with pytest.raises(SocialMusicContractError):
        capability_manifest("threads")
    with pytest.raises(SocialMusicContractError):
        build_social_music_evidence("tiktok", music_declaration=["not", "a", "mapping"])


def test_capability_manifest_hash_binds_offline_adapter_version_and_authority():
    first = capability_manifest("youtube", adapter_version="collector-a-v1")
    second = capability_manifest("youtube", adapter_version="collector-b-v1")

    assert first["manifest_hash"] != second["manifest_hash"]
    assert first["adapter"] == {
        "name": "offline_json_ingest",
        "version": "collector-a-v1",
        "authority": "offline_normalized_import",
        "access_scope": "caller_supplied_normalized_records",
        "network_access": False,
        "source_modes": ["topic", "creator", "url"],
        "discovery_horizon": "input_payload_only",
    }
    assert validate_capability_manifest(first)
    assert validate_capability_manifest(second)


def test_supported_unattempted_stages_are_hash_valid_but_not_evidence_ready():
    evidence = build_social_music_evidence("tiktok", observed_at=OBSERVED_AT)

    assert validate_social_music_evidence(evidence)
    assert evidence["music_declaration"]["attempted"] is False
    assert evidence["transcript"]["attempted"] is False
    assert evidence["subtitles"]["attempted"] is False
    assert social_music_readiness_reasons(evidence) == [
        "platform_music_declaration_not_attempted",
        "transcript_not_attempted",
        "subtitles_not_attempted",
    ]


def test_explicit_terminal_attempts_with_provenance_are_evidence_ready():
    terminal = {
        "attempted": True,
        "status": "not_provided",
        "provenance": provenance("platform_response", "offline_collector"),
    }
    evidence = build_social_music_evidence(
        "tiktok",
        observed_at=OBSERVED_AT,
        music_declaration=terminal,
        transcript=terminal,
        subtitles=terminal,
        adapter_version="collector-a-v1",
    )

    assert validate_social_music_evidence(evidence)
    assert evidence["capability_manifest"]["adapter"]["version"] == "collector-a-v1"
    assert social_music_readiness_reasons(evidence) == []
