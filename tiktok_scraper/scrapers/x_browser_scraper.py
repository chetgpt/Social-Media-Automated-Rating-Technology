"""Authenticated X web GraphQL transport with no DOM data extraction."""

from __future__ import annotations

import asyncio
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import quote, urlparse

from playwright.async_api import BrowserContext, async_playwright

from .base_scraper import BaseScraper
from .x_scraper import XScraper


class XBrowserAPIError(RuntimeError):
    """Raised when authenticated X web GraphQL collection cannot continue."""


class XBrowserScraper(BaseScraper):
    """Capture the JSON APIs used by a legitimately authenticated X session."""

    GRAPHQL_MARKER = "/i/api/graphql/"
    SEARCH_OPERATIONS = {
        "SearchTimeline",
        "UserTweets",
        "UserTweetsAndReplies",
        "UserMedia",
    }
    DETAIL_OPERATIONS = {
        "TweetDetail",
        "TweetResultByRestId",
    }

    def __init__(self) -> None:
        super().__init__("x")
        self._external_context = False
        self._cdp_browser = None
        self.graphql_response_count = 0
        self.graphql_operations: Set[str] = set()

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

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _storage_state_path() -> str:
        path = os.environ.get("X_STORAGE_STATE", "").strip()
        if path and not os.path.exists(path):
            raise XBrowserAPIError(f"X storage-state file does not exist: {path}")
        return path

    @staticmethod
    def _user_data_dir() -> str:
        path = os.environ.get("X_USER_DATA_DIR", "").strip()
        if path:
            os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _profile_directory() -> str:
        return os.environ.get("X_PROFILE_DIRECTORY", "").strip()

    @staticmethod
    def _browser_channel() -> str:
        return os.environ.get("X_BROWSER_CHANNEL", "").strip()

    @staticmethod
    def _cdp_url() -> str:
        return os.environ.get("X_CDP_URL", "").strip()

    @classmethod
    def _headless(cls) -> bool:
        return cls._env_bool("X_HEADLESS", True)

    @staticmethod
    def _user_agent() -> str:
        return os.environ.get("X_USER_AGENT", "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

    @classmethod
    def _viewport(cls) -> Dict[str, int]:
        return {
            "width": cls._env_int("X_VIEWPORT_WIDTH", 1366, 320),
            "height": cls._env_int("X_VIEWPORT_HEIGHT", 900, 320),
        }

    async def _new_context(self):
        playwright = await async_playwright().start()
        self._external_context = False
        self._cdp_browser = None

        cdp_url = self._cdp_url()
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            self._external_context = True
            self._cdp_browser = browser
            if browser.contexts:
                return playwright, None, browser.contexts[0]
            context = await browser.new_context(
                viewport=self._viewport(),
                user_agent=self._user_agent(),
                locale="en-US",
            )
            return playwright, None, context

        common = {
            "viewport": self._viewport(),
            "user_agent": self._user_agent(),
            "locale": "en-US",
        }
        user_data_dir = self._user_data_dir()
        if user_data_dir:
            launch_kwargs: Dict[str, Any] = {}
            if self._browser_channel():
                launch_kwargs["channel"] = self._browser_channel()
            args = []
            if self._profile_directory():
                args.append(f"--profile-directory={self._profile_directory()}")
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=self._headless(),
                args=args,
                **launch_kwargs,
                **common,
            )
            return playwright, None, context

        launch_kwargs = {}
        if self._browser_channel():
            launch_kwargs["channel"] = self._browser_channel()
        browser = await playwright.chromium.launch(headless=self._headless(), **launch_kwargs)
        context_kwargs = dict(common)
        if self._storage_state_path():
            context_kwargs["storage_state"] = self._storage_state_path()
        context = await browser.new_context(**context_kwargs)
        return playwright, browser, context

    @staticmethod
    async def _authenticated(context: BrowserContext) -> bool:
        cookies = await context.cookies(["https://x.com/", "https://twitter.com/"])
        names = {str(cookie.get("name") or "") for cookie in cookies}
        return "auth_token" in names and "ct0" in names

    async def require_authenticated(self, context: BrowserContext) -> None:
        if not self._env_bool("X_REQUIRE_AUTH", True):
            return
        if not await self._authenticated(context):
            raise XBrowserAPIError(
                "The selected X browser session is not authenticated. "
                "Log in to X and export an X storage-state file first."
            )

    def usage_summary(self) -> Dict[str, Any]:
        return {
            "transport": "authenticated_web_graphql",
            "official_api": False,
            "official_api_post_reads": 0,
            "estimated_official_api_cost_usd": 0.0,
            "graphql_response_count": self.graphql_response_count,
            "graphql_operations": sorted(self.graphql_operations),
        }

    @classmethod
    def _operation_name(cls, url: str) -> str:
        path = urlparse(url).path
        if cls.GRAPHQL_MARKER not in path:
            return ""
        tail = path.split(cls.GRAPHQL_MARKER, 1)[1]
        parts = [part for part in tail.split("/") if part]
        return parts[1] if len(parts) >= 2 else ""

    @classmethod
    def _is_graphql_response(cls, url: str, operations: Set[str]) -> bool:
        operation = cls._operation_name(url)
        return operation in operations

    @classmethod
    def _target_url(cls, value: str) -> str:
        target = str(value or "").strip()
        post_id = XScraper.extract_post_id(target)
        if post_id:
            return target if XScraper.is_x_url(target) else f"https://x.com/i/status/{post_id}"
        if XScraper.is_x_url(target):
            return target
        handle = XScraper.extract_handle(target)
        if handle:
            return f"https://x.com/{handle}"
        return f"https://x.com/search?q={quote(target)}&src=typed_query&f=live"

    @staticmethod
    def _walk(node: Any) -> Iterable[Dict[str, Any]]:
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                yield current
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    @staticmethod
    def _unwrap_result(result: Any) -> Dict[str, Any]:
        current = result
        visited: Set[int] = set()
        while isinstance(current, dict) and id(current) not in visited:
            visited.add(id(current))
            if current.get("rest_id") and isinstance(current.get("legacy"), dict):
                return current
            typename = str(current.get("__typename") or "")
            if typename in {"TweetWithVisibilityResults", "TweetTombstone"} and isinstance(current.get("tweet"), dict):
                current = current["tweet"]
                continue
            if isinstance(current.get("result"), dict):
                current = current["result"]
                continue
            if isinstance(current.get("tweet"), dict):
                current = current["tweet"]
                continue
            break
        return current if isinstance(current, dict) else {}

    @classmethod
    def _tweet_results(cls, payload: Any) -> Iterable[Dict[str, Any]]:
        seen: Set[str] = set()
        for node in cls._walk(payload):
            containers = []
            if isinstance(node.get("tweet_results"), dict):
                containers.append(node["tweet_results"])
            if isinstance(node.get("tweetResult"), dict):
                containers.append(node["tweetResult"])
            for container in containers:
                tweet = cls._unwrap_result(container.get("result") or container)
                post_id = str(tweet.get("rest_id") or "")
                if post_id and post_id not in seen:
                    seen.add(post_id)
                    yield tweet

    @classmethod
    def _has_bottom_cursor(cls, payload: Any) -> bool:
        for node in cls._walk(payload):
            cursor_type = str(node.get("cursorType") or node.get("cursor_type") or "").lower()
            if cursor_type == "bottom" and node.get("value"):
                return True
            entry_id = str(node.get("entryId") or node.get("entry_id") or "").lower()
            if "cursor-bottom" in entry_id and node.get("content"):
                return True
        return False

    @classmethod
    def _graphql_errors(cls, payload: Any) -> List[str]:
        errors = payload.get("errors") if isinstance(payload, dict) else []
        output = []
        for error in errors if isinstance(errors, list) else []:
            if not isinstance(error, dict):
                continue
            message = str(error.get("message") or error.get("detail") or error.get("code") or "").strip()
            if message:
                output.append(message[:300])
        return output

    @staticmethod
    def _user_from_tweet(tweet: Dict[str, Any]) -> Dict[str, Any]:
        core = tweet.get("core") if isinstance(tweet.get("core"), dict) else {}
        user_results = core.get("user_results") if isinstance(core.get("user_results"), dict) else {}
        return XBrowserScraper._unwrap_result(user_results.get("result") or user_results)

    @staticmethod
    def _iso_time(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _note_result(tweet: Dict[str, Any]) -> Dict[str, Any]:
        note = tweet.get("note_tweet") if isinstance(tweet.get("note_tweet"), dict) else {}
        results = note.get("note_tweet_results") if isinstance(note.get("note_tweet_results"), dict) else {}
        return results.get("result") if isinstance(results.get("result"), dict) else {}

    @classmethod
    def _tweet_text(cls, tweet: Dict[str, Any]) -> str:
        legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
        note = cls._note_result(tweet)
        return str(note.get("text") or legacy.get("full_text") or legacy.get("text") or "").strip()

    @staticmethod
    def _media(legacy: Dict[str, Any]) -> List[Dict[str, Any]]:
        extended = legacy.get("extended_entities") if isinstance(legacy.get("extended_entities"), dict) else {}
        raw_media = extended.get("media") if isinstance(extended.get("media"), list) else []
        output = []
        for item in raw_media:
            if not isinstance(item, dict):
                continue
            video_info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
            output.append({
                "media_key": str(item.get("media_key") or item.get("id_str") or ""),
                "type": str(item.get("type") or ""),
                "url": str(item.get("media_url_https") or item.get("media_url") or ""),
                "expanded_url": str(item.get("expanded_url") or ""),
                "width": (item.get("original_info") or {}).get("width") if isinstance(item.get("original_info"), dict) else None,
                "height": (item.get("original_info") or {}).get("height") if isinstance(item.get("original_info"), dict) else None,
                "duration_ms": video_info.get("duration_millis"),
                "variants": video_info.get("variants") if isinstance(video_info.get("variants"), list) else [],
            })
        return output

    @staticmethod
    def _content_type(media: List[Dict[str, Any]]) -> str:
        types = {str(item.get("type") or "").lower() for item in media}
        if "video" in types or "animated_gif" in types:
            return "x_video_post"
        if "photo" in types:
            return "x_image_post"
        return "x_text_post"

    @classmethod
    def tweet_to_candidate(
        cls,
        tweet: Dict[str, Any],
        *,
        discovery_method: str = "x_graphql_response",
    ) -> Dict[str, Any]:
        legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
        user = cls._user_from_tweet(tweet)
        user_legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else {}
        user_core = user.get("core") if isinstance(user.get("core"), dict) else {}
        post_id = str(tweet.get("rest_id") or legacy.get("id_str") or "")
        username = str(user_core.get("screen_name") or user_legacy.get("screen_name") or "")
        display_name = str(user_core.get("name") or user_legacy.get("name") or "")
        media = cls._media(legacy)
        views = tweet.get("views") if isinstance(tweet.get("views"), dict) else {}
        text = cls._tweet_text(tweet)
        entities = legacy.get("entities") if isinstance(legacy.get("entities"), dict) else {}
        note = cls._note_result(tweet)
        if isinstance(note.get("entity_set"), dict):
            entities = {**entities, **note["entity_set"]}
        return {
            "platform": "x",
            "video_id": post_id,
            "url": f"https://x.com/{username or 'i'}/status/{post_id}",
            "title": text,
            "caption": text,
            "description": text,
            "username": username or display_name or "X User",
            "creator_id": str(user.get("rest_id") or legacy.get("user_id_str") or ""),
            "creator_display_name": display_name,
            "creator_verified": bool(user.get("is_blue_verified") or user_legacy.get("verified")),
            "creator_profile_location": str(user_legacy.get("location") or ""),
            "follower_count": user_legacy.get("followers_count"),
            "published_at": cls._iso_time(legacy.get("created_at")),
            "content_type": cls._content_type(media),
            "content_language": str(legacy.get("lang") or ""),
            "conversation_id": str(legacy.get("conversation_id_str") or post_id),
            "in_reply_to_post_id": str(legacy.get("in_reply_to_status_id_str") or ""),
            "in_reply_to_user_id": str(legacy.get("in_reply_to_user_id_str") or ""),
            "view_count": views.get("count"),
            "like_count": legacy.get("favorite_count"),
            "share_count": legacy.get("retweet_count"),
            "save_count": legacy.get("bookmark_count"),
            "quote_count": legacy.get("quote_count"),
            "retweet_count": legacy.get("retweet_count"),
            "reported_comment_count": legacy.get("reply_count"),
            "reply_count": legacy.get("reply_count"),
            "metric_availability": {
                "views": "available" if "count" in views and views.get("count") is not None else "not_publicly_exposed",
                "likes": "available" if "favorite_count" in legacy else "missing_from_public_response",
                "reported_comments": "available" if "reply_count" in legacy else "missing_from_public_response",
                "shares": "available" if "retweet_count" in legacy else "missing_from_public_response",
                "saves": "available" if "bookmark_count" in legacy else "not_publicly_exposed",
                "followers": "available" if "followers_count" in user_legacy else "missing_from_public_response",
            },
            "possibly_sensitive": bool(legacy.get("possibly_sensitive")),
            "media": media,
            "entities": entities,
            "is_reply_post": bool(legacy.get("in_reply_to_status_id_str")),
            "is_retweet": bool(legacy.get("retweeted_status_result")) or text.startswith("RT @"),
            "discovery_method": discovery_method,
            "metadata_method": "x_graphql_response",
            "x_graphql_tweet": tweet,
            "x_graphql_author": user,
        }

    @classmethod
    def tweet_to_comment(cls, tweet: Dict[str, Any], root_post_id: str) -> Dict[str, Any]:
        candidate = cls.tweet_to_candidate(tweet, discovery_method="x_tweet_detail_graphql")
        parent_post_id = str(candidate.get("in_reply_to_post_id") or "")
        return {
            "platform": "x",
            "comment_id": candidate["video_id"],
            "id": candidate["video_id"],
            "text": candidate["caption"],
            "author": candidate["username"],
            "author_id": candidate["creator_id"],
            "author_display_name": candidate["creator_display_name"],
            "author_verified": candidate["creator_verified"],
            "author_profile_location": candidate["creator_profile_location"],
            "likes": candidate.get("like_count") or 0,
            "reply_count": candidate.get("reply_count") or 0,
            "retweet_count": candidate.get("retweet_count") or 0,
            "quote_count": candidate.get("quote_count") or 0,
            "created_at": candidate.get("published_at") or "",
            "time": candidate.get("published_at") or "",
            "lang": candidate.get("content_language") or "",
            "is_reply": True,
            "reply_to_post_id": parent_post_id,
            "parent_comment_id": "" if parent_post_id == root_post_id else parent_post_id,
            "url": candidate["url"],
            "x_graphql_tweet": candidate["x_graphql_tweet"],
            "x_graphql_author": candidate["x_graphql_author"],
        }

    @classmethod
    def collect_candidates(
        cls,
        payload: Any,
        output: Dict[str, Dict[str, Any]],
        *,
        discovery_method: str,
    ) -> int:
        before = len(output)
        for tweet in cls._tweet_results(payload):
            candidate = cls.tweet_to_candidate(tweet, discovery_method=discovery_method)
            post_id = str(candidate.get("video_id") or "")
            if post_id:
                output[post_id] = candidate
        return len(output) - before

    @classmethod
    def _eligible_discovery(cls, candidate: Dict[str, Any]) -> bool:
        if candidate.get("is_reply_post") and not cls._env_bool("X_INCLUDE_REPLIES_IN_DISCOVERY", False):
            return False
        if candidate.get("is_retweet") and not cls._env_bool("X_INCLUDE_RETWEETS_IN_DISCOVERY", False):
            return False
        return True

    @staticmethod
    async def _await_tasks(tasks: Set[asyncio.Task], timeout: float = 10.0) -> None:
        if not tasks:
            return
        _, pending = await asyncio.wait(set(tasks), timeout=timeout)
        for task in pending:
            task.cancel()

    async def search(
        self,
        query: str,
        max_videos: int = 10,
        context: Optional[BrowserContext] = None,
        **_: Any,
    ) -> List[Dict[str, Any]]:
        max_posts = max(0, int(max_videos or 0))
        manage_context = context is None
        playwright = browser = None
        if manage_context:
            playwright, browser, context = await self._new_context()
        await self.require_authenticated(context)

        page = await context.new_page()
        tasks: Set[asyncio.Task] = set()
        posts_by_id: Dict[str, Dict[str, Any]] = {}
        state: Dict[str, Any] = {
            "response_count": 0,
            "last_has_bottom_cursor": None,
            "operations": set(),
            "errors": [],
        }

        async def capture(response) -> None:
            operation = self._operation_name(response.url)
            if operation not in self.SEARCH_OPERATIONS | self.DETAIL_OPERATIONS:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            self.graphql_response_count += 1
            self.graphql_operations.add(operation)
            state["response_count"] += 1
            state["operations"].add(operation)
            state["errors"].extend(self._graphql_errors(payload))
            method = "x_tweet_detail_graphql" if operation in self.DETAIL_OPERATIONS else "x_search_timeline_graphql"
            added = self.collect_candidates(payload, posts_by_id, discovery_method=method)
            if operation in self.SEARCH_OPERATIONS:
                state["last_has_bottom_cursor"] = self._has_bottom_cursor(payload)

        def schedule(response) -> None:
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", schedule)
        target_url = self._target_url(query)

        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(self._env_float("X_INITIAL_WAIT_SECONDS", 3.0))
            if any(marker in page.url for marker in ("/i/flow/login", "/account/access", "/login")):
                raise XBrowserAPIError("X redirected the selected session to a login or access-check page")

            rounds = self._env_int("X_SEARCH_SCROLL_ROUNDS", 24 if not max_posts else 10, 1)
            stall_limit = self._env_int("X_SEARCH_STALL_ROUNDS", 4 if not max_posts else 3, 1)
            stall = 0
            previous = -1
            for _round in range(rounds):
                await self._await_tasks(tasks, timeout=3.0)
                eligible = [item for item in posts_by_id.values() if self._eligible_discovery(item)]
                if max_posts and len(eligible) >= max_posts:
                    break
                current = len(posts_by_id)
                stall = stall + 1 if current == previous else 0
                previous = current
                if stall >= stall_limit:
                    break
                await page.mouse.wheel(0, self._env_int("X_SCROLL_PIXELS", 1800, 200))
                await asyncio.sleep(self._env_float("X_SEARCH_SCROLL_DELAY_SECONDS", 1.5))

            await self._await_tasks(tasks)
            eligible = [item for item in posts_by_id.values() if self._eligible_discovery(item)]
            if max_posts:
                eligible = eligible[:max_posts]
            if not eligible:
                error_text = "; ".join(dict.fromkeys(state["errors"]))
                raise XBrowserAPIError(
                    "X browser API captured no post records"
                    + (f": {error_text}" if error_text else "")
                )
            for item in eligible:
                item["browser_api_response_count"] = int(state["response_count"])
                item["browser_api_operations"] = sorted(state["operations"])
                item["discovery_exhausted"] = (
                    state["last_has_bottom_cursor"] is False and (not max_posts or len(eligible) < max_posts)
                )
            return eligible
        finally:
            for task in list(tasks):
                task.cancel()
            await page.close()
            if manage_context and not self._external_context:
                await context.close()
                if browser:
                    await browser.close()
                await playwright.stop()
            elif manage_context and playwright:
                await playwright.stop()

    async def extract_comments(
        self,
        video_data: Dict[str, Any],
        max_comments: int = 100,
        context: Optional[BrowserContext] = None,
    ) -> Dict[str, Any]:
        root_post_id = str(
            video_data.get("video_id")
            or video_data.get("id")
            or XScraper.extract_post_id(str(video_data.get("url") or ""))
        )
        if not root_post_id:
            raise XBrowserAPIError("Cannot collect X replies because the root post ID is missing")
        max_comments = max(0, int(max_comments or 0))
        manage_context = context is None
        playwright = browser = None
        if manage_context:
            playwright, browser, context = await self._new_context()
        await self.require_authenticated(context)

        page = await context.new_page()
        tasks: Set[asyncio.Task] = set()
        tweets_by_id: Dict[str, Dict[str, Any]] = {}
        state: Dict[str, Any] = {
            "response_count": 0,
            "last_has_bottom_cursor": None,
            "operations": set(),
            "errors": [],
        }

        async def capture(response) -> None:
            operation = self._operation_name(response.url)
            if operation not in self.DETAIL_OPERATIONS:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            self.graphql_response_count += 1
            self.graphql_operations.add(operation)
            state["response_count"] += 1
            state["operations"].add(operation)
            state["errors"].extend(self._graphql_errors(payload))
            before = len(tweets_by_id)
            for tweet in self._tweet_results(payload):
                candidate = self.tweet_to_candidate(tweet, discovery_method="x_tweet_detail_graphql")
                post_id = str(candidate.get("video_id") or "")
                if post_id:
                    tweets_by_id[post_id] = tweet
            if operation == "TweetDetail":
                state["last_has_bottom_cursor"] = self._has_bottom_cursor(payload)

        def schedule(response) -> None:
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", schedule)
        target_url = str(video_data.get("url") or f"https://x.com/i/status/{root_post_id}")
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(self._env_float("X_POST_INITIAL_WAIT_SECONDS", 3.0))
            if any(marker in page.url for marker in ("/i/flow/login", "/account/access", "/login")):
                raise XBrowserAPIError("X redirected the selected session to a login or access-check page")

            rounds = self._env_int("X_COMMENT_SCROLL_ROUNDS", 30 if not max_comments else 12, 1)
            stall_limit = self._env_int("X_COMMENT_STALL_ROUNDS", 4 if not max_comments else 3, 1)
            stall = 0
            previous = -1
            for _round in range(rounds):
                await self._await_tasks(tasks, timeout=3.0)
                replies = [
                    tweet
                    for post_id, tweet in tweets_by_id.items()
                    if post_id != root_post_id
                    and str((tweet.get("legacy") or {}).get("conversation_id_str") or "") == root_post_id
                    and (tweet.get("legacy") or {}).get("in_reply_to_status_id_str")
                ]
                if max_comments and len(replies) >= max_comments:
                    break
                current = len(replies)
                stall = stall + 1 if current == previous else 0
                previous = current
                if stall >= stall_limit:
                    break
                await page.mouse.wheel(0, self._env_int("X_SCROLL_PIXELS", 1800, 200))
                await asyncio.sleep(self._env_float("X_COMMENT_SCROLL_DELAY_SECONDS", 1.5))

            await self._await_tasks(tasks)
            raw_replies = [
                tweet
                for post_id, tweet in tweets_by_id.items()
                if post_id != root_post_id
                and str((tweet.get("legacy") or {}).get("conversation_id_str") or "") == root_post_id
                and (tweet.get("legacy") or {}).get("in_reply_to_status_id_str")
            ]
            raw_replies.sort(
                key=lambda tweet: (
                    self._iso_time((tweet.get("legacy") or {}).get("created_at")),
                    str(tweet.get("rest_id") or ""),
                )
            )
            seen_count = len(raw_replies)
            limit_reached = bool(max_comments and seen_count >= max_comments)
            if max_comments:
                raw_replies = raw_replies[:max_comments]
            comments = [self.tweet_to_comment(tweet, root_post_id) for tweet in raw_replies]
            nested = XScraper._nest_comments(comments, root_post_id)
            comments_exhausted: Optional[bool]
            if limit_reached:
                comments_exhausted = False
            elif state["last_has_bottom_cursor"] is False and state["response_count"]:
                comments_exhausted = True
            else:
                comments_exhausted = None

            error_text = "; ".join(dict.fromkeys(state["errors"]))
            return {
                **dict(video_data),
                "video_id": root_post_id,
                "url": target_url,
                "caption": video_data.get("caption") or video_data.get("title") or "",
                "title": video_data.get("title") or video_data.get("caption") or "",
                "username": video_data.get("username") or "X User",
                "comments": nested,
                "comments_seen_in_response": seen_count,
                "comment_limit_reached": limit_reached,
                "comments_exhausted": comments_exhausted,
                "comment_history_scope": "visible_conversation_graphql",
                "comment_error": error_text,
                "discovery_method": video_data.get("discovery_method") or "direct_url",
                "metadata_method": video_data.get("metadata_method") or "x_graphql_response",
                "comment_method": "x_browser_graphql",
                "browser_api_response_count": int(state["response_count"]),
                "browser_api_operations": sorted(state["operations"]),
            }
        except Exception as exc:
            return {
                **dict(video_data),
                "video_id": root_post_id,
                "url": target_url,
                "comments": [],
                "comments_seen_in_response": 0,
                "comments_exhausted": None,
                "comment_error": str(exc),
                "metadata_method": video_data.get("metadata_method") or "x_graphql_response",
                "comment_method": "x_browser_graphql",
                "browser_api_response_count": int(state["response_count"]),
                "browser_api_operations": sorted(state["operations"]),
            }
        finally:
            for task in list(tasks):
                task.cancel()
            await page.close()
            if manage_context and not self._external_context:
                await context.close()
                if browser:
                    await browser.close()
                await playwright.stop()
            elif manage_context and playwright:
                await playwright.stop()

    def format_output(self, raw_comments: List[Any]) -> List[Dict[str, Any]]:
        return [dict(comment) for comment in raw_comments if isinstance(comment, dict)]
