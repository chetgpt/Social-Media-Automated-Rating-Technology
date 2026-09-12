from tiktok_scraper.raw_contract import finalize_content_record
from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper
from tiktok_scraper.social_music_contract import validate_social_music_evidence
from tiktok_scraper.social_music_projection import social_music_evidence_from_record


def test_instagram_declared_sound_keeps_explicit_nonofficial_provenance():
    evidence = social_music_evidence_from_record(
        {
            "platform": "instagram",
            "observed_at": "2026-08-20T12:00:00+00:00",
            "music_id": "audio-42",
            "music_title": "Declared Track",
            "music_author": "Declared Artist",
            "music_is_original": False,
            "music_duration_ms": 12_345,
            "music_metadata_status": "available",
            "music_metadata_source": "instagram_authenticated_web_metadata",
            "music_metadata_authority": "experimental_authenticated_web",
            "music_metadata_access_scope": "authenticated_web_api_not_official_graph_api",
            "metadata_method": "instagram_graphql_api",
            "cookie": "secret",
            "media_url": "https://cdn.example.test/audio?token=secret",
        }
    )

    assert validate_social_music_evidence(evidence)
    declaration = evidence["music_declaration"]
    assert declaration["status"] == "available"
    assert declaration["fields"]["title"] == "Declared Track"
    assert declaration["provenance"] == {
        "source": "instagram_authenticated_web_metadata",
        "method": "instagram_graphql_api",
        "authority": "experimental_authenticated_web",
        "access_scope": "authenticated_web_api_not_official_graph_api",
        "observed_at": "2026-08-20T12:00:00+00:00",
    }
    assert "secret" not in str(evidence)
    assert evidence["acoustic_verification"] == {
        "status": "not_attempted",
        "verified": False,
    }


def test_youtube_keeps_transcript_and_subtitle_outcomes_separate_from_music():
    evidence = social_music_evidence_from_record(
        {
            "platform": "youtube",
            "observed_at": "2026-08-20T12:00:00+00:00",
            "caption": "The title says a song name, but that is not a declaration",
            "transcript": "Spoken words from the selected caption track",
            "transcript_status": "ok",
            "transcript_source": "youtube_transcript_api",
            "transcript_authority": "youtube_caption_track",
            "transcript_access_scope": "authorized_caption_read",
            "transcript_language": "en",
            "transcript_language_name": "English",
            "transcript_is_auto_generated": True,
            "transcript_segment_count": 8,
        }
    )

    assert validate_social_music_evidence(evidence)
    assert evidence["music_declaration"]["status"] == "unsupported"
    assert evidence["music_declaration"]["fields"]["title"] == ""
    assert evidence["transcript"]["status"] == "available"
    assert evidence["transcript"]["text"].startswith("Spoken words")
    assert evidence["transcript"]["provenance"]["authority"] == (
        "youtube_caption_track"
    )
    assert evidence["transcript"]["provenance"]["access_scope"] == (
        "authorized_caption_read"
    )
    assert evidence["subtitles"]["status"] == "unavailable"
    assert evidence["subtitles"]["track_count"] == 0
    assert evidence["subtitles"]["selected_track"] is None
    assert evidence["subtitles"]["provenance"]["source"] == ""
    assert evidence["subtitles"]["provenance"]["method"] == ""


def test_explicit_subtitle_tracks_are_projected_without_transcript_inference():
    track = {
        "language": "id",
        "language_name": "Indonesian",
        "is_auto_generated": False,
        "segment_count": 3,
        "url": "https://signed.example.test/captions?token=secret",
    }
    evidence = social_music_evidence_from_record(
        {
            "platform": "youtube",
            "observed_at": "2026-08-20T12:00:00+00:00",
            "subtitle_status": "available",
            "subtitle_tracks": [track],
            "subtitle_selected_track": track,
            "subtitle_source": "youtube_caption_track_inventory",
            "subtitle_method": "authorized_captions_list",
            "subtitle_authority": "youtube_caption_manifest",
            "subtitle_access_scope": "authorized_caption_inventory",
        },
        adapter_version="youtube-normalizer-v2",
    )

    assert validate_social_music_evidence(evidence)
    assert evidence["transcript"]["status"] == "unavailable"
    assert evidence["subtitles"]["status"] == "available"
    assert evidence["subtitles"]["tracks"] == [
        {
            "language_code": "id",
            "language_name": "Indonesian",
            "is_auto_generated": False,
            "segment_count": 3,
        }
    ]
    assert evidence["subtitles"]["selected_track"] == evidence["subtitles"]["tracks"][0]
    assert evidence["subtitles"]["provenance"]["authority"] == (
        "youtube_caption_manifest"
    )
    assert evidence["subtitles"]["provenance"]["access_scope"] == (
        "authorized_caption_inventory"
    )
    assert evidence["capability_manifest"]["adapter"]["version"] == (
        "youtube-normalizer-v2"
    )
    assert "signed.example.test" not in str(evidence)


def test_explicit_subtitle_not_provided_is_not_derived_from_transcript_status():
    evidence = social_music_evidence_from_record(
        {
            "platform": "youtube",
            "observed_at": "2026-08-20T12:00:00+00:00",
            "transcript": "An explicitly attributed transcript",
            "transcript_status": "available",
            "transcript_source": "youtube_transcript_api",
            "subtitle_status": "not_provided",
            "subtitle_source": "youtube_caption_track_inventory",
            "subtitle_method": "authorized_captions_list",
            "subtitle_no_caption_reason": "caption_tracks_not_returned",
        }
    )

    assert validate_social_music_evidence(evidence)
    assert evidence["transcript"]["status"] == "available"
    assert evidence["subtitles"]["status"] == "not_provided"
    assert evidence["subtitles"]["reason"] == "caption_tracks_not_returned"
    assert evidence["subtitles"]["track_count"] == 0
    assert evidence["subtitles"]["provenance"]["source"] == (
        "youtube_caption_track_inventory"
    )


def test_structured_secret_values_are_discarded_before_scalar_projection():
    evidence = social_music_evidence_from_record(
        {
            "platform": "tiktok",
            "observed_at": "2026-08-20T12:00:00+00:00",
            "music_title": {"authorization": "Bearer MUSIC_SECRET"},
            "music_author": ["MUSIC_SECRET"],
            "music_id": b"MUSIC_SECRET",
            "music_album": ("MUSIC_SECRET",),
            "music_audio_type": bytearray(b"MUSIC_SECRET"),
            "music_metadata_source": {"token": "MUSIC_SECRET"},
            "music_metadata_authority": ["MUSIC_SECRET"],
            "music_metadata_access_scope": memoryview(b"MUSIC_SECRET"),
            "metadata_method": ["MUSIC_SECRET"],
            "transcript": {"authorization": "Bearer TRANSCRIPT_SECRET"},
            "transcript_status": "ok",
            "transcript_source": {"token": "TRANSCRIPT_SECRET"},
            "transcript_language": ("TRANSCRIPT_SECRET",),
            "transcript_language_name": bytearray(b"TRANSCRIPT_SECRET"),
            "transcript_is_auto_generated": {"secret": "TRANSCRIPT_SECRET"},
            "transcript_segment_count": ["TRANSCRIPT_SECRET"],
        }
    )

    assert validate_social_music_evidence(evidence)
    assert evidence["music_declaration"]["status"] == "unavailable"
    assert all(
        value in ("", None)
        for value in evidence["music_declaration"]["fields"].values()
    )
    assert evidence["transcript"]["status"] == "unavailable"
    assert evidence["transcript"]["text"] == ""
    assert evidence["transcript"]["language"] == ""
    serialized = str(evidence)
    assert "MUSIC_SECRET" not in serialized
    assert "TRANSCRIPT_SECRET" not in serialized


def test_platforms_without_music_read_surface_never_infer_from_post_text():
    for platform in ("facebook", "x"):
        evidence = social_music_evidence_from_record(
            {
                "platform": platform,
                "caption": "Listening to Definitely A Song by Somebody",
                "description": "#music",
                "observed_at": "2026-08-20T12:00:00+00:00",
            }
        )

        assert validate_social_music_evidence(evidence)
        assert evidence["music_declaration"]["status"] == "unsupported"
        assert all(
            value in ("", None)
            for value in evidence["music_declaration"]["fields"].values()
        )
        assert evidence["transcript"]["status"] == "unsupported"
        assert evidence["subtitles"]["status"] == "unsupported"


def test_raw_contract_attaches_hash_bound_social_music_evidence_idempotently():
    record = {
        "platform": "instagram",
        "video_id": "ABC123",
        "url": "https://www.instagram.com/reel/ABC123/",
        "caption": "Post caption",
        "content_creator": "creator",
        "music_id": "m1",
        "music_title": "Track",
        "music_author": "Artist",
        "music_metadata_status": "available",
        "music_metadata_source": "instagram_authenticated_web_metadata",
        "metadata_method": "instagram_graphql_api",
        "comments": [],
    }
    first = finalize_content_record(
        record,
        observed_at="2026-08-20T12:00:00+00:00",
    )
    second = finalize_content_record(
        first,
        observed_at="2026-08-20T12:00:00+00:00",
    )

    assert validate_social_music_evidence(first["social_music_evidence"])
    assert first["brief_evidence"]["social_music"] == first["social_music_evidence"]
    assert second["social_music_evidence"] == first["social_music_evidence"]


def test_instagram_extractor_labels_music_source_and_terminal_status():
    metrics = InstagramScraper()._media_metrics(
        {
            "video_duration": 15.0,
            "media_audio_type": "MUSIC",
            "clips_metadata": {
                "music_info": {
                    "music_asset_info": {
                        "audio_id": "ig-audio",
                        "title": "IG Track",
                        "display_artist": "IG Artist",
                        "audio_duration_in_ms": 14_500,
                    }
                }
            },
        }
    )

    assert metrics["music_metadata_status"] == "available"
    assert metrics["music_metadata_attempted"] is True
    assert metrics["music_metadata_source"] == "instagram_authenticated_web_metadata"
    assert metrics["music_metadata_authority"] == "experimental_authenticated_web"
    assert metrics["music_metadata_access_scope"] == (
        "authenticated_web_api_not_official_graph_api"
    )
    assert metrics["music_is_original"] is False
    assert metrics["music_duration_ms"] == 14_500
    assert metrics["music_audio_type"] == "MUSIC"


def test_instagram_coarse_official_audio_type_is_partial_not_track_identity():
    evidence = social_music_evidence_from_record(
        {
            "platform": "instagram",
            "observed_at": "2026-08-20T12:00:00+00:00",
            "media_audio_type": "MUSIC",
            "music_metadata_status": "partial",
            "music_metadata_source": "instagram_graph_api_media",
            "metadata_method": "instagram_graph_api",
        }
    )

    assert validate_social_music_evidence(evidence)
    declaration = evidence["music_declaration"]
    assert declaration["status"] == "partial"
    assert declaration["fields"]["audio_type"] == "MUSIC"
    assert declaration["fields"]["title"] == ""
    assert declaration["fields"]["artist"] == ""
    assert declaration["reason"] == "platform_music_identity_incomplete"
