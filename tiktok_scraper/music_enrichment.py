"""Historical MusicBrainz evidence compatibility and local retirement outcomes.

The collector-facing boundary in this module is intentionally small.  It
accepts only platform-declared audio identifiers and descriptors and returns
a terminal retirement result without provider access. Pure projection and
validation helpers preserve the historical schema. This module never handles
media, cookies, access tokens, or lyrics.

MusicBrainz search scores are retained as provider evidence but are not used
as the identity decision.  Identity is decided by the deterministic matcher
documented in :data:`MATCH_POLICY`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Protocol, Sequence
import unicodedata
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


SCHEMA_VERSION = "musicbrainz-enrichment-v1"
PROVIDER = "musicbrainz"
VALID_STATUSES = frozenset(
    {
        "matched",
        "ambiguous",
        "not_found",
        "unsupported",
        "unavailable",
        "rate_limited",
        "provider_error",
    }
)

MATCH_POLICY: dict[str, Any] = {
    "name": "title-artist-isrc-duration-v1",
    "title_points": 40,
    "artist_points": 35,
    "isrc_points": 20,
    "duration_points": 5,
    "requires_title_match": True,
    "requires_artist_match": True,
    "minimum_score": 80,
    "minimum_margin": 15,
    "duration_absolute_tolerance_ms": 2_000,
    "duration_relative_tolerance": 0.02,
}

_PRODUCT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_GENERIC_ORIGINAL_SOUND_RE = re.compile(
    r"^(?:original\s+sound|suara\s+asli)(?:\s*[-\u2013\u2014:]\s*.*)?$",
    re.IGNORECASE,
)


class MusicEnrichmentError(ValueError):
    """Raised for invalid local configuration or non-JSON-safe documents."""


class ProviderPayloadError(RuntimeError):
    """Raised when the provider response cannot be safely decoded."""


@dataclass(frozen=True)
class HTTPResponse:
    """Minimal transport-neutral HTTP response used by the adapter."""

    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HTTPTransport(Protocol):
    """Injectable HTTP boundary.  Tests can implement this without network I/O."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse:
        """Return an HTTP response or raise an OS/timeout error."""


@dataclass(frozen=True)
class MusicBrainzConfig:
    """MusicBrainz client identity and bounded request configuration."""

    application_name: str
    application_version: str
    contact: str
    candidate_limit: int = 5
    timeout_seconds: float = 15.0
    endpoint: str = "https://musicbrainz.org/ws/2/recording/"
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not _PRODUCT_TOKEN_RE.fullmatch(self.application_name):
            raise MusicEnrichmentError(
                "application_name must be a non-empty HTTP product token"
            )
        if not _PRODUCT_TOKEN_RE.fullmatch(self.application_version):
            raise MusicEnrichmentError(
                "application_version must be a non-empty HTTP product version"
            )
        contact = self.contact.strip()
        if not contact or any(character in contact for character in "\r\n"):
            raise MusicEnrichmentError("contact must be non-empty and single-line")
        if "@" not in contact and not contact.startswith(("https://", "http://")):
            raise MusicEnrichmentError("contact must be an email address or HTTP(S) URL")
        if not 1 <= self.candidate_limit <= 25:
            raise MusicEnrichmentError("candidate_limit must be between 1 and 25")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise MusicEnrichmentError("timeout_seconds must be positive and finite")
        if not self.endpoint.startswith("https://"):
            raise MusicEnrichmentError("MusicBrainz endpoint must use HTTPS")
        if self.max_response_bytes < 1_024:
            raise MusicEnrichmentError("max_response_bytes must be at least 1024")

    @property
    def user_agent(self) -> str:
        """Return the MusicBrainz-required identifying User-Agent value."""

        return (
            f"{self.application_name}/{self.application_version} "
            f"( {self.contact.strip()} )"
        )


class UrllibHTTPTransport:
    """Small standard-library transport suitable for production injection."""

    def __init__(self, *, max_response_bytes: int = 2_000_000) -> None:
        if max_response_bytes < 1_024:
            raise MusicEnrichmentError("max_response_bytes must be at least 1024")
        self.max_response_bytes = max_response_bytes

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse:
        request = urllib_request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib_request.urlopen(request, timeout=timeout) as response:
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise ProviderPayloadError("provider response exceeded size limit")
                return HTTPResponse(
                    status_code=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except urllib_error.HTTPError as exc:
            body = exc.read(self.max_response_bytes + 1)
            if len(body) > self.max_response_bytes:
                body = b""
            return HTTPResponse(
                status_code=int(exc.code),
                body=body,
                headers=dict(exc.headers.items()) if exc.headers else {},
            )


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _first_present(source: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    normalized = _text(value).casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _positive_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _normalize_duration_ms(source: Mapping[str, Any]) -> int | None:
    milliseconds = _positive_number(source.get("duration_ms"))
    if milliseconds is not None:
        return int(round(milliseconds))
    seconds = _positive_number(
        _first_present(source, ("duration_seconds", "duration"))
    )
    return int(round(seconds * 1_000)) if seconds is not None else None


def _normalize_isrc(value: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", _text(value)).upper()
    return normalized if _ISRC_RE.fullmatch(normalized) else ""


def normalize_platform_audio(audio: Mapping[str, Any]) -> dict[str, Any]:
    """Project untrusted platform metadata into the safe enrichment boundary.

    Unknown input keys are deliberately discarded.  Duration is canonicalized
    to milliseconds.  ``isrc`` is optional because TikTok normally does not
    expose one, but accepting a valid ISRC lets an upstream licensed source
    improve identity confidence without changing the matcher.
    """

    if not isinstance(audio, Mapping):
        raise MusicEnrichmentError("platform audio must be a mapping")
    return {
        "music_id": _text(
            _first_present(audio, ("music_id", "id", "musicId"))
        ),
        "title": _text(
            _first_present(audio, ("title", "music_title", "musicTitle"))
        ),
        "author": _text(
            _first_present(
                audio,
                ("author", "artist", "author_name", "authorName"),
            )
        ),
        "is_original": _optional_bool(
            _first_present(
                audio,
                ("is_original", "original", "original_sound", "isOriginal"),
            )
        ),
        "duration_ms": _normalize_duration_ms(audio),
        "isrc": _normalize_isrc(audio.get("isrc")),
    }


def _json_default_reject(value: Any) -> Any:
    raise MusicEnrichmentError(
        f"value of type {type(value).__name__} is not canonical-JSON compatible"
    )


def canonical_json(document: Any) -> str:
    """Serialize a document using the workspace's stable JSON representation."""

    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default_reject,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, MusicEnrichmentError):
            raise
        raise MusicEnrichmentError("document is not canonical-JSON compatible") from exc


def canonical_sha256(document: Any) -> str:
    """Return the SHA-256 digest of :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def bind_canonical_hash(
    document: Mapping[str, Any],
    *,
    field_name: str = "enrichment_hash",
) -> dict[str, Any]:
    """Return a copy with a hash bound to every field except the hash itself."""

    if not isinstance(document, Mapping):
        raise MusicEnrichmentError("hashed document must be a mapping")
    payload = dict(document)
    payload.pop(field_name, None)
    result = dict(payload)
    result[field_name] = canonical_sha256(payload)
    return result


def verify_canonical_hash(
    document: Mapping[str, Any],
    *,
    field_name: str = "enrichment_hash",
) -> bool:
    """Verify a hash created by :func:`bind_canonical_hash`."""

    expected = _text(document.get(field_name))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    payload = dict(document)
    payload.pop(field_name, None)
    return canonical_sha256(payload) == expected


def validate_enrichment_document(document: Mapping[str, Any]) -> bool:
    """Validate the closed, hash-bound provider result stored in evidence."""

    if not isinstance(document, Mapping) or not verify_canonical_hash(document):
        return False
    top_level = {
        "schema_version", "provider", "status", "platform_audio", "query",
        "match_policy", "provider_total_count", "provider_returned_count",
        "retained_candidate_count", "invalid_candidate_count",
        "provider_results_truncated", "candidates", "selected_candidate_rank",
        "selected_recording_mbid", "decision", "error", "enrichment_hash",
    }
    if set(document) != top_level:
        return False
    if (
        _text(document.get("schema_version")) != SCHEMA_VERSION
        or _text(document.get("provider")) != PROVIDER
        or _text(document.get("status")) not in VALID_STATUSES
    ):
        return False

    def allowed_mapping(value: Any, keys: set[str]) -> bool:
        return isinstance(value, Mapping) and set(value).issubset(keys)

    audio = document.get("platform_audio")
    if not allowed_mapping(
        audio,
        {"music_id", "title", "author", "is_original", "duration_ms", "isrc"},
    ) or set(audio) != {
        "music_id", "title", "author", "is_original", "duration_ms", "isrc"
    }:
        return False
    if audio != normalize_platform_audio(audio):
        return False
    if not allowed_mapping(document.get("query"), {"type", "candidate_limit"}):
        return False
    if not allowed_mapping(document.get("match_policy"), set(MATCH_POLICY)):
        return False
    if not allowed_mapping(
        document.get("decision"),
        {
            "top_score", "runner_up_score", "score_margin", "reason",
            "leading_candidate_rank", "missing_fields",
        },
    ):
        return False
    error = document.get("error")
    if error is not None and not allowed_mapping(
        error,
        {"code", "message", "retry_after_seconds", "http_status"},
    ):
        return False
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > 25:
        return False
    candidate_keys = {
        "rank", "recording_mbid", "title", "artist_credit", "artists",
        "isrcs", "duration_ms", "releases", "provider_score",
        "variant_metadata", "match",
    }
    observed_ranks: set[int] = set()
    for candidate in candidates:
        if not allowed_mapping(candidate, candidate_keys) or set(candidate) != candidate_keys:
            return False
        rank = _safe_int(candidate.get("rank"), minimum=1)
        if rank is None or rank in observed_ranks:
            return False
        observed_ranks.add(rank)
        if candidate.get("provider_score") is not None and _safe_number(
            candidate.get("provider_score")
        ) is None:
            return False
        if not isinstance(candidate.get("artists"), list) or not all(
            allowed_mapping(
                artist,
                {"name", "artist_mbid", "sort_name", "disambiguation", "join_phrase"},
            )
            for artist in candidate["artists"]
        ):
            return False
        if not isinstance(candidate.get("isrcs"), list) or any(
            not _normalize_isrc(value) for value in candidate["isrcs"]
        ):
            return False
        if not isinstance(candidate.get("releases"), list):
            return False
        for release in candidate["releases"]:
            if not allowed_mapping(
                release,
                {"release_mbid", "title", "status", "date", "country", "track_count", "release_group"},
            ) or not allowed_mapping(
                release.get("release_group"),
                {"release_group_mbid", "title", "primary_type", "secondary_types"},
            ):
                return False
        variant = candidate.get("variant_metadata")
        if not allowed_mapping(
            variant,
            {"disambiguation", "first_release_date", "is_video", "aliases"},
        ) or not isinstance(variant.get("aliases"), list):
            return False
        if not all(
            allowed_mapping(
                alias,
                {"name", "sort_name", "locale", "type", "primary", "begin_date", "end_date"},
            )
            for alias in variant["aliases"]
        ):
            return False
        match = candidate.get("match")
        if not allowed_mapping(
            match,
            {
                "score", "eligible", "title_match", "artist_match",
                "isrc_match", "duration_comparable", "points",
            },
        ) or not allowed_mapping(
            match.get("points"), {"title", "artist", "isrc", "duration"}
        ):
            return False
        if match != score_candidate(audio, candidate):
            return False
    retained = _safe_int(document.get("retained_candidate_count"))
    if retained != len(candidates):
        return False
    status = _text(document.get("status"))
    selected_rank = _safe_int(document.get("selected_candidate_rank"), minimum=1)
    selected_mbid = _text(document.get("selected_recording_mbid"))
    if status == "matched":
        selected = next(
            (item for item in candidates if item.get("rank") == selected_rank),
            None,
        )
        if (
            selected is None
            or selected_mbid != _text(selected.get("recording_mbid"))
            or selected.get("match", {}).get("eligible") is not True
        ):
            return False
    elif selected_rank is not None or selected_mbid:
        return False
    if status == "not_found" and candidates:
        return False
    if candidates:
        if document.get("match_policy") != MATCH_POLICY:
            return False
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                -candidate["match"]["score"],
                -(
                    candidate["provider_score"]
                    if candidate["provider_score"] is not None
                    else -1
                ),
                candidate["rank"],
                candidate["recording_mbid"],
            ),
        )
        top = ordered[0]
        runner_score = (
            ordered[1]["match"]["score"] if len(ordered) > 1 else None
        )
        margin = (
            top["match"]["score"] - runner_score
            if runner_score is not None
            else top["match"]["score"]
        )
        expected_status = (
            "matched"
            if top["match"]["eligible"]
            and margin >= MATCH_POLICY["minimum_margin"]
            else "ambiguous"
        )
        decision = document.get("decision")
        if (
            status != expected_status
            or decision.get("top_score") != top["match"]["score"]
            or decision.get("runner_up_score") != runner_score
            or decision.get("score_margin") != margin
            or decision.get("leading_candidate_rank") != top["rank"]
        ):
            return False
    elif status in {"matched", "ambiguous"}:
        return False
    return True


def _match_text(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", _text(value)).casefold()
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    return " ".join(re.findall(r"[\w]+", without_marks, flags=re.UNICODE))


def is_generic_original_sound(audio: Mapping[str, Any]) -> bool:
    """Return whether TikTok's title is a generic original-sound label.

    TikTok localizes this label, so the structured original-sound flag is the
    primary language-independent signal.  The bounded title pattern is only a
    fallback when the flag is absent or false.  Neither form is a recording
    identity and neither should be sent to MusicBrainz as a song title.
    """

    if audio.get("is_original") is True:
        return True
    title = _text(audio.get("title"))
    return bool(_GENERIC_ORIGINAL_SOUND_RE.fullmatch(title))


def _safe_int(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result < minimum or (maximum is not None and result > maximum):
        return None
    return result


def _safe_number(value: Any, *, minimum: float = 0.0, maximum: float = 100.0) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < minimum or result > maximum:
        return None
    return int(result) if result.is_integer() else result


def _artist_projection(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    artist = value.get("artist") if isinstance(value.get("artist"), Mapping) else {}
    name = _text(value.get("name") or artist.get("name"))
    if not name:
        return None
    return {
        "name": name,
        "artist_mbid": _text(artist.get("id")),
        "sort_name": _text(artist.get("sort-name")),
        "disambiguation": _text(artist.get("disambiguation")),
        "join_phrase": _text(value.get("joinphrase")),
    }


def _release_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    release_group = (
        value.get("release-group")
        if isinstance(value.get("release-group"), Mapping)
        else {}
    )
    release_mbid = _text(value.get("id"))
    title = _text(value.get("title"))
    if not release_mbid and not title:
        return None
    secondary_types = release_group.get("secondary-types")
    if not isinstance(secondary_types, list):
        secondary_types = []
    return {
        "release_mbid": release_mbid,
        "title": title,
        "status": _text(value.get("status")),
        "date": _text(value.get("date")),
        "country": _text(value.get("country")),
        "track_count": _safe_int(value.get("track-count")),
        "release_group": {
            "release_group_mbid": _text(release_group.get("id")),
            "title": _text(release_group.get("title")),
            "primary_type": _text(release_group.get("primary-type")),
            "secondary_types": [
                _text(item) for item in secondary_types if _text(item)
            ],
        },
    }


def _alias_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    name = _text(value.get("name"))
    if not name:
        return None
    primary_value = value.get("primary")
    return {
        "name": name,
        "sort_name": _text(value.get("sort-name")),
        "locale": _text(value.get("locale")),
        "type": _text(value.get("type")),
        "primary": primary_value if isinstance(primary_value, bool) else None,
        "begin_date": _text(value.get("begin-date")),
        "end_date": _text(value.get("end-date")),
    }


def _candidate_projection(value: Any, *, rank: int) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    recording_mbid = _text(value.get("id"))
    title = _text(value.get("title"))
    if not recording_mbid or not title:
        return None
    artist_values = value.get("artist-credit")
    if not isinstance(artist_values, list):
        artist_values = []
    artists = [
        artist
        for artist in (_artist_projection(item) for item in artist_values)
        if artist is not None
    ]
    artist_credit = "".join(
        f"{artist['name']}{artist['join_phrase']}" for artist in artists
    ).strip()
    release_values = value.get("releases")
    if not isinstance(release_values, list):
        release_values = []
    releases = [
        release
        for release in (_release_projection(item) for item in release_values)
        if release is not None
    ]
    alias_values = value.get("aliases")
    if not isinstance(alias_values, list):
        alias_values = []
    aliases = [
        alias
        for alias in (_alias_projection(item) for item in alias_values)
        if alias is not None
    ]
    isrc_values = value.get("isrcs")
    if not isinstance(isrc_values, list):
        isrc_values = []
    isrcs = sorted(
        {
            normalized
            for normalized in (_normalize_isrc(item) for item in isrc_values)
            if normalized
        }
    )
    video_value = value.get("video")
    return {
        "rank": rank,
        "recording_mbid": recording_mbid,
        "title": title,
        "artist_credit": artist_credit,
        "artists": artists,
        "isrcs": isrcs,
        "duration_ms": _safe_int(value.get("length"), minimum=1),
        "releases": releases,
        "provider_score": _safe_number(value.get("score")),
        "variant_metadata": {
            "disambiguation": _text(value.get("disambiguation")),
            "first_release_date": _text(value.get("first-release-date")),
            "is_video": video_value if isinstance(video_value, bool) else None,
            "aliases": aliases,
        },
    }


def _duration_comparable(left_ms: int | None, right_ms: int | None) -> bool:
    if left_ms is None or right_ms is None:
        return False
    tolerance = max(
        int(MATCH_POLICY["duration_absolute_tolerance_ms"]),
        int(round(max(left_ms, right_ms) * MATCH_POLICY["duration_relative_tolerance"])),
    )
    return abs(left_ms - right_ms) <= tolerance


def score_candidate(
    platform_audio: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Score one projected candidate with the deterministic v1 matcher."""

    source_title = _match_text(platform_audio.get("title"))
    candidate_title = _match_text(candidate.get("title"))
    title_match = bool(source_title and source_title == candidate_title)

    source_artist = _match_text(platform_audio.get("author"))
    candidate_artists = {
        _match_text(candidate.get("artist_credit")),
        *(
            _match_text(artist.get("name"))
            for artist in candidate.get("artists", [])
            if isinstance(artist, Mapping)
        ),
    }
    candidate_artists.discard("")
    artist_match = bool(source_artist and source_artist in candidate_artists)

    source_isrc = _normalize_isrc(platform_audio.get("isrc"))
    candidate_isrcs = {
        normalized
        for normalized in (
            _normalize_isrc(item) for item in candidate.get("isrcs", [])
        )
        if normalized
    }
    isrc_match = bool(source_isrc and source_isrc in candidate_isrcs)
    comparable_duration = _duration_comparable(
        _safe_int(platform_audio.get("duration_ms"), minimum=1),
        _safe_int(candidate.get("duration_ms"), minimum=1),
    )

    title_points = MATCH_POLICY["title_points"] if title_match else 0
    artist_points = MATCH_POLICY["artist_points"] if artist_match else 0
    isrc_points = MATCH_POLICY["isrc_points"] if isrc_match else 0
    duration_points = MATCH_POLICY["duration_points"] if comparable_duration else 0
    score = title_points + artist_points + isrc_points + duration_points
    eligible = bool(
        title_match
        and artist_match
        and score >= MATCH_POLICY["minimum_score"]
    )
    return {
        "score": score,
        "eligible": eligible,
        "title_match": title_match,
        "artist_match": artist_match,
        "isrc_match": isrc_match,
        "duration_comparable": comparable_duration,
        "points": {
            "title": title_points,
            "artist": artist_points,
            "isrc": isrc_points,
            "duration": duration_points,
        },
    }


def _lucene_phrase(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_search_url(config: MusicBrainzConfig, audio: Mapping[str, Any]) -> str:
    title_artist = (
        f'recording:"{_lucene_phrase(_text(audio["title"]))}" '
        f'AND artist:"{_lucene_phrase(_text(audio["author"]))}"'
    )
    isrc = _normalize_isrc(audio.get("isrc"))
    query = f"(isrc:{isrc}) OR ({title_artist})" if isrc else title_artist
    separator = "&" if "?" in config.endpoint else "?"
    return config.endpoint + separator + urllib_parse.urlencode(
        {"query": query, "fmt": "json", "limit": config.candidate_limit}
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    for key, value in headers.items():
        if key.casefold() == "retry-after":
            return _safe_int(value, minimum=0, maximum=86_400)
    return None


def _base_result(
    audio: Mapping[str, Any],
    config: MusicBrainzConfig,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "status": "unavailable",
        "platform_audio": dict(audio),
        "query": {
            "type": "recording_by_title_and_artist",
            "candidate_limit": config.candidate_limit,
        },
        "match_policy": dict(MATCH_POLICY),
        "provider_total_count": None,
        "provider_returned_count": 0,
        "retained_candidate_count": 0,
        "invalid_candidate_count": 0,
        "provider_results_truncated": False,
        "candidates": [],
        "selected_candidate_rank": None,
        "selected_recording_mbid": "",
        "decision": {
            "top_score": None,
            "runner_up_score": None,
            "score_margin": None,
            "reason": "not_evaluated",
        },
        "error": None,
    }


def _finished(result: Mapping[str, Any]) -> dict[str, Any]:
    status = _text(result.get("status"))
    if status not in VALID_STATUSES:
        raise MusicEnrichmentError(f"invalid enrichment status: {status}")
    return bind_canonical_hash(result)


def retired_musicbrainz_result(
    platform_audio: Mapping[str, Any],
    *,
    config: MusicBrainzConfig,
) -> dict[str, Any]:
    """Keep the historical result schema while recording no provider activity."""

    result = _base_result(normalize_platform_audio(platform_audio), config)
    result["status"] = "unsupported"
    result["decision"]["reason"] = "provider_retired"
    result["error"] = {
        "code": "provider_retired",
        "message": "MusicBrainz is retired; no lookup was attempted",
    }
    return _finished(result)


class MusicBrainzAdapter:
    """Compatibility adapter for the retired MusicBrainz provider."""

    def __init__(
        self,
        config: MusicBrainzConfig,
        *,
        transport: HTTPTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibHTTPTransport(
            max_response_bytes=config.max_response_bytes
        )

    def enrich(self, platform_audio: Mapping[str, Any]) -> dict[str, Any]:
        """Return the terminal retirement outcome without contacting a provider."""

        return retired_musicbrainz_result(platform_audio, config=self.config)


def enrich_musicbrainz(
    platform_audio: Mapping[str, Any],
    *,
    config: MusicBrainzConfig,
    transport: HTTPTransport | None = None,
) -> dict[str, Any]:
    """Compatibility facade returning a local MusicBrainz retirement result."""

    return MusicBrainzAdapter(config, transport=transport).enrich(platform_audio)
