"""Small, dependency-injected client for TikTok's Content Posting API.

The client intentionally supports only the official PHOTO + DIRECT_POST +
PULL_FROM_URL flow needed by this workspace.  It performs no implicit network
setup: callers must inject a requests-compatible session.

TikTok API references:

* https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
* https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
* https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


API_ORIGIN = "https://open.tiktokapis.com"
CREATOR_INFO_URL = f"{API_ORIGIN}/v2/post/publish/creator_info/query/"
PHOTO_INIT_URL = f"{API_ORIGIN}/v2/post/publish/content/init/"
STATUS_FETCH_URL = f"{API_ORIGIN}/v2/post/publish/status/fetch/"

MAX_PHOTO_COUNT = 35
MAX_PHOTO_TITLE_UTF16 = 90
MAX_PHOTO_DESCRIPTION_UTF16 = 4_000
MAX_PUBLISH_ID_LENGTH = 64

KNOWN_PRIVACY_LEVELS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)
KNOWN_STATUS_VALUES = frozenset(
    {
        "PROCESSING_UPLOAD",
        "PROCESSING_DOWNLOAD",
        "SEND_TO_USER_INBOX",
        "PUBLISH_COMPLETE",
        "FAILED",
    }
)
TERMINAL_STATUS_VALUES = frozenset({"PUBLISH_COMPLETE", "FAILED"})


class TikTokContentPostingError(RuntimeError):
    """Base class for safe, redacted Content Posting API failures."""


class TikTokValidationError(TikTokContentPostingError, ValueError):
    """Raised before an invalid request can be sent."""


class TikTokCreatorMismatchError(TikTokValidationError):
    """Raised when the token belongs to a different TikTok creator."""

    def __init__(self, *, expected: str, observed: str) -> None:
        self.expected = expected
        self.observed = observed
        super().__init__(
            "TikTok creator mismatch: "
            f"expected {expected!r}, observed {observed!r}"
        )


class TikTokTransportError(TikTokContentPostingError):
    """Raised when the injected HTTP transport fails."""


class TikTokProtocolError(TikTokContentPostingError):
    """Raised when TikTok returns a malformed or unexpected response."""


class TikTokAPIError(TikTokContentPostingError):
    """A non-``ok`` error envelope returned by TikTok."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        log_id: str | None,
        http_status: int,
    ) -> None:
        self.code = code
        self.message = message
        self.log_id = log_id
        self.http_status = http_status
        diagnostic = f"TikTok API error {code!r} (HTTP {http_status})"
        if message:
            diagnostic += f": {message}"
        if log_id:
            diagnostic += f" [log_id={log_id}]"
        super().__init__(diagnostic)


class TikTokStatusTimeout(TikTokContentPostingError, TimeoutError):
    """Raised when bounded polling ends before TikTok reaches a final state."""

    def __init__(
        self,
        *,
        publish_id: str,
        attempts: int,
        last_status: str | None,
    ) -> None:
        self.publish_id = publish_id
        self.attempts = attempts
        self.last_status = last_status
        detail = f"; last status was {last_status}" if last_status else ""
        super().__init__(
            f"TikTok publish {publish_id!r} did not finish after "
            f"{attempts} status checks{detail}"
        )


@dataclass(frozen=True, slots=True)
class CreatorInfo:
    """Validated creator information returned by TikTok."""

    creator_username: str
    creator_nickname: str
    privacy_level_options: tuple[str, ...]
    comment_disabled: bool


@dataclass(frozen=True, slots=True)
class PhotoPublishInitiation:
    """Safe subset of the successful photo initialization response."""

    publish_id: str
    creator_username: str
    privacy_level: str
    photo_count: int


@dataclass(frozen=True, slots=True)
class PublishStatus:
    """Validated status for one Content Posting API publish operation."""

    publish_id: str
    status: str
    fail_reason: str | None
    publicly_available_post_ids: tuple[str, ...]
    uploaded_bytes: int | None
    downloaded_bytes: int | None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUS_VALUES

    @property
    def successful(self) -> bool:
        return self.status == "PUBLISH_COMPLETE"


def utf16_code_units(value: str) -> int:
    """Return the number of UTF-16 code units used by ``value``.

    TikTok expresses its text limits in UTF-16 units.  An astral character
    such as an emoji therefore consumes two units.
    """

    if not isinstance(value, str):
        raise TikTokValidationError("text values must be strings")
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise TikTokValidationError(
            "text values must not contain unpaired Unicode surrogates"
        ) from None


def validate_https_media_url(value: str) -> str:
    """Validate the local, syntactic portion of a PULL_FROM_URL image URL.

    TikTok separately verifies public reachability and ownership of the HTTPS
    domain or URL prefix.  This function deliberately does not make a network
    request.
    """

    if not isinstance(value, str) or not value:
        raise TikTokValidationError("each photo URL must be a non-empty string")
    if value != value.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise TikTokValidationError("photo URLs must not contain whitespace")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise TikTokValidationError("photo URL is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise TikTokValidationError("photo URLs must use HTTPS")
    if not parsed.hostname:
        raise TikTokValidationError("photo URLs must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise TikTokValidationError("photo URLs must not contain credentials")
    if parsed.fragment:
        raise TikTokValidationError("photo URLs must not contain fragments")
    if port is not None and not 1 <= port <= 65_535:
        raise TikTokValidationError("photo URL contains an invalid port")
    return value


def _require_string(
    value: Any,
    *,
    field: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TikTokValidationError(f"{field} must be a string")
    if not allow_empty and not value:
        raise TikTokValidationError(f"{field} must not be empty")
    return value


def _require_publish_id(value: Any) -> str:
    publish_id = _require_string(value, field="publish_id")
    if publish_id != publish_id.strip() or any(
        character.isspace() for character in publish_id
    ):
        raise TikTokValidationError("publish_id must not contain whitespace")
    if len(publish_id) > MAX_PUBLISH_ID_LENGTH:
        raise TikTokValidationError(
            f"publish_id exceeds {MAX_PUBLISH_ID_LENGTH} characters"
        )
    return publish_id


def _require_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise TikTokValidationError(f"{field} must be a boolean")
    return value


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TikTokProtocolError(
            f"TikTok response field {field!r} must be a non-negative integer"
        )
    return value


class TikTokContentPostingClient:
    """Official PHOTO Direct Post API client with fail-closed validation."""

    __slots__ = (
        "_access_token",
        "_clock",
        "_expected_creator_username",
        "_request_timeout",
        "_session",
        "_sleep",
    )

    def __init__(
        self,
        *,
        session: Any,
        access_token: str,
        expected_creator_username: str,
        request_timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(getattr(session, "post", None)):
            raise TikTokValidationError(
                "session must expose a requests-compatible post method"
            )
        token = _require_string(access_token, field="access_token")
        if token != token.strip():
            raise TikTokValidationError(
                "access_token must not have surrounding whitespace"
            )
        username = _require_string(
            expected_creator_username,
            field="expected_creator_username",
        )
        if username != username.strip():
            raise TikTokValidationError(
                "expected_creator_username must not have surrounding whitespace"
            )
        if token == username:
            raise TikTokValidationError(
                "access_token and expected_creator_username must be distinct"
            )
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(float(request_timeout))
            or request_timeout <= 0
        ):
            raise TikTokValidationError(
                "request_timeout must be a finite positive number"
            )
        if not callable(sleep) or not callable(clock):
            raise TikTokValidationError("sleep and clock must be callable")
        self._session = session
        self._access_token = token
        self._expected_creator_username = username
        self._request_timeout = float(request_timeout)
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"expected_creator_username={self._expected_creator_username!r}, "
            "access_token=[REDACTED])"
        )

    @property
    def expected_creator_username(self) -> str:
        return self._expected_creator_username

    def _redact(self, value: Any) -> str:
        rendered = str(value)
        return rendered.replace(self._access_token, "[REDACTED]")

    def _post_json(
        self,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self._request_timeout,
        }
        if payload is not None:
            kwargs["json"] = dict(payload)
        try:
            response = self._session.post(url, **kwargs)
        except Exception:
            # A requests exception can include prepared-request headers.  Do
            # not chain it or interpolate it because that could expose the
            # bearer token.
            raise TikTokTransportError(
                "TikTok Content Posting API transport failed"
            ) from None

        http_status = getattr(response, "status_code", None)
        if isinstance(http_status, bool) or not isinstance(http_status, int):
            raise TikTokProtocolError(
                "TikTok response did not include a numeric HTTP status"
            )
        try:
            envelope = response.json()
        except Exception:
            raise TikTokProtocolError(
                f"TikTok returned invalid JSON (HTTP {http_status})"
            ) from None
        if not isinstance(envelope, Mapping):
            raise TikTokProtocolError("TikTok response must be a JSON object")

        error = envelope.get("error")
        if not isinstance(error, Mapping):
            raise TikTokProtocolError(
                "TikTok response is missing its error envelope"
            )
        code = error.get("code")
        if not isinstance(code, str) or not code:
            raise TikTokProtocolError(
                "TikTok error envelope has no valid error code"
            )
        message_value = error.get("message", "")
        if message_value is None:
            message_value = ""
        if not isinstance(message_value, str):
            raise TikTokProtocolError(
                "TikTok error envelope has an invalid message"
            )
        log_id_value = error.get("log_id", error.get("logid"))
        if log_id_value is not None and not isinstance(log_id_value, str):
            raise TikTokProtocolError(
                "TikTok error envelope has an invalid log ID"
            )

        if code != "ok":
            raise TikTokAPIError(
                code=self._redact(code),
                message=self._redact(message_value),
                log_id=(
                    self._redact(log_id_value)
                    if log_id_value is not None
                    else None
                ),
                http_status=http_status,
            )
        if not 200 <= http_status < 300:
            raise TikTokProtocolError(
                "TikTok returned an ok envelope with a non-success "
                f"HTTP status ({http_status})"
            )
        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise TikTokProtocolError(
                "TikTok success response is missing its data object"
            )
        return data

    def query_creator_info(self) -> CreatorInfo:
        """Fetch current creator permissions and enforce the account binding."""

        data = self._post_json(CREATOR_INFO_URL)
        observed = data.get("creator_username")
        if not isinstance(observed, str) or not observed:
            raise TikTokProtocolError(
                "TikTok creator info has no valid creator_username"
            )
        if observed != self._expected_creator_username:
            raise TikTokCreatorMismatchError(
                expected=self._expected_creator_username,
                observed=self._redact(observed),
            )

        nickname = data.get("creator_nickname", "")
        if nickname is None:
            nickname = ""
        if not isinstance(nickname, str):
            raise TikTokProtocolError(
                "TikTok creator info has an invalid creator_nickname"
            )

        options_value = data.get("privacy_level_options")
        if (
            not isinstance(options_value, Sequence)
            or isinstance(options_value, (str, bytes))
            or not options_value
        ):
            raise TikTokProtocolError(
                "TikTok creator info has no privacy level options"
            )
        options: list[str] = []
        for option in options_value:
            if (
                not isinstance(option, str)
                or option not in KNOWN_PRIVACY_LEVELS
                or option in options
            ):
                raise TikTokProtocolError(
                    "TikTok creator info contains an invalid privacy level"
                )
            options.append(option)

        comment_disabled = data.get("comment_disabled")
        if type(comment_disabled) is not bool:
            raise TikTokProtocolError(
                "TikTok creator info has an invalid comment_disabled value"
            )
        return CreatorInfo(
            creator_username=observed,
            creator_nickname=self._redact(nickname),
            privacy_level_options=tuple(options),
            comment_disabled=comment_disabled,
        )

    def initialize_photo_direct_post(
        self,
        *,
        photo_urls: Sequence[str],
        privacy_level: str,
        title: str = "",
        caption: str = "",
        photo_cover_index: int = 0,
        allow_comments: bool = True,
        auto_add_music: bool = False,
        brand_content: bool = False,
        brand_organic: bool = False,
        before_submit: Callable[[], None] | None = None,
    ) -> PhotoPublishInitiation:
        """Start a PHOTO ``DIRECT_POST`` using ``PULL_FROM_URL``.

        A fresh creator-info request is always made immediately before the
        initialization request.  This prevents a stale privacy choice or an
        access token for another account from being used.
        """

        if isinstance(photo_urls, (str, bytes)) or not isinstance(
            photo_urls, Sequence
        ):
            raise TikTokValidationError(
                "photo_urls must be a sequence of HTTPS URLs"
            )
        if not 1 <= len(photo_urls) <= MAX_PHOTO_COUNT:
            raise TikTokValidationError(
                f"photo_urls must contain between 1 and {MAX_PHOTO_COUNT} items"
            )
        validated_urls = tuple(validate_https_media_url(url) for url in photo_urls)
        if len(set(validated_urls)) != len(validated_urls):
            raise TikTokValidationError("photo_urls must not contain duplicates")

        title = _require_string(title, field="title", allow_empty=True)
        caption = _require_string(caption, field="caption", allow_empty=True)
        title_length = utf16_code_units(title)
        caption_length = utf16_code_units(caption)
        if title_length > MAX_PHOTO_TITLE_UTF16:
            raise TikTokValidationError(
                f"title exceeds {MAX_PHOTO_TITLE_UTF16} UTF-16 code units"
            )
        if caption_length > MAX_PHOTO_DESCRIPTION_UTF16:
            raise TikTokValidationError(
                "caption exceeds "
                f"{MAX_PHOTO_DESCRIPTION_UTF16} UTF-16 code units"
            )

        privacy_level = _require_string(
            privacy_level,
            field="privacy_level",
        )
        if privacy_level not in KNOWN_PRIVACY_LEVELS:
            raise TikTokValidationError("privacy_level is not recognized")
        if (
            isinstance(photo_cover_index, bool)
            or not isinstance(photo_cover_index, int)
            or not 0 <= photo_cover_index < len(validated_urls)
        ):
            raise TikTokValidationError(
                "photo_cover_index must select one of the supplied photos"
            )
        allow_comments = _require_bool(
            allow_comments,
            field="allow_comments",
        )
        auto_add_music = _require_bool(
            auto_add_music,
            field="auto_add_music",
        )
        brand_content = _require_bool(
            brand_content,
            field="brand_content",
        )
        brand_organic = _require_bool(
            brand_organic,
            field="brand_organic",
        )
        if before_submit is not None and not callable(before_submit):
            raise TikTokValidationError("before_submit must be callable")

        creator = self.query_creator_info()
        if privacy_level not in creator.privacy_level_options:
            raise TikTokValidationError(
                f"privacy_level {privacy_level!r} is not currently available "
                f"for creator {creator.creator_username!r}"
            )
        if allow_comments and creator.comment_disabled:
            raise TikTokValidationError(
                "comments cannot be enabled because the creator has disabled them"
            )

        payload = {
            "post_info": {
                "title": title,
                "description": caption,
                "privacy_level": privacy_level,
                "disable_comment": not allow_comments,
                "auto_add_music": auto_add_music,
                "brand_content_toggle": brand_content,
                "brand_organic_toggle": brand_organic,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_images": list(validated_urls),
                "photo_cover_index": photo_cover_index,
            },
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        }
        if before_submit is not None:
            before_submit()
        data = self._post_json(PHOTO_INIT_URL, payload=payload)
        publish_id = data.get("publish_id")
        try:
            publish_id = _require_publish_id(publish_id)
        except TikTokValidationError as exc:
            raise TikTokProtocolError(
                f"TikTok photo initialization returned an invalid publish_id: {exc}"
            ) from None
        if self._access_token in publish_id:
            raise TikTokProtocolError(
                "TikTok photo initialization returned an unsafe publish_id"
            )
        return PhotoPublishInitiation(
            publish_id=publish_id,
            creator_username=creator.creator_username,
            privacy_level=privacy_level,
            photo_count=len(validated_urls),
        )

    def fetch_publish_status(self, publish_id: str) -> PublishStatus:
        """Fetch and validate one current publish status."""

        publish_id = _require_publish_id(publish_id)
        if self._access_token in publish_id:
            raise TikTokValidationError("publish_id contains protected data")
        data = self._post_json(
            STATUS_FETCH_URL,
            payload={"publish_id": publish_id},
        )
        status = data.get("status")
        if not isinstance(status, str) or status not in KNOWN_STATUS_VALUES:
            raise TikTokProtocolError(
                "TikTok status response contains an unknown status"
            )

        fail_reason = data.get("fail_reason")
        if fail_reason in ("", None):
            fail_reason = None
        elif not isinstance(fail_reason, str):
            raise TikTokProtocolError(
                "TikTok status response has an invalid fail_reason"
            )
        else:
            fail_reason = self._redact(fail_reason)

        public_ids_value = data.get("publicaly_available_post_id", ())
        if (
            not isinstance(public_ids_value, Sequence)
            or isinstance(public_ids_value, (str, bytes))
        ):
            raise TikTokProtocolError(
                "TikTok status response has invalid public post IDs"
            )
        public_ids: list[str] = []
        for post_id in public_ids_value:
            if isinstance(post_id, bool) or not isinstance(post_id, (int, str)):
                raise TikTokProtocolError(
                    "TikTok status response has an invalid public post ID"
                )
            normalized = str(post_id)
            if (
                not normalized.isascii()
                or not normalized.isdigit()
                or len(normalized) > 20
                or int(normalized) <= 0
            ):
                raise TikTokProtocolError(
                    "TikTok status response has an invalid public post ID"
                )
            public_ids.append(normalized)

        return PublishStatus(
            publish_id=publish_id,
            status=status,
            fail_reason=fail_reason,
            publicly_available_post_ids=tuple(public_ids),
            uploaded_bytes=_optional_nonnegative_int(
                data.get("uploaded_bytes"),
                field="uploaded_bytes",
            ),
            downloaded_bytes=_optional_nonnegative_int(
                data.get("downloaded_bytes"),
                field="downloaded_bytes",
            ),
        )

    def poll_publish_status(
        self,
        publish_id: str,
        *,
        timeout_seconds: float = 120.0,
        interval_seconds: float = 2.0,
        max_attempts: int = 60,
    ) -> PublishStatus:
        """Poll until success/failure, bounded by both time and attempts."""

        publish_id = _require_publish_id(publish_id)
        if self._access_token in publish_id:
            raise TikTokValidationError("publish_id contains protected data")
        for value, field, allow_zero in (
            (timeout_seconds, "timeout_seconds", True),
            (interval_seconds, "interval_seconds", True),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
                or (not allow_zero and value == 0)
            ):
                raise TikTokValidationError(
                    f"{field} must be a finite non-negative number"
                )
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise TikTokValidationError("max_attempts must be a positive integer")

        started_at = float(self._clock())
        deadline = started_at + float(timeout_seconds)
        attempts = 0
        last_status: PublishStatus | None = None
        while attempts < max_attempts:
            last_status = self.fetch_publish_status(publish_id)
            attempts += 1
            if last_status.terminal:
                return last_status
            if attempts >= max_attempts:
                break
            remaining = deadline - float(self._clock())
            if remaining <= 0:
                break
            delay = min(float(interval_seconds), remaining)
            if delay > 0:
                self._sleep(delay)

        raise TikTokStatusTimeout(
            publish_id=publish_id,
            attempts=attempts,
            last_status=last_status.status if last_status else None,
        )


__all__ = [
    "CREATOR_INFO_URL",
    "PHOTO_INIT_URL",
    "STATUS_FETCH_URL",
    "CreatorInfo",
    "PhotoPublishInitiation",
    "PublishStatus",
    "TikTokAPIError",
    "TikTokContentPostingClient",
    "TikTokContentPostingError",
    "TikTokCreatorMismatchError",
    "TikTokProtocolError",
    "TikTokStatusTimeout",
    "TikTokTransportError",
    "TikTokValidationError",
    "utf16_code_units",
    "validate_https_media_url",
]
