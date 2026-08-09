"""Official X API v2 adapter for post discovery and conversation replies."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from .base_scraper import BaseScraper


class XAPIError(RuntimeError):
    """An actionable X API error with credentials removed from the message."""


class XScraper(BaseScraper):
    """Collect public X posts and replies through the official X API v2.

    The adapter deliberately does not fall back to DOM extraction. A browser
    GraphQL transport can be added later as a separate, explicitly selected
    method without weakening provenance for official API runs.
    """

    API_BASE = "https://api.x.com/2"
    STATUS_RE = re.compile(r"/(?:i/web/)?status(?:es)?/(\d+)", re.I)
    HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
    RESERVED_PATHS = {
        "compose",
        "explore",
        "home",
        "i",
        "intent",
        "login",
        "messages",
        "notifications",
        "search",
        "settings",
        "share",
        "signup",
    }
    POST_FIELDS = ",".join(
        (
            "attachments",
            "author_id",
            "conversation_id",
            "created_at",
            "entities",
            "geo",
            "id",
            "in_reply_to_user_id",
            "lang",
            "note_tweet",
            "possibly_sensitive",
            "public_metrics",
            "referenced_tweets",
            "reply_settings",
            "text",
            "withheld",
        )
    )
    EXPANSIONS = ",".join(
        (
            "author_id",
            "attachments.media_keys",
            "geo.place_id",
        )
    )
    USER_FIELDS = ",".join(
        (
            "created_at",
            "description",
            "id",
            "location",
            "name",
            "profile_image_url",
            "protected",
            "public_metrics",
            "url",
            "username",
            "verified",
            "verified_type",
        )
    )
    MEDIA_FIELDS = ",".join(
        (
            "alt_text",
            "duration_ms",
            "height",
            "media_key",
            "preview_image_url",
            "public_metrics",
            "type",
            "url",
            "variants",
            "width",
        )
    )
    PLACE_FIELDS = "contained_within,country,country_code,full_name,geo,id,name,place_type"

    def __init__(
        self,
        *,
        bearer_token: str = "",
        session: Optional[requests.Session] = None,
        search_mode: str = "",
    ) -> None:
        super().__init__("x")
        self.bearer_token = bearer_token.strip() or self._token_from_environment()
        self.session = session or requests.Session()
        self.search_mode = self._normalize_search_mode(
            search_mode or os.environ.get("X_SEARCH_MODE", "recent")
        )
        self.post_reads = 0
        self.user_reads = 0
        self.media_reads = 0
        self.request_count = 0
        self.last_rate_limit: Dict[str, Any] = {}

    @staticmethod
    def _env_int(name: str, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
        try:
            return max(minimum, float(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _normalize_search_mode(cls, value: str) -> str:
        normalized = str(value or "recent").strip().lower().replace("_", "-")
        if normalized in {"all", "archive", "full", "full-archive"}:
            return "all"
        if normalized == "recent":
            return "recent"
        raise ValueError("X search mode must be 'recent' or 'all'")

    @staticmethod
    def _token_from_environment() -> str:
        token = (
            os.environ.get("X_BEARER_TOKEN", "").strip()
            or os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
        )
        if token:
            return token

        token_file = (
            os.environ.get("X_BEARER_TOKEN_FILE", "").strip()
            or os.environ.get("TWITTER_BEARER_TOKEN_FILE", "").strip()
        )
        if not token_file:
            return ""
        path = Path(token_file)
        if not path.exists():
            raise XAPIError(f"X bearer-token file does not exist: {path}")
        return path.read_text(encoding="utf-8-sig").strip()

    def _require_token(self) -> None:
        if not self.bearer_token:
            raise XAPIError(
                "X API credentials are missing. Set X_BEARER_TOKEN or "
                "X_BEARER_TOKEN_FILE to an official X API v2 bearer token."
            )

    @property
    def search_endpoint(self) -> str:
        suffix = "all" if self.search_mode == "all" else "recent"
        return f"{self.API_BASE}/tweets/search/{suffix}"

    def usage_summary(self) -> Dict[str, Any]:
        post_price = self._env_float("X_POST_READ_USD", 0.005)
        user_price = self._env_float("X_USER_READ_USD", 0.010)
        media_price = self._env_float("X_MEDIA_READ_USD", 0.005)
        return {
            "request_count": self.request_count,
            "post_reads": self.post_reads,
            "user_reads": self.user_reads,
            "media_reads": self.media_reads,
            "estimated_cost_usd": round(
                (self.post_reads * post_price)
                + (self.user_reads * user_price)
                + (self.media_reads * media_price),
                4,
            ),
            "pricing_assumption_usd": {
                "post_read": post_price,
                "user_read": user_price,
                "media_read": media_price,
            },
            "last_rate_limit": dict(self.last_rate_limit),
        }

    @staticmethod
    def extract_post_id(value: str) -> str:
        match = XScraper.STATUS_RE.search(str(value or ""))
        if match:
            return match.group(1)
        text = str(value or "").strip()
        return text if text.isdigit() else ""

    @classmethod
    def extract_handle(cls, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("@") and cls.HANDLE_RE.fullmatch(text[1:]):
            return text[1:]
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return ""
        handle = parts[0]
        if handle.lower() in cls.RESERVED_PATHS or not cls.HANDLE_RE.fullmatch(handle):
            return ""
        return handle

    @classmethod
    def is_x_url(cls, value: str) -> bool:
        host = (urlparse(str(value or "")).hostname or "").lower()
        return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}

    @staticmethod
    def _error_detail(response: Any) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                parts = []
                for item in errors[:3]:
                    if not isinstance(item, dict):
                        continue
                    detail = item.get("detail") or item.get("message") or item.get("title")
                    if detail:
                        parts.append(str(detail))
                if parts:
                    return "; ".join(parts)
            detail = payload.get("detail") or payload.get("title")
            if detail:
                return str(detail)
        text = str(getattr(response, "text", "") or "").strip()
        return re.sub(r"\s+", " ", text)[:500]

    def _update_rate_limit(self, response: Any) -> None:
        headers = getattr(response, "headers", {}) or {}
        self.last_rate_limit = {
            "limit": headers.get("x-rate-limit-limit", ""),
            "remaining": headers.get("x-rate-limit-remaining", ""),
            "reset": headers.get("x-rate-limit-reset", ""),
        }

    def _read_budget_remaining(self) -> Optional[int]:
        limit = self._env_int("X_API_READ_LIMIT", 0)
        if limit <= 0:
            return None
        return max(0, limit - self.post_reads)

    def _request_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        resource_type: str = "posts",
    ) -> Dict[str, Any]:
        self._require_token()
        if resource_type == "posts" and self._read_budget_remaining() == 0:
            raise XAPIError("X API post-read budget reached before the next request")

        retries = self._env_int("X_API_RETRIES", 2)
        timeout = self._env_float("X_API_TIMEOUT_SECONDS", 30.0, 1.0)
        max_rate_wait = self._env_float("X_MAX_RATE_LIMIT_WAIT_SECONDS", 60.0)
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Accept": "application/json",
            "User-Agent": "KitaCo-SocialListening/1.0",
        }

        for attempt in range(retries + 1):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=timeout)
            except requests.RequestException as exc:
                if attempt >= retries:
                    raise XAPIError(f"X API network request failed: {exc}") from exc
                time.sleep(min(2 ** attempt, 8))
                continue

            self.request_count += 1
            self._update_rate_limit(response)
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 429 and attempt < retries:
                reset_raw = (getattr(response, "headers", {}) or {}).get("x-rate-limit-reset", "")
                try:
                    wait_seconds = max(1.0, float(reset_raw) - time.time() + 1.0)
                except (TypeError, ValueError):
                    wait_seconds = min(2 ** attempt, 8)
                if wait_seconds <= max_rate_wait:
                    time.sleep(wait_seconds)
                    continue
            if status >= 500 and attempt < retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            if status < 200 or status >= 300:
                detail = self._error_detail(response)
                if status == 401:
                    hint = " Check that the bearer token is valid for this X developer app."
                elif status == 403:
                    hint = " The selected endpoint or query may not be enabled for this app."
                elif status == 429:
                    hint = " The endpoint rate limit is exhausted; retry after the reset time."
                else:
                    hint = ""
                raise XAPIError(f"X API HTTP {status}: {detail or 'request failed'}.{hint}")

            try:
                payload = response.json()
            except Exception as exc:
                raise XAPIError("X API returned a non-JSON response") from exc
            if not isinstance(payload, dict):
                raise XAPIError("X API returned an unexpected JSON payload")

            data = payload.get("data")
            resource_count = len(data) if isinstance(data, list) else int(isinstance(data, dict))
            if resource_type == "posts":
                self.post_reads += resource_count
            elif resource_type == "users":
                self.user_reads += resource_count
            includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
            included_users = includes.get("users") if isinstance(includes.get("users"), list) else []
            included_media = includes.get("media") if isinstance(includes.get("media"), list) else []
            self.user_reads += len(included_users)
            self.media_reads += len(included_media)
            return payload

        raise XAPIError("X API request failed after retries")

    @classmethod
    def _request_fields(cls) -> Dict[str, str]:
        return {
            "tweet.fields": cls.POST_FIELDS,
            "expansions": cls.EXPANSIONS,
            "user.fields": cls.USER_FIELDS,
            "media.fields": cls.MEDIA_FIELDS,
            "place.fields": cls.PLACE_FIELDS,
        }

    @staticmethod
    def _include_maps(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
        maps: Dict[str, Dict[str, Any]] = {
            "users": {},
            "media": {},
            "places": {},
            "tweets": {},
        }
        for user in includes.get("users", []) if isinstance(includes.get("users"), list) else []:
            if isinstance(user, dict) and user.get("id"):
                maps["users"][str(user["id"])] = user
        for media in includes.get("media", []) if isinstance(includes.get("media"), list) else []:
            if isinstance(media, dict) and media.get("media_key"):
                maps["media"][str(media["media_key"])] = media
        for place in includes.get("places", []) if isinstance(includes.get("places"), list) else []:
            if isinstance(place, dict) and place.get("id"):
                maps["places"][str(place["id"])] = place
        for post in includes.get("tweets", []) if isinstance(includes.get("tweets"), list) else []:
            if isinstance(post, dict) and post.get("id"):
                maps["tweets"][str(post["id"])] = post
        return maps

    @staticmethod
    def _post_text(post: Dict[str, Any]) -> str:
        note = post.get("note_tweet") if isinstance(post.get("note_tweet"), dict) else {}
        return str(note.get("text") or post.get("text") or "").strip()

    @staticmethod
    def _post_parent_id(post: Dict[str, Any]) -> str:
        references = post.get("referenced_tweets")
        if not isinstance(references, list):
            return ""
        for reference in references:
            if isinstance(reference, dict) and reference.get("type") == "replied_to":
                return str(reference.get("id") or "")
        return ""

    @staticmethod
    def _content_type(media: List[Dict[str, Any]]) -> str:
        media_types = {str(item.get("type") or "").lower() for item in media}
        if "video" in media_types or "animated_gif" in media_types:
            return "x_video_post"
        if "photo" in media_types:
            return "x_image_post"
        return "x_text_post"

    def _post_to_candidate(
        self,
        post: Dict[str, Any],
        includes: Dict[str, Dict[str, Any]],
        *,
        discovery_method: str,
    ) -> Dict[str, Any]:
        post_id = str(post.get("id") or "")
        author_id = str(post.get("author_id") or "")
        author = dict(includes["users"].get(author_id) or {})
        username = str(author.get("username") or "")
        media_keys = (
            post.get("attachments", {}).get("media_keys", [])
            if isinstance(post.get("attachments"), dict)
            else []
        )
        media = [
            dict(includes["media"][str(key)])
            for key in media_keys
            if str(key) in includes["media"]
        ]
        geo = post.get("geo") if isinstance(post.get("geo"), dict) else {}
        place = dict(includes["places"].get(str(geo.get("place_id") or "")) or {})
        metrics = post.get("public_metrics") if isinstance(post.get("public_metrics"), dict) else {}
        author_metrics = author.get("public_metrics") if isinstance(author.get("public_metrics"), dict) else {}
        text = self._post_text(post)
        canonical_username = username or "i"
        return {
            "platform": "x",
            "video_id": post_id,
            "url": f"https://x.com/{canonical_username}/status/{post_id}",
            "title": text,
            "caption": text,
            "description": text,
            "username": username or str(author.get("name") or "X User"),
            "creator_id": author_id,
            "creator_display_name": str(author.get("name") or ""),
            "creator_verified": bool(author.get("verified")),
            "creator_verified_type": str(author.get("verified_type") or ""),
            "creator_profile_location": str(author.get("location") or ""),
            "follower_count": author_metrics.get("followers_count"),
            "published_at": str(post.get("created_at") or ""),
            "content_type": self._content_type(media),
            "content_language": str(post.get("lang") or ""),
            "conversation_id": str(post.get("conversation_id") or post_id),
            "in_reply_to_user_id": str(post.get("in_reply_to_user_id") or ""),
            "view_count": metrics.get("impression_count"),
            "like_count": metrics.get("like_count"),
            "share_count": metrics.get("retweet_count"),
            "save_count": metrics.get("bookmark_count"),
            "quote_count": metrics.get("quote_count"),
            "retweet_count": metrics.get("retweet_count"),
            "reported_comment_count": metrics.get("reply_count"),
            "reply_count": metrics.get("reply_count"),
            "metric_availability": {
                "views": "available" if "impression_count" in metrics else "not_publicly_exposed",
                "likes": "available" if "like_count" in metrics else "missing_from_public_response",
                "reported_comments": "available" if "reply_count" in metrics else "missing_from_public_response",
                "shares": "available" if "retweet_count" in metrics else "missing_from_public_response",
                "saves": "available" if "bookmark_count" in metrics else "not_publicly_exposed",
                "followers": "available" if "followers_count" in author_metrics else "missing_from_public_response",
            },
            "possibly_sensitive": bool(post.get("possibly_sensitive")),
            "reply_settings": str(post.get("reply_settings") or ""),
            "geo": geo,
            "place": place,
            "media": media,
            "entities": post.get("entities") if isinstance(post.get("entities"), dict) else {},
            "referenced_tweets": post.get("referenced_tweets") if isinstance(post.get("referenced_tweets"), list) else [],
            "withheld": post.get("withheld") if isinstance(post.get("withheld"), dict) else {},
            "discovery_method": discovery_method,
            "metadata_method": "x_api_v2",
            "x_api_post": dict(post),
            "x_api_author": author,
        }

    def _post_to_comment(
        self,
        post: Dict[str, Any],
        includes: Dict[str, Dict[str, Any]],
        root_post_id: str,
    ) -> Dict[str, Any]:
        comment_id = str(post.get("id") or "")
        author_id = str(post.get("author_id") or "")
        author = dict(includes["users"].get(author_id) or {})
        metrics = post.get("public_metrics") if isinstance(post.get("public_metrics"), dict) else {}
        username = str(author.get("username") or "")
        parent_post_id = self._post_parent_id(post)
        return {
            "platform": "x",
            "comment_id": comment_id,
            "id": comment_id,
            "text": self._post_text(post),
            "author": username or str(author.get("name") or "X User"),
            "author_id": author_id,
            "author_display_name": str(author.get("name") or ""),
            "author_verified": bool(author.get("verified")),
            "author_profile_location": str(author.get("location") or ""),
            "likes": metrics.get("like_count", 0),
            "reply_count": metrics.get("reply_count", 0),
            "retweet_count": metrics.get("retweet_count", 0),
            "quote_count": metrics.get("quote_count", 0),
            "created_at": str(post.get("created_at") or ""),
            "time": str(post.get("created_at") or ""),
            "lang": str(post.get("lang") or ""),
            "is_reply": True,
            "reply_to_post_id": parent_post_id,
            "parent_comment_id": "" if parent_post_id == root_post_id else parent_post_id,
            "url": f"https://x.com/{username or 'i'}/status/{comment_id}",
            "public_metrics": dict(metrics),
            "x_api_post": dict(post),
            "x_api_author": author,
        }

    @staticmethod
    def _normalize_api_time(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
        return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @classmethod
    def _date_bounds_from_environment(cls) -> tuple[str, str]:
        raw = os.environ.get("SCRAPER_DATE_WINDOWS_JSON", "").strip()
        if not raw:
            return "", ""
        try:
            windows = json.loads(raw)
        except (TypeError, ValueError):
            return "", ""
        starts: List[str] = []
        ends: List[str] = []
        for window in windows if isinstance(windows, list) else []:
            if not isinstance(window, dict):
                continue
            start = cls._normalize_api_time(str(window.get("start") or ""))
            end = cls._normalize_api_time(str(window.get("end") or ""))
            if start:
                starts.append(start)
            if end:
                ends.append(end)
        return (min(starts) if starts else "", max(ends) if ends else "")

    @staticmethod
    def _parse_api_time(value: str) -> Optional[dt.datetime]:
        if not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    @staticmethod
    def _format_api_time(value: Optional[dt.datetime]) -> str:
        if value is None:
            return ""
        return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _search_date_bounds(self) -> tuple[str, str]:
        start_raw, end_raw = self._date_bounds_from_environment()
        start = self._parse_api_time(start_raw)
        end = self._parse_api_time(end_raw)
        now = dt.datetime.now(dt.timezone.utc)
        newest_allowed = now - dt.timedelta(seconds=10)
        if end and end > newest_allowed:
            end = newest_allowed

        if self.search_mode == "recent":
            oldest_allowed = now - dt.timedelta(days=7)
            if end and end <= oldest_allowed:
                raise XAPIError(
                    "The requested date window is older than X recent search. "
                    "Use --x-search-mode all with full-archive access."
                )
            if start and start < oldest_allowed:
                start = oldest_allowed

        if start and end and start >= end:
            raise XAPIError("The requested X API date window has no searchable time range")
        return self._format_api_time(start), self._format_api_time(end)

    def _prepare_discovery_query(self, target: str) -> str:
        text = str(target or "").strip()
        if self.is_x_url(text):
            parsed = urlparse(text)
            if (parsed.path or "").rstrip("/").lower() == "/search":
                query = parse_qs(parsed.query).get("q", [""])[0]
                if query:
                    text = query
            handle = self.extract_handle(text)
            if handle and not self.extract_post_id(text):
                text = f"from:{handle}"
        elif text.startswith("@") and self.extract_handle(text):
            text = f"from:{self.extract_handle(text)}"

        lowered = text.casefold()
        include_replies = os.environ.get("X_INCLUDE_REPLIES_IN_DISCOVERY", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        include_retweets = os.environ.get("X_INCLUDE_RETWEETS_IN_DISCOVERY", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        operators = [text]
        if not include_replies and "is:reply" not in lowered:
            operators.append("-is:reply")
        if not include_retweets and "is:retweet" not in lowered:
            operators.append("-is:retweet")
        return " ".join(part for part in operators if part).strip()

    def _page_size(self, remaining: Optional[int]) -> int:
        budget = self._read_budget_remaining()
        values = [100]
        if remaining is not None:
            values.append(max(1, remaining))
        if budget is not None:
            values.append(max(1, budget))
        # X requires at least 10 results per search request.
        return max(10, min(values))

    def _search_posts_sync(
        self,
        query: str,
        *,
        max_posts: int = 0,
        since_id: str = "",
        apply_date_bounds: bool = True,
        discovery_method: str = "",
    ) -> tuple[List[Dict[str, Any]], bool]:
        params: Dict[str, Any] = {
            **self._request_fields(),
            "query": query,
            "sort_order": "recency",
        }
        query_limit = 1024 if self.search_mode == "all" else 512
        if len(query) > query_limit:
            raise XAPIError(
                f"X {self.search_mode} search query is {len(query)} characters; "
                f"the supported limit is {query_limit}"
            )
        if since_id:
            params["since_id"] = since_id
        if apply_date_bounds:
            start_time, end_time = self._search_date_bounds()
            if start_time:
                params["start_time"] = start_time
            if end_time:
                params["end_time"] = end_time

        found: List[Dict[str, Any]] = []
        seen: set[str] = set()
        next_token = ""
        exhausted = False
        method = discovery_method or f"x_api_v2_{self.search_mode}_search"

        while True:
            remaining = max_posts - len(found) if max_posts > 0 else None
            if remaining is not None and remaining <= 0:
                break
            if self._read_budget_remaining() == 0:
                break
            page_params = dict(params)
            page_params["max_results"] = self._page_size(remaining)
            if next_token:
                page_params["next_token"] = next_token

            payload = self._request_json(self.search_endpoint, params=page_params, resource_type="posts")
            includes = self._include_maps(payload)
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            for post in data:
                if not isinstance(post, dict):
                    continue
                post_id = str(post.get("id") or "")
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                found.append(
                    self._post_to_candidate(post, includes, discovery_method=method)
                )
                if max_posts > 0 and len(found) >= max_posts:
                    break

            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            next_token = str(meta.get("next_token") or "")
            if not next_token:
                exhausted = True
                break
            if max_posts > 0 and len(found) >= max_posts:
                break
            if not data:
                break

        return found[:max_posts] if max_posts > 0 else found, exhausted

    def _lookup_post_sync(self, post_id: str) -> Dict[str, Any]:
        payload = self._request_json(
            f"{self.API_BASE}/tweets/{post_id}",
            params=self._request_fields(),
            resource_type="posts",
        )
        post = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not post:
            return {}
        return self._post_to_candidate(
            post,
            self._include_maps(payload),
            discovery_method="x_api_v2_post_lookup",
        )

    async def search(
        self,
        query: str,
        max_videos: int = 10,
        context: Any = None,
        *,
        since_id: str = "",
    ) -> List[Dict[str, Any]]:
        del context
        target = str(query or "").strip()
        post_id = self.extract_post_id(target)
        if post_id:
            post = await asyncio.to_thread(self._lookup_post_sync, post_id)
            return [post] if post else []

        api_query = self._prepare_discovery_query(target)
        if not api_query:
            return []
        self.logger.info("Searching X API (%s) for %r", self.search_mode, api_query)
        posts, _ = await asyncio.to_thread(
            self._search_posts_sync,
            api_query,
            max_posts=max(0, int(max_videos or 0)),
            since_id=str(since_id or "").strip(),
            apply_date_bounds=True,
        )
        self.logger.info("X API found %s posts for %r", len(posts), api_query)
        return posts

    @staticmethod
    def _nest_comments(comments: List[Dict[str, Any]], root_post_id: str) -> List[Dict[str, Any]]:
        by_id = {
            str(comment.get("comment_id") or ""): comment
            for comment in comments
            if comment.get("comment_id")
        }
        roots: List[Dict[str, Any]] = []
        for comment in comments:
            parent_post_id = str(comment.get("reply_to_post_id") or "")
            parent = by_id.get(parent_post_id)
            if parent is not None and parent is not comment:
                comment["parent_comment_id"] = parent_post_id
                parent.setdefault("replies", []).append(comment)
            else:
                if parent_post_id == root_post_id:
                    comment["parent_comment_id"] = ""
                roots.append(comment)

        def sort_tree(nodes: List[Dict[str, Any]]) -> None:
            nodes.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("comment_id") or "")))
            for node in nodes:
                replies = node.get("replies")
                if isinstance(replies, list):
                    sort_tree(replies)

        sort_tree(roots)
        return roots

    @staticmethod
    def _recent_scope_can_be_complete(published_at: str) -> bool:
        try:
            published = dt.datetime.fromisoformat(str(published_at or "").replace("Z", "+00:00"))
        except ValueError:
            return False
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.timezone.utc)
        return published.astimezone(dt.timezone.utc) >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)

    def _extract_comments_sync(
        self,
        video_data: Dict[str, Any],
        max_comments: int = 0,
    ) -> Dict[str, Any]:
        root_post_id = str(
            video_data.get("video_id")
            or video_data.get("id")
            or self.extract_post_id(str(video_data.get("url") or ""))
        )
        if not root_post_id:
            raise XAPIError("Cannot collect X replies because the post ID is missing")

        query = f"conversation_id:{root_post_id} is:reply"
        posts, endpoint_exhausted = self._search_posts_sync(
            query,
            max_posts=max(0, int(max_comments or 0)),
            apply_date_bounds=False,
            discovery_method="x_api_v2_conversation_search",
        )
        comments = []
        # _search_posts_sync already normalizes each post and keeps the raw API
        # object. Rebuild comments from those raw objects so parent IDs survive.
        for candidate in posts:
            post = candidate.get("x_api_post") if isinstance(candidate.get("x_api_post"), dict) else {}
            author = candidate.get("x_api_author") if isinstance(candidate.get("x_api_author"), dict) else {}
            includes = {
                "users": {str(author.get("id") or ""): author} if author.get("id") else {},
                "media": {},
                "places": {},
                "tweets": {},
            }
            comments.append(self._post_to_comment(post, includes, root_post_id))

        comments_seen = len(comments)
        nested_comments = self._nest_comments(comments, root_post_id)
        limit_reached = bool(max_comments and max_comments > 0 and comments_seen >= max_comments)
        comments_exhausted: Optional[bool]
        if limit_reached:
            comments_exhausted = False
        elif self.search_mode == "all" or self._recent_scope_can_be_complete(
            str(video_data.get("published_at") or video_data.get("published") or "")
        ):
            comments_exhausted = endpoint_exhausted
        else:
            # A recent-search endpoint can exhaust its seven-day window without
            # proving that an older post's historical conversation is complete.
            comments_exhausted = None

        return {
            **dict(video_data),
            "video_id": root_post_id,
            "url": video_data.get("url") or f"https://x.com/i/status/{root_post_id}",
            "caption": video_data.get("caption") or video_data.get("title") or "",
            "title": video_data.get("title") or video_data.get("caption") or "",
            "description": video_data.get("description") or video_data.get("caption") or "",
            "username": video_data.get("username") or "X User",
            "comments": nested_comments,
            "comments_seen_in_response": comments_seen,
            "comment_limit_reached": limit_reached,
            "comments_exhausted": comments_exhausted,
            "comment_history_scope": "full_archive" if self.search_mode == "all" else "recent_7_days",
            "comment_error": "",
            "discovery_method": video_data.get("discovery_method") or "direct_url",
            "metadata_method": video_data.get("metadata_method") or "x_api_v2",
            "comment_method": "x_api_v2_conversation_search",
            "x_api_usage": self.usage_summary(),
        }

    async def extract_comments(
        self,
        video_data: Dict[str, Any],
        max_comments: int = 100,
        context: Any = None,
    ) -> Dict[str, Any]:
        del context
        try:
            return await asyncio.to_thread(
                self._extract_comments_sync,
                video_data,
                max(0, int(max_comments or 0)),
            )
        except Exception as exc:
            self.logger.error("Could not collect X replies for %s: %s", video_data.get("url"), exc)
            return {
                **dict(video_data),
                "comments": [],
                "comments_seen_in_response": 0,
                "comments_exhausted": None,
                "comment_error": str(exc),
                "metadata_method": video_data.get("metadata_method") or "x_api_v2",
                "comment_method": "x_api_v2_conversation_search",
                "x_api_usage": self.usage_summary(),
            }

    def format_output(self, raw_comments: List[Any]) -> List[Dict[str, Any]]:
        return [dict(comment) for comment in raw_comments if isinstance(comment, dict)]
