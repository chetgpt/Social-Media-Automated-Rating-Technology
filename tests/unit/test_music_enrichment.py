import json
from urllib.parse import parse_qs, urlparse

import pytest

from tiktok_scraper.music_enrichment import (
    HTTPResponse,
    MATCH_POLICY,
    MusicBrainzAdapter,
    MusicBrainzConfig,
    MusicEnrichmentError,
    bind_canonical_hash,
    canonical_json,
    canonical_sha256,
    normalize_platform_audio,
    score_candidate,
    validate_enrichment_document,
    verify_canonical_hash,
)


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.response


def response(payload, status=200, headers=None):
    return HTTPResponse(
        status_code=status,
        body=json.dumps(payload).encode("utf-8"),
        headers=headers or {},
    )


@pytest.fixture
def config():
    return MusicBrainzConfig(
        application_name="TikTokEngage",
        application_version="1.0",
        contact="ops@example.test",
        candidate_limit=5,
        timeout_seconds=7.5,
    )


def recording(
    mbid,
    *,
    title="stupid song",
    artist="Olivia Rodrigo",
    length=209_680,
    isrcs=None,
    score=100,
    disambiguation="",
):
    return {
        "id": mbid,
        "title": title,
        "score": score,
        "length": length,
        "isrcs": isrcs or [],
        "disambiguation": disambiguation,
        "first-release-date": "2026-06-12",
        "video": False,
        "artist-credit": [
            {
                "name": artist,
                "artist": {
                    "id": f"artist-{mbid}",
                    "name": artist,
                    "sort-name": "Rodrigo, Olivia",
                },
                "joinphrase": "",
            }
        ],
        "releases": [
            {
                "id": f"release-{mbid}",
                "title": "You Seem Pretty Sad for a Girl So in Love",
                "status": "Official",
                "date": "2026-06-12",
                "country": "US",
                "track-count": 12,
                "release-group": {
                    "id": f"group-{mbid}",
                    "title": "You Seem Pretty Sad for a Girl So in Love",
                    "primary-type": "Album",
                    "secondary-types": [],
                },
                "ignored_secret": "must-not-survive",
            }
        ],
        "aliases": [
            {
                "name": "Stupid Song",
                "sort-name": "Stupid Song",
                "locale": "en",
                "primary": True,
            }
        ],
        "lyrics": "must-not-survive",
        "media_url": "https://signed.example/audio?token=secret",
    }


def platform_audio(**overrides):
    value = {
        "music_id": "7666125413885725470",
        "title": "Stupid Song",
        "author": "Olivia Rodrigo",
        "original": False,
        "duration": 210,
    }
    value.update(overrides)
    return value


def test_normalize_platform_audio_is_a_safe_projection():
    normalized = normalize_platform_audio(
        {
            "musicId": 123,
            "musicTitle": " Track ",
            "authorName": " Artist ",
            "isOriginal": "false",
            "duration_seconds": "12.345",
            "isrc": "US-UG1-26-01723",
            "cookie": "secret",
            "authorization": "Bearer secret",
            "media_url": "https://example.test/signed",
            "lyrics": "not allowed",
        }
    )

    assert normalized == {
        "music_id": "123",
        "title": "Track",
        "author": "Artist",
        "is_original": False,
        "duration_ms": 12_345,
        "isrc": "USUG12601723",
    }
    assert "secret" not in canonical_json(normalized)


@pytest.mark.parametrize(
    "bad_contact",
    ["", "contact-without-address", "ops@example.test\nInjected: yes"],
)
def test_config_requires_a_proper_contact(bad_contact):
    with pytest.raises(MusicEnrichmentError):
        MusicBrainzConfig("App", "1.0", bad_contact)


def test_config_builds_identifying_user_agent(config):
    assert config.user_agent == "TikTokEngage/1.0 ( ops@example.test )"


@pytest.mark.parametrize(
    ("title", "is_original"),
    [
        ("original sound - chipotle", True),
        ("suara asli - Omg.Banget", True),
        ("localized native label unknown to this code", True),
        ("suara asli - Omg.Banget", None),
    ],
)
def test_generic_original_sound_is_unsupported_without_calling_provider(
    config, title, is_original
):
    transport = FakeTransport(error=AssertionError("transport must not be called"))
    result = MusicBrainzAdapter(config, transport=transport).enrich(
        {
            "music_id": "1",
            "title": title,
            "author": "Chipotle",
            "is_original": is_original,
            "duration": 12,
        }
    )

    assert result["status"] == "unsupported"
    assert result["decision"]["reason"] == "generic_original_sound"
    assert transport.calls == []
    assert verify_canonical_hash(result)


def test_missing_title_or_artist_is_unsupported(config):
    result = MusicBrainzAdapter(
        config, transport=FakeTransport(error=AssertionError("no request"))
    ).enrich({"title": "Known", "duration": 20})

    assert result["status"] == "unsupported"
    assert result["decision"]["missing_fields"] == ["author"]


def test_adapter_retains_structured_candidates_and_makes_deterministic_match(config):
    payload = {
        "count": 2,
        "recordings": [
            recording(
                "6f0d85f5-dbe0-4c53-820c-dc23402af982",
                isrcs=["USUG12601723"],
            ),
            recording(
                "unrelated",
                title="Different Song",
                artist="Different Artist",
                length=180_000,
                score=91,
            ),
        ],
    }
    transport = FakeTransport(response(payload))
    result = MusicBrainzAdapter(config, transport=transport).enrich(platform_audio())

    assert result["status"] == "matched"
    assert result["selected_recording_mbid"] == (
        "6f0d85f5-dbe0-4c53-820c-dc23402af982"
    )
    assert result["decision"] == {
        "top_score": 80,
        "runner_up_score": 0,
        "score_margin": 80,
        "reason": "matched_policy",
        "leading_candidate_rank": 1,
    }
    candidate = result["candidates"][0]
    assert candidate["recording_mbid"] == "6f0d85f5-dbe0-4c53-820c-dc23402af982"
    assert candidate["isrcs"] == ["USUG12601723"]
    assert candidate["duration_ms"] == 209_680
    assert candidate["provider_score"] == 100
    assert candidate["releases"][0]["release_mbid"].startswith("release-")
    assert candidate["variant_metadata"]["first_release_date"] == "2026-06-12"
    assert candidate["variant_metadata"]["aliases"][0]["name"] == "Stupid Song"
    serialized = canonical_json(result)
    assert "must-not-survive" not in serialized
    assert "signed.example" not in serialized
    assert verify_canonical_hash(result)
    assert validate_enrichment_document(result)

    tampered = json.loads(json.dumps(result))
    tampered["candidates"][0]["match"]["score"] = 99
    tampered = bind_canonical_hash(tampered)
    assert verify_canonical_hash(tampered)
    assert not validate_enrichment_document(tampered)

    call = transport.calls[0]
    assert call["headers"] == {
        "Accept": "application/json",
        "User-Agent": "TikTokEngage/1.0 ( ops@example.test )",
    }
    assert call["timeout"] == 7.5
    query = parse_qs(urlparse(call["url"]).query)
    assert query["limit"] == ["5"]
    assert query["fmt"] == ["json"]
    assert 'recording:"Stupid Song"' in query["query"][0]
    assert 'artist:"Olivia Rodrigo"' in query["query"][0]


def test_optional_isrc_contributes_twenty_points(config):
    candidate = {
        "title": "Stupid Song",
        "artist_credit": "Olivia Rodrigo",
        "artists": [{"name": "Olivia Rodrigo"}],
        "isrcs": ["USUG12601723"],
        "duration_ms": 209_680,
    }
    score = score_candidate(
        normalize_platform_audio(platform_audio(isrc="USUG12601723")), candidate
    )

    assert score["score"] == 100
    assert score["points"] == {
        "title": 40,
        "artist": 35,
        "isrc": 20,
        "duration": 5,
    }
    assert score["eligible"] is True


def test_title_and_artist_are_required_even_if_isrc_matches():
    candidate = {
        "title": "Other",
        "artist_credit": "Other",
        "artists": [{"name": "Other"}],
        "isrcs": ["USUG12601723"],
        "duration_ms": 210_000,
    }
    score = score_candidate(
        normalize_platform_audio(platform_audio(isrc="USUG12601723")), candidate
    )

    assert score["score"] == 25
    assert score["eligible"] is False


def test_two_equally_plausible_recording_variants_are_ambiguous(config):
    transport = FakeTransport(
        response(
            {
                "count": 2,
                "recordings": [
                    recording("standard", disambiguation="standard mix"),
                    recording("atmos", disambiguation="Dolby Atmos mix"),
                ],
            }
        )
    )
    result = MusicBrainzAdapter(config, transport=transport).enrich(platform_audio())

    assert result["status"] == "ambiguous"
    assert result["selected_recording_mbid"] == ""
    assert result["decision"]["reason"] == "insufficient_score_margin"
    assert result["decision"]["score_margin"] == 0
    assert [item["recording_mbid"] for item in result["candidates"]] == [
        "standard",
        "atmos",
    ]


def test_candidate_below_threshold_is_ambiguous_not_not_found(config):
    transport = FakeTransport(
        response(
            {
                "count": 1,
                "recordings": [recording("candidate", length=None)],
            }
        )
    )
    result = MusicBrainzAdapter(config, transport=transport).enrich(platform_audio())

    assert result["status"] == "ambiguous"
    assert result["decision"]["top_score"] == 75
    assert result["decision"]["reason"] == "no_candidate_met_match_policy"


def test_empty_successful_search_is_not_found(config):
    result = MusicBrainzAdapter(
        config,
        transport=FakeTransport(response({"count": 0, "recordings": []})),
    ).enrich(platform_audio())

    assert result["status"] == "not_found"
    assert result["provider_total_count"] == 0
    assert result["error"] is None


def test_malformed_recordings_are_provider_error_not_no_match(config):
    result = MusicBrainzAdapter(
        config,
        transport=FakeTransport(
            response({"count": 1, "recordings": [{"id": "missing-title"}]})
        ),
    ).enrich(platform_audio())

    assert result["status"] == "provider_error"
    assert result["error"]["code"] == "invalid_candidate_schema"
    assert result["invalid_candidate_count"] == 1


def test_adapter_bounds_candidates_even_if_transport_returns_more(config):
    recordings = [
        recording(f"candidate-{number}", title=f"Track {number}")
        for number in range(8)
    ]
    result = MusicBrainzAdapter(
        config,
        transport=FakeTransport(response({"count": 30, "recordings": recordings})),
    ).enrich(platform_audio())

    assert result["provider_returned_count"] == 8
    assert result["retained_candidate_count"] == 5
    assert result["provider_results_truncated"] is True
    assert len(result["candidates"]) == config.candidate_limit


def test_rate_limit_is_not_converted_to_no_match(config):
    result = MusicBrainzAdapter(
        config,
        transport=FakeTransport(
            HTTPResponse(429, b"slow down", {"Retry-After": "3"})
        ),
    ).enrich(platform_audio())

    assert result["status"] == "rate_limited"
    assert result["error"]["retry_after_seconds"] == 3
    assert result["status"] != "not_found"


@pytest.mark.parametrize("status", [400, 500, 503])
def test_provider_http_errors_are_not_converted_to_no_match(config, status):
    result = MusicBrainzAdapter(
        config, transport=FakeTransport(HTTPResponse(status, b"error"))
    ).enrich(platform_audio())

    assert result["status"] == "provider_error"
    assert result["error"]["http_status"] == status


@pytest.mark.parametrize(
    ("body", "code"),
    [(b"not-json", "invalid_json"), (b'{"recordings": {}}', "invalid_schema")],
)
def test_invalid_provider_payload_is_provider_error(config, body, code):
    result = MusicBrainzAdapter(
        config, transport=FakeTransport(HTTPResponse(200, body))
    ).enrich(platform_audio())

    assert result["status"] == "provider_error"
    assert result["error"]["code"] == code


@pytest.mark.parametrize("exception", [TimeoutError(), OSError("offline")])
def test_transport_failures_are_unavailable_not_no_match(config, exception):
    result = MusicBrainzAdapter(
        config, transport=FakeTransport(error=exception)
    ).enrich(platform_audio())

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "transport_unavailable"
    assert result["status"] != "not_found"


def test_canonical_json_and_hash_are_stable_and_tamper_evident():
    left = {"z": "café", "a": {"two": 2, "one": 1}}
    right = {"a": {"one": 1, "two": 2}, "z": "café"}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_sha256(left) == canonical_sha256(right)
    bound = bind_canonical_hash(left)
    assert verify_canonical_hash(bound)
    bound["a"]["one"] = 9
    assert verify_canonical_hash(bound) is False


def test_canonical_json_rejects_non_finite_numbers():
    with pytest.raises(MusicEnrichmentError):
        canonical_json({"score": float("nan")})


def test_match_policy_is_the_required_40_35_20_5_gate():
    assert MATCH_POLICY["title_points"] == 40
    assert MATCH_POLICY["artist_points"] == 35
    assert MATCH_POLICY["isrc_points"] == 20
    assert MATCH_POLICY["duration_points"] == 5
    assert MATCH_POLICY["minimum_score"] == 80
    assert MATCH_POLICY["minimum_margin"] == 15
    assert MATCH_POLICY["requires_title_match"] is True
    assert MATCH_POLICY["requires_artist_match"] is True
