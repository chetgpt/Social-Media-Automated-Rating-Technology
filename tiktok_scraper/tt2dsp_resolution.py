"""Resolve safe TikTok ``tt2dsp`` linkage IDs into catalog metadata.

TikTok's consumer API may return a ``tt2dsp`` linkage while leaving the outer
sound labelled ``original sound``.  This module resolves only a verified
provider identifier.  It does not download media, retain provider artwork or
preview URLs, inspect TikTok tokens, or perform acoustic recognition.

The platform-number mapping is an observed TikTok web contract rather than a
documented public TikTok API.  Version 1 therefore supports only the mapping
that can be validated deterministically: platform ``1`` is an Apple/iTunes
track identifier.  Unknown platforms remain explicit terminal outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping, Sequence
from urllib import parse as urllib_parse

from tiktok_scraper.music_enrichment import (
    HTTPTransport,
    UrllibHTTPTransport,
    bind_canonical_hash,
    verify_canonical_hash,
)


SCHEMA_VERSION = "tiktok-tt2dsp-resolution-v1"
PROVIDER = "apple_itunes_lookup"
VALID_STATUSES = frozenset(
    {
        "resolved",
        "not_found",
        "unsupported",
        "unavailable",
        "rate_limited",
        "provider_error",
    }
)
APPLE_PLATFORM_CODE = "1"
APPLE_MIN_REQUEST_INTERVAL_SECONDS = 3.05
_NUMERIC_ID_RE = re.compile(r"^[1-9]\d{1,24}$")


class TT2DSPResolutionError(ValueError):
    """Raised when resolver configuration or a document is invalid."""


@dataclass(frozen=True)
class TT2DSPResolutionConfig:
    """Bounded Apple lookup configuration for one workspace/storefront."""

    storefront: str = "ID"
    timeout_seconds: float = 15.0
    endpoint: str = "https://itunes.apple.com/lookup"
    max_response_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        storefront = self.storefront.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", storefront):
            raise TT2DSPResolutionError("storefront must be a two-letter code")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise TT2DSPResolutionError("timeout_seconds must be positive")
        if self.endpoint != "https://itunes.apple.com/lookup":
            raise TT2DSPResolutionError("Apple lookup endpoint is fixed")
        if self.max_response_bytes < 1024:
            raise TT2DSPResolutionError("max_response_bytes must be at least 1024")


def _text(value: Any, *, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def normalize_tt2dsp_links(value: Any) -> list[dict[str, str]]:
    """Return a bounded allowlist projection of untrusted TikTok links."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    links: list[dict[str, str]] = []
    for raw in list(value)[:10]:
        if not isinstance(raw, Mapping):
            continue
        link = {
            key: _text(raw.get(key), limit=200)
            for key in ("meta_song_id", "song_id", "platform", "button_type")
            if _text(raw.get(key), limit=200)
        }
        if link and link not in links:
            links.append(link)
    return links


def apple_song_ids(value: Any) -> list[str]:
    """Return distinct validated Apple IDs from safe tt2dsp rows."""

    result: list[str] = []
    for link in normalize_tt2dsp_links(value):
        song_id = link.get("song_id", "")
        if (
            link.get("platform") == APPLE_PLATFORM_CODE
            and _NUMERIC_ID_RE.fullmatch(song_id)
            and song_id not in result
        ):
            result.append(song_id)
    return result


def _safe_int(value: Any, *, minimum: int = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= minimum else None


def _base_document(links: Any, config: TT2DSPResolutionConfig) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "status": "unsupported",
        "input_links": normalize_tt2dsp_links(links),
        "storefront": config.storefront.strip().upper(),
        "selected": None,
        "decision": {"reason": "no_supported_apple_link"},
        "error": None,
        "identification_basis": "tiktok_tt2dsp_external_catalog_resolution",
        "acoustic_recognition_performed": False,
    }


def terminal_tt2dsp_resolution(
    links: Any,
    *,
    config: TT2DSPResolutionConfig | None = None,
    status: str,
    reason: str,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a closed, hash-bound terminal resolver document."""

    if status not in VALID_STATUSES:
        raise TT2DSPResolutionError("invalid terminal status")
    cfg = config or TT2DSPResolutionConfig()
    document = _base_document(links, cfg)
    document["status"] = status
    document["decision"] = {"reason": _text(reason, limit=200)}
    if isinstance(error, Mapping):
        safe_error = {
            key: value
            for key in ("code", "http_status", "retry_after_seconds")
            if (value := error.get(key)) not in (None, "")
        }
        document["error"] = safe_error or None
    return bind_canonical_hash(document, field_name="resolution_hash")


class TT2DSPResolver:
    """Resolve a TikTok Apple DSP linkage using Apple's ID lookup API."""

    def __init__(
        self,
        config: TT2DSPResolutionConfig | None = None,
        *,
        transport: HTTPTransport | None = None,
    ) -> None:
        self.config = config or TT2DSPResolutionConfig()
        self.transport = transport or UrllibHTTPTransport(
            max_response_bytes=self.config.max_response_bytes
        )

    def resolve(self, links: Any) -> dict[str, Any]:
        safe_links = normalize_tt2dsp_links(links)
        ids = apple_song_ids(safe_links)
        if not ids:
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="unsupported",
                reason="no_supported_apple_link",
            )
        # Multiple different Apple IDs in one contained declaration are not
        # silently collapsed into one recording identity.
        if len(ids) != 1:
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="unsupported",
                reason="multiple_apple_track_ids",
            )
        requested_id = ids[0]
        query = urllib_parse.urlencode(
            {
                "id": requested_id,
                "entity": "song",
                "country": self.config.storefront.strip().upper(),
            }
        )
        try:
            response = self.transport.get(
                f"{self.config.endpoint}?{query}",
                headers={"Accept": "application/json"},
                timeout=self.config.timeout_seconds,
            )
        except Exception:
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="unavailable",
                reason="transport_error",
                error={"code": "transport_error"},
            )
        if response.status_code == 429:
            retry_after = None
            for key, value in response.headers.items():
                if str(key).casefold() == "retry-after":
                    try:
                        retry_after = max(0.0, float(value))
                    except (TypeError, ValueError):
                        retry_after = None
                    break
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="rate_limited",
                reason="provider_rate_limited",
                error={
                    "code": "provider_rate_limited",
                    "http_status": 429,
                    "retry_after_seconds": retry_after,
                },
            )
        if response.status_code != 200:
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="provider_error",
                reason="provider_http_error",
                error={
                    "code": "provider_http_error",
                    "http_status": int(response.status_code),
                },
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="provider_error",
                reason="invalid_provider_payload",
                error={"code": "invalid_provider_payload"},
            )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="provider_error",
                reason="invalid_provider_payload",
                error={"code": "invalid_provider_payload"},
            )
        exact = []
        for raw in results[:25]:
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("trackId") or "") != requested_id:
                continue
            if _text(raw.get("kind")).casefold() != "song":
                continue
            title = _text(raw.get("trackName"))
            artist = _text(raw.get("artistName"))
            if not title or not artist:
                continue
            exact.append(raw)
        if not exact:
            return terminal_tt2dsp_resolution(
                safe_links,
                config=self.config,
                status="not_found",
                reason="exact_track_id_not_returned",
            )
        raw = exact[0]
        selected = {
            "provider_track_id": requested_id,
            "title": _text(raw.get("trackName")),
            "artist": _text(raw.get("artistName")),
            "album": _text(raw.get("collectionName")),
            "collection_id": str(raw.get("collectionId") or "")[:50],
            "duration_ms": _safe_int(raw.get("trackTimeMillis"), minimum=1),
            "release_date": _text(raw.get("releaseDate"), limit=50),
            "genre": _text(raw.get("primaryGenreName"), limit=200),
            "explicitness": _text(raw.get("trackExplicitness"), limit=50),
        }
        document = _base_document(safe_links, self.config)
        document.update(
            status="resolved",
            selected=selected,
            decision={"reason": "exact_provider_track_id"},
        )
        return bind_canonical_hash(document, field_name="resolution_hash")


def resolve_tt2dsp(
    links: Any,
    *,
    config: TT2DSPResolutionConfig | None = None,
    transport: HTTPTransport | None = None,
) -> dict[str, Any]:
    """Functional facade for one tt2dsp resolution."""

    return TT2DSPResolver(config, transport=transport).resolve(links)


def validate_tt2dsp_resolution_document(document: Mapping[str, Any]) -> bool:
    """Validate the closed, hash-bound resolver result stored in evidence."""

    if not isinstance(document, Mapping) or not verify_canonical_hash(
        document, field_name="resolution_hash"
    ):
        return False
    if set(document) != {
        "schema_version",
        "provider",
        "status",
        "input_links",
        "storefront",
        "selected",
        "decision",
        "error",
        "identification_basis",
        "acoustic_recognition_performed",
        "resolution_hash",
    }:
        return False
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("provider") != PROVIDER
        or document.get("status") not in VALID_STATUSES
        or document.get("input_links")
        != normalize_tt2dsp_links(document.get("input_links"))
        or not re.fullmatch(r"[A-Z]{2}", _text(document.get("storefront")))
        or document.get("identification_basis")
        != "tiktok_tt2dsp_external_catalog_resolution"
        or document.get("acoustic_recognition_performed") is not False
    ):
        return False
    decision = document.get("decision")
    if not isinstance(decision, Mapping) or set(decision) != {"reason"}:
        return False
    error = document.get("error")
    if error is not None and (
        not isinstance(error, Mapping)
        or not set(error).issubset(
            {"code", "http_status", "retry_after_seconds"}
        )
    ):
        return False
    selected = document.get("selected")
    if document.get("status") == "resolved":
        if not isinstance(selected, Mapping) or set(selected) != {
            "provider_track_id",
            "title",
            "artist",
            "album",
            "collection_id",
            "duration_ms",
            "release_date",
            "genre",
            "explicitness",
        }:
            return False
        if (
            not _NUMERIC_ID_RE.fullmatch(_text(selected.get("provider_track_id")))
            or not _text(selected.get("title"))
            or not _text(selected.get("artist"))
            or _safe_int(selected.get("duration_ms"), minimum=1)
            != selected.get("duration_ms")
        ):
            return False
    elif selected is not None:
        return False
    return True
