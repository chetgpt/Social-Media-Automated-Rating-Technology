import json

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
    enrich_musicbrainz,
    is_generic_original_sound,
    _base_result,
    _candidate_projection,
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


@pytest.mark.parametrize("entrypoint", ["adapter", "facade"])
@pytest.mark.parametrize("audio", [
    platform_audio(),
    {"title": "Known"},
    {},
    {"title": "suara asli - maker", "author": "maker", "is_original": True},
])
def test_retired_entrypoints_never_consult_transport(config, entrypoint, audio):
    transport = FakeTransport(error=AssertionError("retired provider was contacted"))
    original = json.loads(json.dumps(audio))
    if entrypoint == "adapter":
        result = MusicBrainzAdapter(config, transport=transport).enrich(audio)
    else:
        result = enrich_musicbrainz(audio, config=config, transport=transport)
    assert transport.calls == []
    assert audio == original
    assert result["status"] == "unsupported"
    assert result["decision"]["reason"] == "provider_retired"
    assert result["error"]["code"] == "provider_retired"
    assert result["candidates"] == []
    assert result["selected_recording_mbid"] == ""
    assert result["provider_total_count"] is None
    assert result["provider_returned_count"] == 0
    assert result["platform_audio"] == normalize_platform_audio(audio)
    assert verify_canonical_hash(result)
    assert validate_enrichment_document(result)


@pytest.mark.parametrize("title,is_original", [
    ("original sound - chipotle", True),
    ("suara asli - Omg.Banget", True),
    ("localized native label unknown to this code", True),
    ("suara asli - Omg.Banget", None),
])
def test_historical_generic_original_sound_detection(title, is_original):
    assert is_generic_original_sound({"title": title, "is_original": is_original})


def historical_candidate_result(config, recordings):
    """Synthetic saved result, using only historical projection and scoring."""
    audio = normalize_platform_audio(platform_audio())
    candidates = [_candidate_projection(row, rank=rank)
                  for rank, row in enumerate(recordings, start=1)]
    for candidate in candidates:
        candidate["match"] = score_candidate(audio, candidate)
    ordered = sorted(candidates, key=lambda row: -row["match"]["score"])
    top = ordered[0]
    runner = ordered[1]["match"]["score"] if len(ordered) > 1 else None
    margin = top["match"]["score"] - runner if runner is not None else top["match"]["score"]
    matched = top["match"]["eligible"] and margin >= MATCH_POLICY["minimum_margin"]
    result = _base_result(audio, config)
    result.update(
        status="matched" if matched else "ambiguous",
        provider_total_count=len(candidates), provider_returned_count=len(candidates),
        retained_candidate_count=len(candidates), candidates=candidates,
        selected_candidate_rank=top["rank"] if matched else None,
        selected_recording_mbid=top["recording_mbid"] if matched else "",
        decision={"top_score": top["match"]["score"], "runner_up_score": runner,
                  "score_margin": margin, "leading_candidate_rank": top["rank"],
                  "reason": "matched_policy" if matched else "insufficient_score_margin"},
    )
    return bind_canonical_hash(result)


def test_historical_structured_candidates_and_match_validate_without_rewriting(config):
    result = historical_candidate_result(config, [
        recording("6f0d85f5-dbe0-4c53-820c-dc23402af982", isrcs=["USUG12601723"]),
        recording("unrelated", title="Different Song", artist="Different Artist",
                  length=180_000, score=91),
    ])
    original = canonical_json(result)
    assert result["status"] == "matched"
    candidate = result["candidates"][0]
    assert candidate["match"]["score"] == 80
    assert candidate["isrcs"] == ["USUG12601723"]
    assert candidate["duration_ms"] == 209_680
    assert candidate["provider_score"] == 100
    assert candidate["releases"][0]["release_mbid"].startswith("release-")
    assert candidate["variant_metadata"]["first_release_date"] == "2026-06-12"
    assert candidate["variant_metadata"]["aliases"][0]["name"] == "Stupid Song"
    assert "must-not-survive" not in original
    assert "signed.example" not in original
    assert validate_enrichment_document(result)
    assert verify_canonical_hash(result)
    assert canonical_json(result) == original
    tampered = json.loads(original)
    tampered["candidates"][0]["match"]["score"] = 99
    tampered = bind_canonical_hash(tampered)
    assert verify_canonical_hash(tampered)
    assert not validate_enrichment_document(tampered)


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


def test_historical_equally_plausible_variants_remain_ambiguous(config):
    result = historical_candidate_result(config, [
        recording("standard", disambiguation="standard mix"),
        recording("atmos", disambiguation="Dolby Atmos mix"),
    ])
    original = canonical_json(result)
    assert result["status"] == "ambiguous"
    assert result["selected_recording_mbid"] == ""
    assert result["decision"]["score_margin"] == 0
    assert [row["recording_mbid"] for row in result["candidates"]] == ["standard", "atmos"]
    assert validate_enrichment_document(result)
    assert canonical_json(result) == original


def test_historical_candidate_below_threshold_is_not_eligible():
    candidate = _candidate_projection(recording("candidate", length=None), rank=1)
    score = score_candidate(normalize_platform_audio(platform_audio()), candidate)
    assert score["score"] == 75
    assert score["eligible"] is False


@pytest.mark.parametrize("status", [
    "not_found", "unsupported", "unavailable", "rate_limited", "provider_error",
])
def test_historical_terminal_results_validate_without_rewriting(config, status):
    result = _base_result(normalize_platform_audio(platform_audio()), config)
    result["status"] = status
    result["decision"]["reason"] = "historical_fixture"
    result = bind_canonical_hash(result)
    original = canonical_json(result)
    assert validate_enrichment_document(result)
    assert verify_canonical_hash(result)
    assert canonical_json(result) == original


def test_historical_candidate_projection_rejects_malformed_recordings():
    assert _candidate_projection({"id": "missing-title"}, rank=1) is None


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
