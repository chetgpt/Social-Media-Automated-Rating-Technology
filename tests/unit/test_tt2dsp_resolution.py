import json
from urllib.parse import parse_qs, urlparse

from tiktok_scraper.music_enrichment import HTTPResponse
from tiktok_scraper.tt2dsp_resolution import (
    TT2DSPResolutionConfig,
    TT2DSPResolver,
    apple_song_ids,
    validate_tt2dsp_resolution_document,
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


def links():
    return [
        {
            "meta_song_id": "6751368172487575554",
            "song_id": "1107248959",
            "platform": 1,
            "button_type": 2,
            "token": {"secret": "MUST_NOT_SURVIVE"},
        },
        {
            "meta_song_id": "6751368172487575554",
            "song_id": "5g1BARk25uUJtEPSUwcjnU",
            "platform": 3,
        },
    ]


def test_apple_id_resolves_to_closed_hash_bound_metadata():
    transport = FakeTransport(
        response(
            {
                "resultCount": 1,
                "results": [
                    {
                        "wrapperType": "track",
                        "kind": "song",
                        "trackId": 1107248959,
                        "trackName": "Hero (feat. Christina Perri)",
                        "artistName": "Cash Cash",
                        "collectionName": "Blood, Sweat & 3 Years",
                        "collectionId": 1107248683,
                        "trackTimeMillis": 197840,
                        "releaseDate": "2016-06-24T12:00:00Z",
                        "primaryGenreName": "Dance",
                        "trackExplicitness": "notExplicit",
                        "previewUrl": "https://audio.example/secret.m4a?token=x",
                        "artworkUrl100": "https://art.example/secret.jpg",
                        "trackViewUrl": "https://music.apple.com/id/song/1107248959",
                    }
                ],
            }
        )
    )
    result = TT2DSPResolver(transport=transport).resolve(links())

    assert result["status"] == "resolved"
    assert result["storefront"] == "ID"
    assert result["selected"] == {
        "provider_track_id": "1107248959",
        "title": "Hero (feat. Christina Perri)",
        "artist": "Cash Cash",
        "album": "Blood, Sweat & 3 Years",
        "collection_id": "1107248683",
        "duration_ms": 197840,
        "release_date": "2016-06-24T12:00:00Z",
        "genre": "Dance",
        "explicitness": "notExplicit",
    }
    assert validate_tt2dsp_resolution_document(result)
    serialized = json.dumps(result)
    assert "MUST_NOT_SURVIVE" not in serialized
    assert "previewUrl" not in serialized
    assert "artworkUrl" not in serialized
    assert "trackViewUrl" not in serialized
    query = parse_qs(urlparse(transport.calls[0]["url"]).query)
    assert query == {"id": ["1107248959"], "entity": ["song"], "country": ["ID"]}


def test_exact_returned_track_id_is_required():
    transport = FakeTransport(
        response(
            {
                "resultCount": 1,
                "results": [
                    {
                        "kind": "song",
                        "trackId": 999,
                        "trackName": "Wrong",
                        "artistName": "Wrong",
                    }
                ],
            }
        )
    )
    result = TT2DSPResolver(transport=transport).resolve(links())

    assert result["status"] == "not_found"
    assert result["decision"]["reason"] == "exact_track_id_not_returned"
    assert validate_tt2dsp_resolution_document(result)


def test_unknown_or_spotify_only_link_is_terminal_unsupported_without_request():
    transport = FakeTransport(error=AssertionError("transport must not be called"))
    result = TT2DSPResolver(transport=transport).resolve(
        [{"platform": "3", "song_id": "5g1BARk25uUJtEPSUwcjnU"}]
    )

    assert result["status"] == "unsupported"
    assert result["decision"]["reason"] == "no_supported_apple_link"
    assert transport.calls == []
    assert validate_tt2dsp_resolution_document(result)


def test_rate_limit_preserves_retry_after_and_hash():
    transport = FakeTransport(
        HTTPResponse(status_code=429, body=b"", headers={"Retry-After": "12"})
    )
    result = TT2DSPResolver(transport=transport).resolve(links())

    assert result["status"] == "rate_limited"
    assert result["error"] == {
        "code": "provider_rate_limited",
        "http_status": 429,
        "retry_after_seconds": 12.0,
    }
    assert validate_tt2dsp_resolution_document(result)


def test_transport_failure_is_unavailable_not_not_found():
    result = TT2DSPResolver(
        transport=FakeTransport(error=TimeoutError("offline"))
    ).resolve(links())

    assert result["status"] == "unavailable"
    assert result["decision"]["reason"] == "transport_error"
    assert validate_tt2dsp_resolution_document(result)


def test_hash_tamper_and_invalid_config_are_rejected():
    result = TT2DSPResolver(
        transport=FakeTransport(response({"resultCount": 0, "results": []}))
    ).resolve(links())
    result["storefront"] = "US"

    assert validate_tt2dsp_resolution_document(result) is False
    assert apple_song_ids(links()) == ["1107248959"]

    try:
        TT2DSPResolutionConfig(storefront="Indonesia")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid storefront must be rejected")
