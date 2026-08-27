"""Small, official-API-only LinkedIn Community Management client.

The public methods in this module return closed, normalized projections.  They
never return response bodies, request headers, OAuth tokens, or transport
objects.  Collection is intentionally restricted to organization-authored
posts; LinkedIn person authors fail closed because member-post read access is a
separate restricted capability.

This module does not implement storage.  Callers remain responsible for
LinkedIn's data-retention and display restrictions, especially the short
retention window for member-authored comments and other member social data.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast


API_BASE_URL = "https://api.linkedin.com/rest"
DEFAULT_API_VERSION = "202608"
RESTLI_PROTOCOL_VERSION = "2.0.0"
OBSERVATION_SCHEMA_VERSION = "linkedin-post-observation-v1"

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_PAGES = 10_000
_ORGANIZATION_URN_RE = re.compile(r"^urn:li:organization:[1-9][0-9]*$")
_POST_URN_RE = re.compile(r"^urn:li:(?:share|ugcPost):[1-9][0-9]*$")
_COMMENT_URN_RE = re.compile(
    r"^urn:li:comment:\(urn:li:activity:[1-9][0-9]*,[1-9][0-9]*\)$"
)
_ACTOR_URN_RE = re.compile(
    r"^urn:li:(?:person:[A-Za-z0-9_-]+|organization(?:Brand)?:[0-9]+)$"
)
_SOCIAL_OBJECT_URN_RE = re.compile(
    r"^urn:li:(?:activity|share|ugcPost):[1-9][0-9]*$"
)
_ASSET_URN_RE = re.compile(
    r"^urn:li:(?:image|video|document|digitalmediaAsset):[A-Za-z0-9_-]+$"
)
_API_VERSION_RE = re.compile(r"^20[0-9]{2}(?:0[1-9]|1[0-2])$")
_SAFE_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CREDENTIAL_TEXT_RE = re.compile(
    r"(?i)\b(?:authorization|access[_-]?token|bearer)\b\s*[:=]?\s*[^\s,;]+"
)
_REACTION_TYPES = frozenset(
    {
        "LIKE",
        "PRAISE",
        "MAYBE",
        "EMPATHY",
        "INTEREST",
        "APPRECIATION",
        "ENTERTAINMENT",
        "CELEBRATION",
        "SUPPORT",
        "LOVE",
        "INSIGHTFUL",
        "CURIOUS",
    }
)
_COMMENTS_STATES = frozenset({"OPEN", "CLOSED", "PROCESSING", "DELETED"})
_CONTENT_KEYS = (
    "article",
    "media",
    "multiImage",
    "poll",
    "carousel",
    "celebration",
    "event",
    "job",
)


class LinkedInValidationError(ValueError):
    """Raised before a request when caller-controlled input is invalid."""

    kind = "invalid_input"


class LinkedInAPIError(RuntimeError):
    """Base class for sanitized LinkedIn transport/API failures."""

    kind = "unavailable"

    def __init__(
        self,
        operation: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"LinkedIn API {self.kind} during {operation}")


class LinkedInForbiddenError(LinkedInAPIError):
    """The token, product, scope, or organization role is insufficient."""

    kind = "forbidden"


class LinkedInRateLimitedError(LinkedInAPIError):
    """LinkedIn returned HTTP 429."""

    kind = "rate_limited"


class LinkedInUnavailableError(LinkedInAPIError):
    """The resource, transport, or provider response is unavailable."""

    kind = "unavailable"


class _PayloadError(ValueError):
    """Internal marker for a provider response outside the closed contract."""


@dataclass(frozen=True)
class _TransportResponse:
    status_code: int
    payload: Any
    headers: Mapping[str, Any]


class _RequestTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Any: ...


class _UrllibTransport:
    """Default transport with no logging and bounded response reads."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> _TransportResponse:
        request = urllib.request.Request(url=url, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                retry_after = response.headers.get("Retry-After")
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return _TransportResponse(
                status_code=int(exc.code),
                payload=None,
                headers={"retry-after": retry_after},
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LinkedInUnavailableError("request") from None

        if len(body) > _MAX_RESPONSE_BYTES:
            raise LinkedInUnavailableError("request")
        if not body:
            payload: Any = {}
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise LinkedInUnavailableError("request") from None
        return _TransportResponse(
            status_code=status,
            payload=payload,
            headers={"retry-after": retry_after},
        )


def validate_organization_urn(value: Any) -> str:
    """Return a canonical organization URN or raise before network access."""

    urn = value if isinstance(value, str) else ""
    if not _ORGANIZATION_URN_RE.fullmatch(urn):
        raise LinkedInValidationError(
            "organization author must be urn:li:organization:<numeric-id>"
        )
    return urn


def validate_post_urn(value: Any) -> str:
    """Accept only the post identifiers supported by LinkedIn's Posts API."""

    urn = value if isinstance(value, str) else ""
    if not _POST_URN_RE.fullmatch(urn):
        raise LinkedInValidationError(
            "post must be a numeric urn:li:share or urn:li:ugcPost URN"
        )
    return urn


def validate_comment_urn(value: Any) -> str:
    urn = value if isinstance(value, str) else ""
    if not _COMMENT_URN_RE.fullmatch(urn):
        raise LinkedInValidationError("invalid LinkedIn comment URN")
    return urn


def _positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise LinkedInValidationError(f"{name} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise LinkedInValidationError(f"{name} must be a positive integer") from None
    if number <= 0 or (maximum is not None and number > maximum):
        suffix = f" no greater than {maximum}" if maximum is not None else ""
        raise LinkedInValidationError(f"{name} must be a positive integer{suffix}")
    return number


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= 9_223_372_036_854_775_807 else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


class LinkedInAPIClient:
    """Official LinkedIn REST client returning only sanitized projections."""

    def __init__(
        self,
        access_token: str,
        api_version: str = DEFAULT_API_VERSION,
        transport: _RequestTransport | Callable[..., Any] | None = None,
        timeout: float = 30,
    ) -> None:
        if (
            not isinstance(access_token, str)
            or not access_token
            or access_token != access_token.strip()
            or any(ord(character) < 32 for character in access_token)
        ):
            raise LinkedInValidationError("a non-empty OAuth access token is required")
        if not isinstance(api_version, str) or not _API_VERSION_RE.fullmatch(
            api_version
        ):
            raise LinkedInValidationError("api_version must use YYYYMM format")
        if isinstance(timeout, bool):
            raise LinkedInValidationError("timeout must be between 0 and 300 seconds")
        try:
            normalized_timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise LinkedInValidationError(
                "timeout must be between 0 and 300 seconds"
            ) from None
        if not math.isfinite(normalized_timeout) or not 0 < normalized_timeout <= 300:
            raise LinkedInValidationError("timeout must be between 0 and 300 seconds")
        if transport is not None and not (
            callable(transport) or callable(getattr(transport, "request", None))
        ):
            raise LinkedInValidationError("transport must be callable or expose request()")

        self._access_token = access_token
        self._api_version = api_version
        self._timeout = normalized_timeout
        self._transport = transport if transport is not None else _UrllibTransport()

    @property
    def api_version(self) -> str:
        return self._api_version

    @property
    def timeout(self) -> float:
        return self._timeout

    def find_author_posts(
        self,
        organization_urn: str,
        *,
        limit: int | None = None,
        page_size: int = 100,
        sort_by: str = "CREATED",
    ) -> list[dict[str, Any]]:
        """Retrieve an administered organization's post inventory with paging.

        Person URNs are rejected before the transport is called.  Results are
        deduplicated by post URN while preserving the API's order.
        """

        organization_urn = validate_organization_urn(organization_urn)
        page_size = _positive_int(page_size, "page_size", maximum=100)
        if limit is not None:
            limit = _positive_int(limit, "limit")
        if sort_by not in {"CREATED", "LAST_MODIFIED"}:
            raise LinkedInValidationError(
                "sort_by must be CREATED or LAST_MODIFIED"
            )

        posts: list[dict[str, Any]] = []
        seen_post_urns: set[str] = set()
        seen_starts: set[int] = set()
        start = 0

        for _ in range(_MAX_PAGES):
            if start in seen_starts:
                raise LinkedInUnavailableError("find_author_posts")
            seen_starts.add(start)
            requested_count = page_size
            if limit is not None:
                requested_count = min(page_size, limit - len(posts))
                if requested_count <= 0:
                    break

            path = "/posts"
            payload = self._request_json(
                "GET",
                path,
                operation="find_author_posts",
                params={
                    "q": "author",
                    "author": organization_urn,
                    "count": requested_count,
                    "start": start,
                    "sortBy": sort_by,
                },
                extra_headers={"X-RestLi-Method": "FINDER"},
            )
            elements = payload.get("elements")
            if not isinstance(elements, list):
                raise LinkedInUnavailableError("find_author_posts")
            try:
                for raw_post in elements:
                    post = self._normalize_post(
                        raw_post,
                        expected_organization_urn=organization_urn,
                    )
                    if post["post_urn"] in seen_post_urns:
                        continue
                    seen_post_urns.add(post["post_urn"])
                    posts.append(post)
                    if limit is not None and len(posts) >= limit:
                        return posts
            except Exception:
                raise LinkedInUnavailableError("find_author_posts") from None

            next_start = self._next_start(
                payload,
                current_start=start,
                returned_count=len(elements),
                requested_count=requested_count,
                expected_path=path,
            )
            if next_start is None:
                return posts
            start = next_start
        else:
            raise LinkedInUnavailableError("find_author_posts")

        return posts

    def find_organization_posts(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Alias that makes the organization-only collection scope explicit."""

        return self.find_author_posts(*args, **kwargs)

    def list_author_post_urns(
        self,
        organization_urn: str,
        *,
        limit: int | None = None,
        page_size: int = 100,
        sort_by: str = "CREATED",
    ) -> list[str]:
        """Return an orchestration-ready frozen inventory of organization posts."""

        return [
            post["post_urn"]
            for post in self.find_author_posts(
                organization_urn,
                limit=limit,
                page_size=page_size,
                sort_by=sort_by,
            )
        ]

    def get_post(
        self,
        post_urn: str,
        *,
        organization_urn: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve one exact organization-authored post as a closed projection."""

        post_urn = validate_post_urn(post_urn)
        if organization_urn is not None:
            organization_urn = validate_organization_urn(organization_urn)
        payload = self._request_json(
            "GET",
            f"/posts/{self._encode_urn(post_urn)}",
            operation="get_post",
            params={"viewContext": "READER"},
        )
        try:
            return self._normalize_post(
                payload,
                expected_post_urn=post_urn,
                expected_organization_urn=organization_urn,
            )
        except Exception:
            raise LinkedInUnavailableError("get_post") from None

    def get_exact_post(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.get_post(*args, **kwargs)

    def get_social_metadata(self, post_urn: str) -> dict[str, Any]:
        """Return current aggregate comment/reaction metadata for an exact post."""

        post_urn = validate_post_urn(post_urn)
        payload = self._request_json(
            "GET",
            f"/socialMetadata/{self._encode_urn(post_urn)}",
            operation="get_social_metadata",
        )
        try:
            return self._normalize_social_metadata(payload, post_urn)
        except Exception:
            raise LinkedInUnavailableError("get_social_metadata") from None

    def get_comments(
        self,
        entity_urn: str,
        *,
        include_replies: bool = True,
        limit: int | None = None,
        page_size: int = 100,
        max_reply_depth: int = 1,
    ) -> list[dict[str, Any]]:
        """Retrieve comments and, by default, one level of nested replies."""

        entity_urn = self._validate_comment_entity_urn(entity_urn)
        if not isinstance(include_replies, bool):
            raise LinkedInValidationError("include_replies must be a boolean")
        page_size = _positive_int(page_size, "page_size", maximum=100)
        if limit is not None:
            limit = _positive_int(limit, "limit")
        if isinstance(max_reply_depth, bool):
            raise LinkedInValidationError("max_reply_depth must be between 0 and 10")
        try:
            max_reply_depth = int(max_reply_depth)
        except (TypeError, ValueError, OverflowError):
            raise LinkedInValidationError(
                "max_reply_depth must be between 0 and 10"
            ) from None
        if not 0 <= max_reply_depth <= 10:
            raise LinkedInValidationError("max_reply_depth must be between 0 and 10")

        seen_comments: set[str] = set()
        seen_threads: set[str] = set()
        retrieved = 0

        def remaining() -> int | None:
            return None if limit is None else max(limit - retrieved, 0)

        def collect_thread(thread_urn: str, depth: int) -> list[dict[str, Any]]:
            nonlocal retrieved
            if thread_urn in seen_threads or remaining() == 0:
                return []
            seen_threads.add(thread_urn)
            items = self._get_comment_pages(
                thread_urn,
                limit=remaining(),
                page_size=page_size,
            )
            unique_items: list[dict[str, Any]] = []
            for item in items:
                comment_urn = item["comment_urn"]
                if comment_urn in seen_comments:
                    continue
                if remaining() == 0:
                    break
                seen_comments.add(comment_urn)
                retrieved += 1
                unique_items.append(item)

            if include_replies and depth < max_reply_depth:
                for item in unique_items:
                    if remaining() == 0:
                        break
                    item["replies"] = collect_thread(
                        item["comment_urn"],
                        depth + 1,
                    )
            return unique_items

        return collect_thread(entity_urn, 0)

    def get_replies(
        self,
        comment_urn: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Retrieve replies for one exact composite comment URN."""

        return self.get_comments(validate_comment_urn(comment_urn), **kwargs)

    def collect_post_observation(
        self,
        organization_urn: str,
        post_urn: str,
        *,
        include_comments: bool = True,
        include_replies: bool = True,
        comment_limit: int | None = None,
        comment_page_size: int = 100,
        max_reply_depth: int = 1,
    ) -> dict[str, Any]:
        """Hydrate one exact post with aggregate metadata and safe comments."""

        organization_urn = validate_organization_urn(organization_urn)
        post_urn = validate_post_urn(post_urn)
        if not isinstance(include_comments, bool):
            raise LinkedInValidationError("include_comments must be a boolean")
        post = self.get_post(post_urn, organization_urn=organization_urn)
        post["social_metadata"] = self.get_social_metadata(post_urn)
        comments: list[dict[str, Any]] = []
        if include_comments:
            comments = self.get_comments(
                post_urn,
                include_replies=include_replies,
                limit=comment_limit,
                page_size=comment_page_size,
                max_reply_depth=max_reply_depth,
            )
        post["comments"] = {
            "status": "available" if include_comments else "not_requested",
            "top_level_retrieved": len(comments),
            "total_retrieved": self._count_comment_tree(comments),
            "items": comments,
        }
        return post

    def collect_organization_post(
        self,
        post_urn: str,
        organization_urn: str,
        comments_limit: int | None = None,
        *,
        include_replies: bool = True,
        comment_page_size: int = 100,
        max_reply_depth: int = 1,
    ) -> dict[str, Any]:
        """Return the closed observation accepted by ``checkpoint_post``.

        The post and organization argument order mirrors collection
        orchestration.  The output deliberately flattens the comment tree but
        preserves every parent-comment binding so the retention-aware state
        layer can expire member text independently.
        """

        hydrated = self.collect_post_observation(
            organization_urn,
            post_urn,
            include_comments=True,
            include_replies=include_replies,
            comment_limit=comments_limit,
            comment_page_size=comment_page_size,
            max_reply_depth=max_reply_depth,
        )
        metadata = _mapping(hydrated.get("social_metadata"))
        comments_document = _mapping(hydrated.get("comments"))
        comment_items = comments_document.get("items")
        comments = self._flatten_comments_for_state(comment_items)
        reaction_counts = _mapping(metadata.get("reaction_counts"))
        reaction_total = sum(
            count
            for count in (
                _optional_nonnegative_int(value)
                for value in reaction_counts.values()
            )
            if count is not None
        )
        observed_at = dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0
        ).isoformat()
        content = dict(_mapping(hydrated.get("content")))
        content.update(
            {
                "lifecycle_state": str(hydrated.get("lifecycle_state") or ""),
                "visibility": str(hydrated.get("visibility") or ""),
                "created_at": self._millis_to_iso(hydrated.get("created_at_ms")),
                "last_modified_at": self._millis_to_iso(
                    hydrated.get("last_modified_at_ms")
                ),
                "is_edited_by_author": hydrated.get("is_edited_by_author"),
                "is_reshare_disabled_by_author": hydrated.get(
                    "is_reshare_disabled_by_author"
                ),
                "distribution": dict(_mapping(hydrated.get("distribution"))),
                "reshare": dict(_mapping(hydrated.get("reshare"))),
                "provenance": dict(_mapping(hydrated.get("provenance"))),
            }
        )
        return {
            "post_urn": hydrated["post_urn"],
            "organization_urn": hydrated["author_urn"],
            "canonical_url": self._canonical_post_url(hydrated["post_urn"]),
            "published_at": self._millis_to_iso(hydrated.get("published_at_ms")),
            "commentary": str(hydrated.get("commentary") or "")[:10_000],
            "content": content,
            "metrics": {
                "comments": _optional_nonnegative_int(
                    metadata.get("comment_count")
                ),
                "top_level_comments": _optional_nonnegative_int(
                    metadata.get("top_level_comment_count")
                ),
                "reactions": reaction_total,
                "reaction_counts": dict(reaction_counts),
                "comments_state": str(metadata.get("comments_state") or ""),
            },
            "collection": {
                "authority": "linkedin_official_api",
                "api_version": self.api_version,
                "post_status": "available",
                "social_metadata_status": "available",
                "comments_status": "available",
                "top_level_comments_retrieved": _optional_nonnegative_int(
                    comments_document.get("top_level_retrieved")
                ),
                "total_comments_retrieved": len(comments),
            },
            "comments": comments,
            "observed_at": observed_at,
        }

    def _get_comment_pages(
        self,
        entity_urn: str,
        *,
        limit: int | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        seen_starts: set[int] = set()
        start = 0
        path = f"/socialActions/{self._encode_urn(entity_urn)}/comments"

        for _ in range(_MAX_PAGES):
            if start in seen_starts:
                raise LinkedInUnavailableError("get_comments")
            seen_starts.add(start)
            requested_count = page_size
            if limit is not None:
                requested_count = min(page_size, limit - len(comments))
                if requested_count <= 0:
                    return comments
            payload = self._request_json(
                "GET",
                path,
                operation="get_comments",
                params={"count": requested_count, "start": start},
            )
            elements = payload.get("elements")
            if not isinstance(elements, list):
                raise LinkedInUnavailableError("get_comments")
            try:
                for raw_comment in elements:
                    comments.append(
                        self._normalize_comment(
                            raw_comment,
                            expected_root_id=self._root_numeric_id(entity_urn),
                        )
                    )
                    if limit is not None and len(comments) >= limit:
                        return comments
            except Exception:
                raise LinkedInUnavailableError("get_comments") from None

            next_start = self._next_start(
                payload,
                current_start=start,
                returned_count=len(elements),
                requested_count=requested_count,
                expected_path=path,
            )
            if next_start is None:
                return comments
            start = next_start
        raise LinkedInUnavailableError("get_comments")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        url = f"{API_BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Linkedin-Version": self.api_version,
            "X-Restli-Protocol-Version": RESTLI_PROTOCOL_VERSION,
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        try:
            requester = getattr(self._transport, "request", None)
            if callable(requester):
                raw_response = requester(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout,
                )
            else:
                callable_transport = cast(Callable[..., Any], self._transport)
                raw_response = callable_transport(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout,
                )
        except LinkedInAPIError:
            raise LinkedInUnavailableError(operation) from None
        except Exception:
            raise LinkedInUnavailableError(operation) from None

        try:
            response = self._coerce_transport_response(raw_response)
        except Exception:
            raise LinkedInUnavailableError(operation) from None
        status = response.status_code
        if status in {401, 403}:
            raise LinkedInForbiddenError(operation, status_code=status)
        if status == 429:
            raise LinkedInRateLimitedError(
                operation,
                status_code=status,
                retry_after_seconds=self._retry_after(response.headers),
            )
        if not 200 <= status < 300:
            raise LinkedInUnavailableError(operation, status_code=status)
        if not isinstance(response.payload, Mapping):
            raise LinkedInUnavailableError(operation, status_code=status)
        return response.payload

    @staticmethod
    def _coerce_transport_response(value: Any) -> _TransportResponse:
        if isinstance(value, _TransportResponse):
            return value
        if isinstance(value, tuple) and len(value) in {2, 3}:
            status, tuple_payload = value[0], value[1]
            headers = value[2] if len(value) == 3 else {}
            return _TransportResponse(int(status), tuple_payload, _mapping(headers))
        if isinstance(value, Mapping):
            if "status_code" in value and (
                "json" in value or "payload" in value or "body" in value
            ):
                payload = value.get("json", value.get("payload", value.get("body")))
                return _TransportResponse(
                    int(value["status_code"]),
                    payload,
                    _mapping(value.get("headers")),
                )
            return _TransportResponse(200, value, {})

        status = getattr(value, "status_code", None)
        if status is None:
            status = getattr(value, "status", None)
        if status is None:
            raise TypeError("transport response has no status")
        status = int(status)
        response_payload: Any = None
        if 200 <= status < 300:
            json_method = getattr(value, "json", None)
            if callable(json_method):
                response_payload = json_method()
            elif hasattr(value, "payload"):
                response_payload = value.payload
            elif hasattr(value, "body"):
                response_payload = value.body
        return _TransportResponse(
            status,
            response_payload,
            _mapping(getattr(value, "headers", None)),
        )

    @staticmethod
    def _retry_after(headers: Mapping[str, Any]) -> int | None:
        for key, value in headers.items():
            if str(key).casefold() != "retry-after":
                continue
            return _optional_nonnegative_int(value)
        return None

    @staticmethod
    def _encode_urn(urn: str) -> str:
        return urllib.parse.quote(urn, safe="")

    def _normalize_post(
        self,
        value: Any,
        *,
        expected_post_urn: str | None = None,
        expected_organization_urn: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise _PayloadError("post must be an object")
        try:
            post_urn = validate_post_urn(value.get("id"))
            author_urn = validate_organization_urn(value.get("author"))
        except LinkedInValidationError as exc:
            raise _PayloadError("post identifier or author is invalid") from exc
        if expected_post_urn is not None and post_urn != expected_post_urn:
            raise _PayloadError("post binding mismatch")
        if (
            expected_organization_urn is not None
            and author_urn != expected_organization_urn
        ):
            raise _PayloadError("organization binding mismatch")

        distribution = _mapping(value.get("distribution"))
        lifecycle_info = _mapping(value.get("lifecycleStateInfo"))
        content = self._normalize_content(value.get("content"))
        reshare_context = _mapping(value.get("reshareContext"))
        parent = self._safe_post_urn(reshare_context.get("parent"))
        root = self._safe_post_urn(reshare_context.get("root"))

        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "platform": "linkedin",
            "post_urn": post_urn,
            "author_urn": author_urn,
            "author_type": "organization",
            "commentary": self._safe_text(value.get("commentary"), 20_000),
            "lifecycle_state": self._safe_enum(value.get("lifecycleState")),
            "visibility": self._safe_enum(value.get("visibility")),
            "created_at_ms": _optional_nonnegative_int(value.get("createdAt")),
            "published_at_ms": _optional_nonnegative_int(value.get("publishedAt")),
            "last_modified_at_ms": _optional_nonnegative_int(
                value.get("lastModifiedAt")
            ),
            "is_edited_by_author": _optional_bool(
                lifecycle_info.get("isEditedByAuthor")
            ),
            "is_reshare_disabled_by_author": _optional_bool(
                value.get("isReshareDisabledByAuthor")
            ),
            "distribution": {
                "feed_distribution": self._safe_enum(
                    distribution.get("feedDistribution")
                ),
                "third_party_channels": self._safe_enum_list(
                    distribution.get("thirdPartyDistributionChannels")
                ),
            },
            "content": content,
            "reshare": {"parent_post_urn": parent, "root_post_urn": root},
            "provenance": {
                "authority": "linkedin_official_api",
                "api_version": self.api_version,
                "resource": "posts",
            },
        }

    def _normalize_content(self, value: Any) -> dict[str, Any]:
        content = _mapping(value)
        content_type = "none" if not content else "unknown"
        selected: Mapping[str, Any] = {}
        for key in _CONTENT_KEYS:
            if key in content:
                content_type = self._camel_to_snake(key)
                selected = _mapping(content.get(key))
                break
        asset_urns: list[str] = []
        self._collect_asset_urns(content, asset_urns, depth=0)
        return {
            "type": content_type,
            "title": self._safe_text(selected.get("title"), 500),
            "description": self._safe_text(selected.get("description"), 2_000),
            "asset_urns": asset_urns,
        }

    def _collect_asset_urns(
        self,
        value: Any,
        output: list[str],
        *,
        depth: int,
    ) -> None:
        if depth > 6 or len(output) >= 50:
            return
        if isinstance(value, str):
            if _ASSET_URN_RE.fullmatch(value) and value not in output:
                output.append(value)
            return
        if isinstance(value, Mapping):
            for nested in value.values():
                self._collect_asset_urns(nested, output, depth=depth + 1)
            return
        for nested in _sequence(value):
            self._collect_asset_urns(nested, output, depth=depth + 1)

    def _normalize_social_metadata(
        self,
        value: Mapping[str, Any],
        requested_post_urn: str,
    ) -> dict[str, Any]:
        comment_summary = _mapping(value.get("commentSummary"))
        reaction_summaries = value.get("reactionSummaries")
        if reaction_summaries is None:
            reaction_summaries = {}
        if not isinstance(reaction_summaries, Mapping):
            raise _PayloadError("reaction summaries must be an object")
        reaction_counts: dict[str, int] = {}
        for raw_key, raw_summary in reaction_summaries.items():
            key = self._safe_enum(raw_key)
            summary = _mapping(raw_summary)
            declared_type = self._safe_enum(summary.get("reactionType"))
            reaction_type = declared_type or key
            count = _optional_nonnegative_int(summary.get("count"))
            if reaction_type in _REACTION_TYPES and count is not None:
                reaction_counts[reaction_type] = count
        state = self._safe_enum(value.get("commentsState"))
        return {
            "entity_urn": requested_post_urn,
            "comments_state": state if state in _COMMENTS_STATES else "",
            "comment_count": _optional_nonnegative_int(comment_summary.get("count")),
            "top_level_comment_count": _optional_nonnegative_int(
                comment_summary.get("topLevelCount")
            ),
            "reaction_counts": dict(sorted(reaction_counts.items())),
            "provenance": {
                "authority": "linkedin_official_api",
                "api_version": self.api_version,
                "resource": "socialMetadata",
            },
        }

    def _normalize_comment(
        self,
        value: Any,
        *,
        expected_root_id: str,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise _PayloadError("comment must be an object")
        try:
            comment_urn = validate_comment_urn(value.get("commentUrn"))
        except LinkedInValidationError as exc:
            raise _PayloadError("comment URN is invalid") from exc
        raw_actor = value.get("actor")
        actor_urn = raw_actor if isinstance(raw_actor, str) else ""
        if not _ACTOR_URN_RE.fullmatch(actor_urn):
            raise _PayloadError("comment actor is invalid")
        actor_type = "person" if actor_urn.startswith("urn:li:person:") else "organization"
        raw_object = value.get("object")
        object_urn = raw_object if isinstance(raw_object, str) else ""
        if not _SOCIAL_OBJECT_URN_RE.fullmatch(object_urn):
            raise _PayloadError("comment object is invalid")
        if self._root_numeric_id(comment_urn) != expected_root_id:
            raise _PayloadError("comment thread binding mismatch")
        if self._root_numeric_id(object_urn) != expected_root_id:
            raise _PayloadError("comment object binding mismatch")
        parent = value.get("parentComment")
        if parent in (None, ""):
            parent_urn = ""
        else:
            try:
                parent_urn = validate_comment_urn(parent)
            except LinkedInValidationError as exc:
                raise _PayloadError("parent comment URN is invalid") from exc

        urn_id = comment_urn.rsplit(",", 1)[-1][:-1]
        raw_id = value.get("id")
        comment_id = str(raw_id) if isinstance(raw_id, (str, int)) else urn_id
        if not comment_id.isdigit() or comment_id != urn_id:
            raise _PayloadError("comment identifier binding mismatch")
        created = _mapping(value.get("created"))
        modified = _mapping(value.get("lastModified"))
        likes = _mapping(value.get("likesSummary"))
        like_count = _optional_nonnegative_int(likes.get("aggregatedTotalLikes"))
        if like_count is None:
            like_count = _optional_nonnegative_int(likes.get("totalLikes"))
        message = _mapping(value.get("message"))
        return {
            "comment_urn": comment_urn,
            "comment_id": comment_id,
            "actor_urn": actor_urn,
            "actor_type": actor_type,
            "object_urn": object_urn,
            "parent_comment_urn": parent_urn,
            "message": self._safe_text(message.get("text"), 20_000),
            "created_at_ms": _optional_nonnegative_int(created.get("time")),
            "last_modified_at_ms": _optional_nonnegative_int(modified.get("time")),
            "like_count": like_count,
            "liked_by_current_user": _optional_bool(likes.get("likedByCurrentUser")),
            "replies": [],
            "provenance": {
                "authority": "linkedin_official_api",
                "api_version": self.api_version,
                "resource": "socialActions/comments",
            },
        }

    def _safe_text(self, value: Any, limit: int) -> str:
        if value is None or isinstance(value, (Mapping, list, tuple, set, bytes)):
            return ""
        text = _CONTROL_RE.sub("", str(value))
        text = text.replace(self._access_token, "[redacted]")
        text = _CREDENTIAL_TEXT_RE.sub("[redacted]", text)
        return text.strip()[:limit]

    def _safe_enum(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        normalized = value.strip().upper()
        return normalized if _SAFE_ENUM_RE.fullmatch(normalized) else ""

    def _safe_enum_list(self, value: Any) -> list[str]:
        output: list[str] = []
        for item in _sequence(value)[:50]:
            normalized = self._safe_enum(item)
            if normalized and normalized not in output:
                output.append(normalized)
        return output

    @staticmethod
    def _safe_post_urn(value: Any) -> str:
        return value if isinstance(value, str) and _POST_URN_RE.fullmatch(value) else ""

    @staticmethod
    def _camel_to_snake(value: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()

    @staticmethod
    def _validate_comment_entity_urn(value: Any) -> str:
        if isinstance(value, str) and (
            _POST_URN_RE.fullmatch(value) or _COMMENT_URN_RE.fullmatch(value)
        ):
            return value
        raise LinkedInValidationError(
            "comment entity must be a share, ugcPost, or composite comment URN"
        )

    @staticmethod
    def _root_numeric_id(urn: str) -> str:
        if _COMMENT_URN_RE.fullmatch(urn):
            return urn.split("urn:li:activity:", 1)[1].split(",", 1)[0]
        if _POST_URN_RE.fullmatch(urn) or _SOCIAL_OBJECT_URN_RE.fullmatch(urn):
            return urn.rsplit(":", 1)[-1]
        raise _PayloadError("entity has no valid root identifier")

    @staticmethod
    def _count_comment_tree(comments: Sequence[Mapping[str, Any]]) -> int:
        total = 0
        stack = list(comments)
        while stack:
            comment = stack.pop()
            total += 1
            replies = comment.get("replies")
            if isinstance(replies, list):
                stack.extend(item for item in replies if isinstance(item, Mapping))
        return total

    @classmethod
    def _flatten_comments_for_state(cls, value: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()

        def walk(items: Any, inferred_parent: str = "") -> None:
            for raw_comment in _sequence(items):
                if not isinstance(raw_comment, Mapping):
                    continue
                comment_urn = str(raw_comment.get("comment_urn") or "")
                if not _COMMENT_URN_RE.fullmatch(comment_urn) or comment_urn in seen:
                    continue
                seen.add(comment_urn)
                parent = str(raw_comment.get("parent_comment_urn") or inferred_parent)
                if parent and not _COMMENT_URN_RE.fullmatch(parent):
                    parent = ""
                output.append(
                    {
                        "comment_urn": comment_urn,
                        "parent_comment_urn": parent,
                        "actor_urn": str(raw_comment.get("actor_urn") or "")[:512],
                        "message": str(raw_comment.get("message") or "")[:5_000],
                        "likes": _optional_nonnegative_int(
                            raw_comment.get("like_count")
                        ),
                        "created_at": cls._millis_to_iso(
                            raw_comment.get("created_at_ms")
                        ),
                    }
                )
                walk(raw_comment.get("replies"), comment_urn)

        walk(value)
        return output

    @staticmethod
    def _canonical_post_url(post_urn: str) -> str:
        validate_post_urn(post_urn)
        return f"https://www.linkedin.com/feed/update/{post_urn}/"

    @staticmethod
    def _millis_to_iso(value: Any) -> str:
        milliseconds = _optional_nonnegative_int(value)
        if milliseconds is None:
            return ""
        try:
            return dt.datetime.fromtimestamp(
                milliseconds / 1_000,
                tz=dt.timezone.utc,
            ).replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""

    @staticmethod
    def _next_start(
        payload: Mapping[str, Any],
        *,
        current_start: int,
        returned_count: int,
        requested_count: int,
        expected_path: str,
    ) -> int | None:
        paging = payload.get("paging")
        if not isinstance(paging, Mapping):
            return None
        links = paging.get("links")
        next_link: Mapping[str, Any] | None = None
        if isinstance(links, list):
            for candidate in links:
                if (
                    isinstance(candidate, Mapping)
                    and str(candidate.get("rel") or "").casefold() == "next"
                ):
                    next_link = candidate
                    break
        if next_link is not None:
            href = next_link.get("href")
            if isinstance(href, str) and href:
                parsed = urllib.parse.urlsplit(href)
                if parsed.scheme and parsed.scheme != "https":
                    raise LinkedInUnavailableError("pagination")
                if parsed.netloc and parsed.netloc.casefold() != "api.linkedin.com":
                    raise LinkedInUnavailableError("pagination")
                allowed_paths = {
                    urllib.parse.unquote(expected_path),
                    urllib.parse.unquote(f"/rest{expected_path}"),
                }
                if (
                    parsed.path
                    and urllib.parse.unquote(parsed.path) not in allowed_paths
                ):
                    raise LinkedInUnavailableError("pagination")
                query = urllib.parse.parse_qs(parsed.query)
                candidate_start = query.get("start", [None])[0]
                next_start = _optional_nonnegative_int(candidate_start)
                if next_start is not None:
                    if next_start <= current_start:
                        raise LinkedInUnavailableError("pagination")
                    return next_start
            step = _optional_nonnegative_int(paging.get("count"))
            step = step or requested_count or returned_count
            if step <= 0:
                raise LinkedInUnavailableError("pagination")
            return current_start + step

        total = _optional_nonnegative_int(paging.get("total"))
        page_start = _optional_nonnegative_int(paging.get("start"))
        page_count = _optional_nonnegative_int(paging.get("count"))
        if total is not None and page_start is not None and page_count is not None:
            candidate = page_start + page_count
            if candidate < total:
                if candidate <= current_start:
                    raise LinkedInUnavailableError("pagination")
                return candidate
        return None


__all__ = [
    "API_BASE_URL",
    "DEFAULT_API_VERSION",
    "OBSERVATION_SCHEMA_VERSION",
    "LinkedInAPIClient",
    "LinkedInAPIError",
    "LinkedInForbiddenError",
    "LinkedInRateLimitedError",
    "LinkedInUnavailableError",
    "LinkedInValidationError",
    "validate_comment_urn",
    "validate_organization_urn",
    "validate_post_urn",
]
