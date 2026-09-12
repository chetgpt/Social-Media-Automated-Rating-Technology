"""Safe TikTok sound-page URL parsing and deterministic locator generation.

TikTok sound URLs identify a platform-scoped sound by the trailing numeric
``music_id``.  The human-readable slug is routing/display metadata only.  This
module performs no network or browser access and never handles media URLs,
cookies, tokens, or raw page payloads.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import quote, unquote, urlsplit


SCHEMA_VERSION = "tiktok-music-page-locator-v1"
CANONICAL_HOST = "www.tiktok.com"
ALLOWED_HOSTS = frozenset({"tiktok.com", CANONICAL_HOST})
MUSIC_ID_PATTERN = re.compile(r"^[0-9]{5,30}$")
MUSIC_SEGMENT_PATTERN = re.compile(
    r"^(?P<slug>.+)-(?P<music_id>[0-9]{5,30})$"
)


class TikTokMusicPageURLValueError(ValueError):
    """Raised when a value is not one exact, safe TikTok sound-detail URL."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> TikTokMusicPageURLValueError:
    return TikTokMusicPageURLValueError(code, message)


def _locator(
    *,
    status: str,
    music_id: str,
    slug: str = "",
    url: str = "",
    source: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "music_id": music_id,
        "slug": slug,
        "url": url,
        "source": source,
        "online_verification": "not_attempted",
        "reason": reason,
    }


def normalize_tiktok_music_page_url(value: Any) -> dict[str, Any]:
    """Normalize one exact TikTok sound-detail URL without visiting it.

    The URL must be HTTPS and query-free, and its path must be exactly
    ``/music/<non-empty-slug>-<numeric-id>``.  A bare ``/music/`` path is a
    route prefix, not a sound identity.
    """

    raw = str(value or "").strip()
    if not raw:
        raise _error("exact_music_page_required", "an exact TikTok music URL is required")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise _error("invalid_music_page_url", "the TikTok music URL is invalid") from exc
    if parsed.scheme.casefold() != "https":
        raise _error("https_required", "the TikTok music URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise _error(
            "authority_not_allowed",
            "userinfo and explicit ports are not allowed in a TikTok music URL",
        )
    host = (parsed.hostname or "").rstrip(".").casefold()
    if host not in ALLOWED_HOSTS:
        raise _error("tiktok_host_required", "the music URL must use tiktok.com")
    if parsed.query or parsed.fragment:
        raise _error(
            "query_or_fragment_not_allowed",
            "the TikTok music URL must not contain a query or fragment",
        )
    path_match = re.fullmatch(r"/music/([^/]+)/?", parsed.path)
    if path_match is None:
        code = (
            "exact_music_page_required"
            if parsed.path.rstrip("/").casefold() == "/music"
            else "invalid_music_page_path"
        )
        raise _error(code, "expected /music/<slug>-<numeric-id>")
    segment = unicodedata.normalize("NFC", unquote(path_match.group(1))).strip()
    if (
        not segment
        or len(segment) > 220
        or any(character in segment for character in ("/", "\\", "?", "#"))
        or any(ord(character) < 32 for character in segment)
    ):
        raise _error("invalid_music_page_path", "the TikTok music path is invalid")
    match = MUSIC_SEGMENT_PATTERN.fullmatch(segment)
    if match is None:
        raise _error(
            "numeric_music_id_required",
            "the TikTok music URL must end with a numeric music ID",
        )
    slug = match.group("slug").strip("- ")
    if not slug or slug in {".", ".."}:
        raise _error("music_slug_required", "the TikTok music URL needs a sound slug")
    music_id = match.group("music_id")
    encoded_slug = quote(slug, safe="-._~")
    return _locator(
        status="observed",
        music_id=music_id,
        slug=slug,
        url=f"https://{CANONICAL_HOST}/music/{encoded_slug}-{music_id}",
        source="supplied_exact_url",
        reason="exact_url_normalized",
    )


def _slug_from_title(value: Any) -> str:
    title = unicodedata.normalize("NFC", str(value or "")).strip()
    pieces: list[str] = []
    pending_separator = False
    for character in title:
        if character.isalnum() or character in "._~":
            if pending_separator and pieces:
                pieces.append("-")
            pieces.append(character)
            pending_separator = False
        else:
            pending_separator = True
    slug = "".join(pieces).strip("-._~")
    slug = re.sub(r"-+", "-", slug)
    return slug[:160].rstrip("-") or "sound"


def build_tiktok_music_page_locator(
    music_id: Any,
    title: Any,
    observed_url: Any = "",
) -> dict[str, Any]:
    """Build a safe locator from TikTok-declared music fields.

    A derived locator is navigation metadata only.  It does not claim that the
    page was resolved online or that the sound matches a commercial master.
    """

    normalized_id = str(music_id or "").strip()
    if not MUSIC_ID_PATTERN.fullmatch(normalized_id):
        return _locator(
            status="not_available",
            music_id=normalized_id[:64],
            source="platform_music",
            reason="numeric_music_id_required",
        )
    observed = str(observed_url or "").strip()
    if observed:
        try:
            target = normalize_tiktok_music_page_url(observed)
        except TikTokMusicPageURLValueError:
            target = None
        if target is not None:
            if target["music_id"] != normalized_id:
                return _locator(
                    status="conflict",
                    music_id=normalized_id,
                    slug=target["slug"],
                    source="observed_tiktok_music_url",
                    reason="observed_url_music_id_mismatch",
                )
            return {
                **target,
                "source": "observed_tiktok_music_url",
                "reason": "observed_same_id_url_normalized",
            }
    slug = _slug_from_title(title)
    reason = (
        "derived_from_music_id_and_title"
        if str(title or "").strip()
        else "derived_from_music_id_only"
    )
    return _locator(
        status="derived",
        music_id=normalized_id,
        slug=slug,
        url=f"https://{CANONICAL_HOST}/music/{quote(slug, safe='-._~')}-{normalized_id}",
        source="derived_from_platform_music",
        reason=reason,
    )

