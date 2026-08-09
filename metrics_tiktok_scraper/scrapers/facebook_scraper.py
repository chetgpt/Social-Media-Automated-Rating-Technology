from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set
from .base_scraper import BaseScraper
from playwright.async_api import BrowserContext, Page, async_playwright
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import time


class FacebookScraper(BaseScraper):
    """Browser-backed scraper for public Facebook posts, reels, and videos.

    Facebook's official API access depends on app review and permissions, so this
    adapter keeps the first implementation session-based and conservative. It
    only works with content that the browser session can legitimately view.
    """

    FACEBOOK_HOST_RE = re.compile(r"(^|\.)facebook\.com$", re.I)
    TRACKING_PARAMS = {
        "__cft__",
        "__tn__",
        "acontext",
        "comment_id",
        "eid",
        "extid",
        "mibextid",
        "notif_id",
        "notif_t",
        "paipv",
        "ref",
        "refid",
        "tracking",
    }

    def __init__(self):
        super().__init__("facebook")
        self._external_context = False
        self._cdp_browser = None

    def _env_int(self, name: str, default: int, minimum: int = 1) -> int:
        value = os.environ.get(name, "").strip()
        if not value:
            return default
        try:
            return max(minimum, int(value))
        except ValueError:
            self.logger.warning(f"Ignoring invalid {name}={value!r}")
            return default

    def _env_float(self, name: str, default: float, minimum: float = 0.0) -> float:
        value = os.environ.get(name, "").strip()
        if not value:
            return default
        try:
            return max(minimum, float(value))
        except ValueError:
            self.logger.warning(f"Ignoring invalid {name}={value!r}")
            return default

    def _storage_state_path(self) -> Optional[str]:
        path = os.environ.get("FACEBOOK_STORAGE_STATE") or os.environ.get("FB_STORAGE_STATE")
        if path and os.path.exists(path):
            return path
        return None

    def _user_data_dir(self) -> Optional[str]:
        path = os.environ.get("FACEBOOK_USER_DATA_DIR") or os.environ.get("FB_USER_DATA_DIR")
        if path:
            os.makedirs(path, exist_ok=True)
            return path
        return None

    def _headless(self) -> bool:
        value = os.environ.get("FACEBOOK_HEADLESS", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _profile_directory(self) -> Optional[str]:
        value = os.environ.get("FACEBOOK_PROFILE_DIRECTORY") or os.environ.get("FB_PROFILE_DIRECTORY")
        return value.strip() if value and value.strip() else None

    def _browser_channel(self) -> Optional[str]:
        value = os.environ.get("FACEBOOK_BROWSER_CHANNEL") or os.environ.get("FB_BROWSER_CHANNEL")
        return value.strip() if value and value.strip() else None

    def _extraction_mode(self) -> str:
        value = (os.environ.get("FACEBOOK_EXTRACTION_MODE") or os.environ.get("FB_EXTRACTION_MODE") or "browser-api")
        normalized = value.strip().lower().replace("_", "-")
        aliases = {
            "api": "browser-api",
            "browserapi": "browser-api",
            "browser-api": "browser-api",
            "network": "browser-api",
            "network-api": "browser-api",
            "dom": "dom",
            "manual": "dom",
            "hybrid": "hybrid",
        }
        mode = aliases.get(normalized)
        if not mode:
            self.logger.warning(f"Ignoring invalid FACEBOOK_EXTRACTION_MODE={value!r}; using browser-api")
            return "browser-api"
        return mode

    def _cdp_url(self) -> Optional[str]:
        value = os.environ.get("FACEBOOK_CDP_URL") or os.environ.get("FB_CDP_URL")
        return value.strip() if value and value.strip() else None

    def _user_agent(self) -> str:
        value = os.environ.get("FACEBOOK_USER_AGENT") or os.environ.get("FB_USER_AGENT")
        if value and value.strip():
            return value.strip()
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

    def _viewport(self) -> Dict[str, int]:
        return {
            "width": self._env_int("FACEBOOK_VIEWPORT_WIDTH", 1366, 320),
            "height": self._env_int("FACEBOOK_VIEWPORT_HEIGHT", 900, 320),
        }

    async def _new_context(self):
        playwright = await async_playwright().start()
        self._external_context = False
        self._cdp_browser = None

        cdp_url = self._cdp_url()
        if cdp_url:
            self.logger.info(f"Connecting to existing Facebook browser over CDP: {cdp_url}")
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

        kwargs = {
            "viewport": self._viewport(),
            "user_agent": self._user_agent(),
            "locale": "en-US",
        }

        user_data_dir = self._user_data_dir()
        if user_data_dir:
            launch_kwargs = {}
            args = []
            browser_channel = self._browser_channel()
            if browser_channel:
                launch_kwargs["channel"] = browser_channel
                self.logger.info(f"Using browser channel: {browser_channel}")
            profile_directory = self._profile_directory()
            if profile_directory:
                args.append(f"--profile-directory={profile_directory}")
                self.logger.info(f"Using browser profile directory: {profile_directory}")
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=self._headless(),
                args=args,
                **launch_kwargs,
                **kwargs,
            )
            return playwright, None, context

        launch_kwargs = {}
        browser_channel = self._browser_channel()
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
            self.logger.info(f"Using browser channel: {browser_channel}")
        browser = await playwright.chromium.launch(headless=self._headless(), **launch_kwargs)
        storage_state = self._storage_state_path()
        if storage_state:
            kwargs["storage_state"] = storage_state
            self.logger.info(f"Using Facebook storage state from {storage_state}")
        context = await browser.new_context(**kwargs)
        return playwright, browser, context

    def is_facebook_url(self, value: str) -> bool:
        try:
            parsed = urlparse(value)
            return bool(parsed.netloc and self.FACEBOOK_HOST_RE.search(parsed.netloc))
        except Exception:
            return False

    def _search_urls_for_query(self, query: str) -> List[str]:
        query = (query or "").strip()
        if self.is_facebook_url(query):
            return [self._normalize_url(query)]
        if query.startswith("@"):
            return [f"https://www.facebook.com/{query[1:].strip('/')}/"]

        scopes = [
            scope.strip().lower()
            for scope in (os.environ.get("FACEBOOK_SEARCH_SCOPES") or "top,posts,videos,reels").split(",")
            if scope.strip()
        ]
        search_paths = {
            "top": "search/top/",
            "posts": "search/posts/",
            "post": "search/posts/",
            "videos": "search/videos/",
            "video": "search/videos/",
            "reels": "reels/search/",
            "reel": "reels/search/",
        }
        urls: List[str] = []
        seen: Set[str] = set()
        for scope in scopes:
            path = search_paths.get(scope)
            if not path:
                continue
            url = f"https://www.facebook.com/{path}?q={quote(query)}"
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls or [f"https://www.facebook.com/search/top/?q={quote(query)}"]

    def _search_url_for_query(self, query: str) -> str:
        return self._search_urls_for_query(query)[0]

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        text = url.strip()
        if text.startswith("fb.watch/"):
            text = f"https://{text}"
        if text.startswith("facebook.com/") or text.startswith("www.facebook.com/"):
            text = f"https://{text}"
        parsed = urlparse(text)
        if not parsed.scheme:
            parsed = urlparse(f"https://www.facebook.com/{text.lstrip('/')}")
        netloc = parsed.netloc.lower()
        if netloc in {"m.facebook.com", "mbasic.facebook.com"}:
            netloc = "www.facebook.com"
        query_pairs = []
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
            if key in self.TRACKING_PARAMS:
                continue
            for value in values:
                query_pairs.append((key, value))
        query = urlencode(query_pairs, doseq=True)
        normalized = urlunparse((parsed.scheme or "https", netloc, parsed.path, "", query, ""))
        return normalized.rstrip("/")

    def extract_content_id(self, url: str, fallback: bool = False) -> str:
        normalized = self._normalize_url(url)
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)
        set_value = query.get("set", [""])[0]
        group_photo_match = re.fullmatch(r"(?:gm|pcb)\.(\d+)", set_value or "", re.I)
        if group_photo_match:
            return group_photo_match.group(1)
        for key in ("v", "story_fbid", "fbid"):
            value = query.get(key, [""])[0]
            if value:
                return value

        parts = [part for part in parsed.path.strip("/").split("/") if part]
        lowered = [part.lower() for part in parts]
        for marker in ("reel", "posts", "permalink"):
            if marker in lowered:
                index = lowered.index(marker)
                if index + 1 < len(parts):
                    return parts[index + 1]
        if "videos" in lowered:
            index = lowered.index("videos")
            tail = parts[index + 1:]
            for part in reversed(tail):
                if re.fullmatch(r"\d{5,}", part):
                    return part
            if tail:
                return tail[0]
        group_match = re.search(r"/groups/[^/]+/posts/([^/?#]+)", normalized)
        if group_match:
            return group_match.group(1)
        return self._stable_id(normalized) if fallback and normalized else ""

    def _is_content_url(self, url: str) -> bool:
        normalized = self._normalize_url(url)
        if not normalized or "facebook.com" not in normalized.lower():
            return False
        return bool(
            self.extract_content_id(normalized)
            and re.search(
                r"/(?:watch|reel|videos|posts|permalink|groups/[^/]+/posts|photo\.php|story\.php)|[?&](?:v|fbid|story_fbid)=",
                normalized,
                re.I,
            )
        )

    def _collect_posts_from_json(
        self,
        payload: Any,
        posts_by_id: Dict[str, Dict[str, Any]],
        depth: int = 0,
    ) -> None:
        """Collect public post evidence from Facebook GraphQL response payloads."""
        if depth > 60:
            return
        if isinstance(payload, list):
            for item in payload:
                self._collect_posts_from_json(item, posts_by_id, depth + 1)
            return
        if not isinstance(payload, dict):
            return

        def content_author(node: Dict[str, Any]) -> tuple[str, str, Any]:
            values: List[Any] = [
                node.get("author"),
                node.get("actor"),
                node.get("owner"),
                node.get("owning_profile"),
                node.get("page"),
            ]
            actors = node.get("actors")
            if isinstance(actors, list):
                values.extend(actors[:3])
            for value in values:
                if not isinstance(value, dict):
                    continue
                name = self._text_from_rich_value(value.get("name") or value.get("title"))
                actor_id = str(value.get("id") or value.get("__id") or value.get("profile_id") or "")
                if name or actor_id:
                    return name, actor_id, value.get("is_verified")
            return "", "", None

        def metric(*keys: str) -> tuple[Optional[int], bool]:
            return self._metric_from_payload(payload, keys)

        candidate_urls: List[str] = []
        for key in (
            "url",
            "permalink_url",
            "permalinkUrl",
            "permalink",
            "story_url",
            "storyUrl",
            "www_url",
            "wwwURL",
            "video_url",
            "shareable_url",
        ):
            value = payload.get(key)
            if isinstance(value, str) and self._is_content_url(value):
                candidate_urls.append(value)
            elif isinstance(value, dict):
                nested_url = value.get("url") or value.get("href")
                if isinstance(nested_url, str) and self._is_content_url(nested_url):
                    candidate_urls.append(nested_url)

        for raw_url in candidate_urls:
            url = self._normalize_url(raw_url)
            content_id = self.extract_content_id(url)
            if not content_id:
                continue
            author, author_id, author_verified = content_author(payload)
            caption = self._comment_text(payload) or self._text_from_rich_value(
                payload.get("title") or payload.get("name") or payload.get("description")
            )
            if self._looks_like_internal_label(caption):
                caption = ""
            view_count, views_present = metric("video_view_count", "view_count", "play_count", "playCount", "views")
            like_count, likes_present = metric("reaction_count", "like_count", "reactors", "reaction_count_reduced")
            comment_count, comments_present = metric("comment_count", "comments_count", "total_comment_count")
            share_count, shares_present = metric("share_count", "shares")
            follower_count, followers_present = metric("followers_count", "follower_count", "fan_count")
            candidate = {
                "video_id": content_id,
                "url": url,
                "title": caption,
                "username": author or self._creator_from_url(url),
                "creator_id": author_id,
                "creator_verified": author_verified,
                "published_at": self._comment_time(payload),
                "content_type": str(payload.get("__typename") or "").lower(),
                "view_count": view_count,
                "like_count": like_count,
                "reported_comment_count": comment_count,
                "share_count": share_count,
                "follower_count": follower_count,
                "metric_availability": {
                    "views": "available" if views_present else "missing_from_public_response",
                    "likes": "available" if likes_present else "missing_from_public_response",
                    "reported_comments": "available" if comments_present else "missing_from_public_response",
                    "shares": "available" if shares_present else "missing_from_public_response",
                    "saves": "not_publicly_exposed",
                    "followers": "available" if followers_present else "missing_from_public_response",
                },
                "discovery_method": "facebook_graphql_response",
                "metadata_method": "facebook_graphql_response",
            }
            item = posts_by_id.setdefault(content_id, candidate)
            if not item.get("title") and caption:
                item["title"] = caption
            if not item.get("username") and author:
                item["username"] = author
            if not item.get("creator_id") and author_id:
                item["creator_id"] = author_id
            for key in ("published_at", "content_type", "view_count", "like_count", "reported_comment_count", "share_count", "follower_count"):
                if item.get(key) in (None, "", 0) and candidate.get(key) not in (None, ""):
                    item[key] = candidate.get(key)
            availability = item.setdefault("metric_availability", {})
            for metric_name, status in candidate["metric_availability"].items():
                if status == "available" or availability.get(metric_name) != "available":
                    availability[metric_name] = status

        for value in payload.values():
            if isinstance(value, (dict, list)):
                self._collect_posts_from_json(value, posts_by_id, depth + 1)

    def _stable_id(self, *parts: Any) -> str:
        payload = "\x1f".join(str(part or "") for part in parts)
        return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:24]

    def _parse_jsonish_payload(self, text: str) -> Any:
        text = (text or "").strip()
        if text.startswith("for (;;);"):
            text = text[len("for (;;);"):].strip()
        if not text:
            raise ValueError("empty response")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            payloads = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                payloads.append(json.loads(line))
            return payloads

    def _is_facebook_error_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("error") or payload.get("errorSummary") or payload.get("errorDescription"):
            return True
        return False

    def _looks_like_comment_graphql_body(self, post_data: str) -> bool:
        lowered = (post_data or "").lower()
        return (
            "commentslistcomponentspaginationquery" in lowered
            or "commentsaftercursor" in lowered
            or "comment_rendering_instance" in lowered
        )

    def _extract_comment_page_cursor(self, payload: Any) -> str:
        best_cursor = ""
        fallback_cursor = ""

        def walk(node: Any, path: str = "") -> None:
            nonlocal best_cursor, fallback_cursor
            if best_cursor:
                return
            if isinstance(node, dict):
                for key in ("page_info", "pageInfo"):
                    page_info = node.get(key)
                    if not isinstance(page_info, dict):
                        continue
                    cursor = page_info.get("end_cursor") or page_info.get("endCursor")
                    has_next = page_info.get("has_next_page")
                    if has_next is None:
                        has_next = page_info.get("hasNextPage")
                    if cursor and has_next is not False:
                        if "comment" in path.lower():
                            best_cursor = str(cursor)
                            return
                        if not fallback_cursor:
                            fallback_cursor = str(cursor)
                for key, value in node.items():
                    walk(value, f"{path}.{key}".strip("."))
            elif isinstance(node, list):
                for index, value in enumerate(node[:120]):
                    walk(value, f"{path}[{index}]")

        walk(payload)
        return best_cursor or fallback_cursor

    def _comment_request_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        blocked = {
            "content-length",
            "host",
        }
        return {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in blocked and value
        }

    def _comment_fetch_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        allowed = {
            "accept",
            "content-type",
            "x-asbd-id",
            "x-fb-friendly-name",
            "x-fb-lsd",
            "x-requested-with",
        }
        return {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() in allowed and value
        }

    async def _post_comment_api_from_page(self, page: Page, request_info: Dict[str, Any], post_data: str) -> Dict[str, Any]:
        headers = self._comment_fetch_headers(request_info.get("headers") or {})
        return await page.evaluate(
            """
            async ({url, headers, body}) => {
              const response = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers,
                body
              });
              return {
                status: response.status,
                ok: response.ok,
                text: await response.text()
              };
            }
            """,
            {
                "url": request_info.get("url"),
                "headers": headers,
                "body": post_data,
            },
        )

    def _update_comment_pagination_body(self, post_data: str, cursor: str, remaining: int = 0) -> str:
        fields = parse_qs(post_data or "", keep_blank_values=True)
        variables_raw = (fields.get("variables") or [""])[0]
        if not variables_raw:
            return post_data
        variables = json.loads(variables_raw)
        if not isinstance(variables, dict):
            return post_data
        variables["commentsAfterCursor"] = cursor
        override_count = os.environ.get("FACEBOOK_API_REPLAY_OVERRIDE_COUNT", "").strip().lower() in {"1", "true", "yes"}
        if override_count and remaining and remaining > 0:
            variables["commentsAfterCount"] = max(1, min(50, remaining))
        fields["variables"] = [json.dumps(variables, separators=(",", ":"))]
        fields["server_timestamps"] = fields.get("server_timestamps") or ["true"]
        return urlencode(fields, doseq=True)

    async def _replay_comment_api_pages(
        self,
        page: Page,
        api_state: Dict[str, Any],
        content_id: str,
        comments_by_id: Dict[str, Dict[str, Any]],
        max_comments: int,
    ) -> None:
        request_info = api_state.get("request") or {}
        cursor = str(api_state.get("next_cursor") or "")
        api_state.setdefault("replay_attempts", 0)
        api_state.setdefault("replay_new_comments", 0)
        api_state.setdefault("replay_error", "")
        if not request_info or not cursor:
            api_state["replay_error"] = "missing_request_or_cursor"
            return

        max_rounds = self._env_int("FACEBOOK_API_REPLAY_ROUNDS", 8)
        delay = self._env_float("FACEBOOK_API_REPLAY_DELAY_SECONDS", 1.0)
        seen_cursors: Set[str] = set()
        rounds_without_new = 0
        for _ in range(max_rounds):
            if not cursor or cursor in seen_cursors:
                break
            if max_comments and max_comments > 0 and len(comments_by_id) >= max_comments:
                break
            seen_cursors.add(cursor)
            before = len(comments_by_id)
            remaining = max(0, max_comments - before) if max_comments and max_comments > 0 else 50
            try:
                api_state["replay_attempts"] = int(api_state.get("replay_attempts") or 0) + 1
                post_data = self._update_comment_pagination_body(
                    request_info.get("post_data") or "",
                    cursor,
                    remaining,
                )
                response = await self._post_comment_api_from_page(page, request_info, post_data)
                if not response.get("ok"):
                    api_state["replay_error"] = f"http_status_{response.get('status')}"
                    break
                text = response.get("text") or ""
                payload = self._parse_jsonish_payload(text)
            except Exception as exc:
                api_state["replay_error"] = f"request_or_parse_failed: {exc}"
                self.logger.debug(f"Facebook browser API replay failed: {exc}")
                break

            if self._is_facebook_error_payload(payload):
                api_state["replay_error"] = "facebook_error_payload"
                self.logger.debug("Facebook browser API replay returned an error payload")
                break

            self._collect_comments_from_json(payload, content_id, comments_by_id)
            next_cursor = self._extract_comment_page_cursor(payload)
            api_state["next_cursor"] = next_cursor
            cursor = next_cursor
            api_state["replay_new_comments"] = int(api_state.get("replay_new_comments") or 0) + max(0, len(comments_by_id) - before)
            if len(comments_by_id) <= before:
                rounds_without_new += 1
            else:
                rounds_without_new = 0
            if rounds_without_new >= self._env_int("FACEBOOK_API_REPLAY_STALL_ROUNDS", 2):
                break
            await asyncio.sleep(delay)
        if not api_state.get("replay_error"):
            api_state["replay_error"] = ""

    async def _dismiss_popups(self, page: Page) -> None:
        labels = [
            "Allow all cookies",
            "Decline optional cookies",
            "Only allow essential cookies",
            "Not now",
            "Not Now",
            "Close",
        ]
        for label in labels:
            try:
                button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I)).first
                if await button.count():
                    await button.click(timeout=1500)
                    await asyncio.sleep(0.5)
            except Exception:
                continue

    async def _page_meta(self, page: Page) -> Dict[str, str]:
        try:
            return await page.evaluate(
                """
                () => {
                  const meta = {};
                  for (const el of document.querySelectorAll('meta[property], meta[name]')) {
                    const key = el.getAttribute('property') || el.getAttribute('name');
                    const content = el.getAttribute('content') || '';
                    if (key && content) meta[key] = content;
                  }
                  const canonical = document.querySelector('link[rel="canonical"]');
                  if (canonical) meta.url = canonical.href || '';
                  meta.title = document.title || '';
                  return meta;
                }
                """
            )
        except Exception:
            return {}

    def _caption_from_meta(self, meta: Dict[str, str]) -> str:
        return (
            meta.get("og:title")
            or meta.get("twitter:title")
            or meta.get("description")
            or meta.get("og:description")
            or meta.get("title")
            or "Facebook"
        ).strip()

    def _creator_from_url(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            parts = [part for part in parsed.path.split("/") if part]
            if not parts:
                return ""
            reserved = {"watch", "reel", "story.php", "groups", "videos", "posts", "permalink"}
            if parts[0].lower() in reserved:
                return ""
            return parts[0]
        except Exception:
            return ""

    def _text_from_rich_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return " ".join(
                part
                for part in (self._text_from_rich_value(item) for item in value)
                if part
            ).strip()
        if not isinstance(value, dict):
            return ""
        for key in ("text", "name", "title", "subtitle", "body", "message"):
            text = self._text_from_rich_value(value.get(key))
            if text:
                return text
        runs = value.get("ranges") or value.get("aggregated_ranges") or value.get("runs")
        if isinstance(runs, list):
            text = " ".join(self._text_from_rich_value(run) for run in runs).strip()
            if text:
                return text
        return ""

    def _int_from_value(self, value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            compact = value.replace(",", "").strip()
            if compact.isdigit():
                return int(compact)
        if isinstance(value, dict):
            for key in ("count", "total_count", "reaction_count", "like_count"):
                parsed = self._int_from_value(value.get(key))
                if parsed:
                    return parsed
        return 0

    def _optional_int_from_value(self, value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            text = value.replace("\u00a0", " ").strip().lower()
            match = re.search(r"(\d[\d.,]*)\s*(k|m|b|rb|ribu|jt|juta)?", text)
            if not match:
                return None
            number = match.group(1)
            suffix = match.group(2) or ""
            if suffix and "," in number and "." not in number:
                number = number.replace(",", ".")
            elif not suffix:
                number = re.sub(r"[.,](?=\d{3}(?:\D|$))", "", number)
            number = number.replace(",", "")
            try:
                numeric = float(number)
            except ValueError:
                return None
            multiplier = {
                "": 1,
                "k": 1_000,
                "rb": 1_000,
                "ribu": 1_000,
                "m": 1_000_000,
                "jt": 1_000_000,
                "juta": 1_000_000,
                "b": 1_000_000_000,
            }[suffix]
            return max(0, int(numeric * multiplier))
        if isinstance(value, dict):
            for key in ("total_count", "count", "reaction_count", "like_count", "value"):
                if key not in value:
                    continue
                parsed = self._optional_int_from_value(value.get(key))
                if parsed is not None:
                    return parsed
        return None

    def _metric_from_payload(
        self,
        payload: Dict[str, Any],
        keys: Iterable[str],
        max_depth: int = 10,
    ) -> tuple[Optional[int], bool]:
        queue: List[tuple[Any, int]] = [(payload, 0)]
        index = 0
        keys = tuple(keys)
        while index < len(queue):
            current, depth = queue[index]
            index += 1
            if not isinstance(current, dict):
                continue
            for key in keys:
                if key not in current:
                    continue
                parsed = self._optional_int_from_value(current.get(key))
                if parsed is not None:
                    return parsed, True
            if depth >= max_depth:
                continue
            for child_key, value in current.items():
                if str(child_key).casefold() in {"comments", "comment", "display_comments", "comment_rendering_instance"}:
                    continue
                if isinstance(value, dict):
                    queue.append((value, depth + 1))
                elif isinstance(value, list):
                    queue.extend((item, depth + 1) for item in value if isinstance(item, dict))
        return None, False

    def _looks_like_internal_label(self, value: str) -> bool:
        text = re.sub(r"\s+", " ", value or "").strip()
        if not text:
            return False
        if text.casefold() in {"facebook", "photo", "video", "story", "cometstorysections"}:
            return True
        return bool(re.fullmatch(r"(?:Comet|XDT|Polaris|Story)[A-Z][A-Za-z0-9_]+", text))

    def _comment_author(self, node: Dict[str, Any]) -> tuple[str, str]:
        for key in ("author", "commenter", "actor", "user", "profile", "owning_profile"):
            value = node.get(key)
            if not isinstance(value, dict):
                continue
            name = self._text_from_rich_value(value.get("name") or value.get("title"))
            author_id = str(value.get("id") or value.get("__id") or value.get("profile_id") or "")
            if name or author_id:
                return name or "Facebook User", author_id
        return "", ""

    def _comment_author_profile(self, node: Dict[str, Any]) -> Dict[str, Any]:
        for key in ("author", "commenter", "actor", "user", "profile", "owning_profile"):
            value = node.get(key)
            if not isinstance(value, dict):
                continue
            name = self._text_from_rich_value(value.get("name") or value.get("title"))
            user_id = str(value.get("id") or value.get("__id") or value.get("profile_id") or "")
            if name or user_id:
                picture = value.get("profile_picture") or value.get("picture") or {}
                if isinstance(picture, dict) and isinstance(picture.get("uri"), str):
                    avatar_url = picture.get("uri")
                elif isinstance(picture, dict) and isinstance(picture.get("url"), str):
                    avatar_url = picture.get("url")
                else:
                    avatar_url = ""
                return {
                    "user_id": user_id,
                    "username": value.get("username") or "",
                    "display_name": name,
                    "bio": self._text_from_rich_value(
                        value.get("about") or value.get("description")
                    ),
                    "avatar_url": avatar_url,
                    "verified": value.get("is_verified") or value.get("verified"),
                    "category": self._text_from_rich_value(value.get("category")),
                    "location": value.get("location") if isinstance(value.get("location"), dict) else "",
                }
        return {}

    def _comment_time(self, node: Dict[str, Any]) -> str:
        keys = (
            "created_time",
            "creation_time",
            "createdAt",
            "creationTime",
            "timestamp",
            "publish_time",
            "publishTime",
            "creation_timestamp",
        )
        queue: List[tuple[Any, int]] = [(node, 0)]
        index = 0
        while index < len(queue):
            current, depth = queue[index]
            index += 1
            if not isinstance(current, dict):
                continue
            for key in keys:
                value = current.get(key)
                if isinstance(value, (int, float)) and value > 1_000_000_000:
                    timestamp = float(value)
                    if timestamp > 10_000_000_000:
                        timestamp /= 1000
                    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).replace(
                        microsecond=0
                    ).isoformat()
                if isinstance(value, str) and value:
                    return value
            if depth >= 10:
                continue
            for child_key, value in current.items():
                if str(child_key).casefold() in {"comments", "comment", "display_comments", "comment_rendering_instance"}:
                    continue
                if isinstance(value, dict):
                    queue.append((value, depth + 1))
                elif isinstance(value, list):
                    queue.extend((item, depth + 1) for item in value if isinstance(item, dict))
        return ""

    def _comment_likes(self, node: Dict[str, Any]) -> int:
        for key in ("like_count", "reaction_count", "reactors", "feedback", "ufi_reaction_count"):
            parsed = self._int_from_value(node.get(key))
            if parsed:
                return parsed
        return 0

    def _store_text_comment(
        self,
        comments_by_id: Dict[str, Dict[str, Any]],
        content_id: str,
        author: str,
        text: str,
        *,
        author_id: str = "",
        likes: int = 0,
        created_time: str = "",
        is_reply: bool = False,
    ) -> None:
        text = re.sub(r"\s+", " ", text or "").strip()
        author = re.sub(r"\s+", " ", author or "").strip() or "Facebook User"
        if not text or len(text) > 3000:
            return
        lower_text = text.lower()
        if (
            lower_text.startswith("· follow")
            or "follow giphy" in lower_text
            or lower_text in {"follow", "giphy", "like comment share"}
        ):
            return
        if author.lower() in {"facebook", "meta", "log in", "login", "sign up"}:
            return
        comment_id = self._stable_id(content_id, author_id, author, text, created_time)
        if comment_id in comments_by_id:
            return
        comments_by_id[comment_id] = {
            "id": comment_id,
            "comment_id": comment_id,
            "text": text,
            "author": author,
            "author_id": author_id,
            "likes": likes,
            "likes_text": str(likes),
            "time": created_time,
            "platform": "facebook",
            "video_id": content_id,
            "reply_count": 0,
            "is_reply": is_reply,
        }

    def _comment_text(self, node: Dict[str, Any]) -> str:
        for key in ("body", "message", "text", "comet_sections"):
            value = node.get(key)
            if key == "comet_sections" and isinstance(value, dict):
                for section in value.values():
                    text = self._text_from_rich_value(section)
                    if text:
                        return text
                continue
            text = self._text_from_rich_value(value)
            if text:
                return text
        return ""

    def _looks_like_comment(self, node: Dict[str, Any], text: str, author: str) -> bool:
        typename = str(node.get("__typename") or node.get("__isNode") or "").lower()
        if "comment" in typename and text:
            return True
        keys = {str(key).lower() for key in node.keys()}
        has_comment_key = bool(keys & {"comment_id", "commenter", "created_time", "like_count"})
        if has_comment_key and text and author:
            return True
        if "feedback" in keys and text and author and ("id" in keys or "__typename" in keys):
            return True
        return False

    def _collect_comments_from_json(
        self,
        payload: Any,
        content_id: str,
        comments_by_id: Dict[str, Dict[str, Any]],
        depth: int = 0,
        is_reply: bool = False,
        parent_comment_id: str = "",
    ) -> None:
        if depth > 60:
            return
        if isinstance(payload, list):
            for item in payload:
                self._collect_comments_from_json(
                    item,
                    content_id,
                    comments_by_id,
                    depth + 1,
                    is_reply,
                    parent_comment_id,
                )
            return
        if not isinstance(payload, dict):
            return

        text = self._comment_text(payload)
        author, author_id = self._comment_author(payload)
        explicit_parent = payload.get("parent")
        if not isinstance(explicit_parent, dict):
            explicit_parent = payload.get("parent_comment")
        if not isinstance(explicit_parent, dict):
            explicit_parent = payload.get("reply_to")
        explicit_parent_id = (
            str(
                explicit_parent.get("id")
                or explicit_parent.get("__id")
                or explicit_parent.get("comment_id")
                or ""
            )
            if isinstance(explicit_parent, dict)
            else ""
        )
        explicit_parent_id = str(
            payload.get("parent_comment_id")
            or payload.get("reply_to_comment_id")
            or explicit_parent_id
            or parent_comment_id
            or ""
        )
        current_is_reply = bool(is_reply or explicit_parent_id)
        current_comment_id = ""
        if self._looks_like_comment(payload, text, author):
            current_comment_id = str(
                payload.get("comment_id")
                or payload.get("id")
                or payload.get("__id")
                or payload.get("legacy_fbid")
                or self._stable_id(content_id, author, text, self._comment_time(payload))
            )
            if current_comment_id not in comments_by_id and text:
                comments_by_id[current_comment_id] = {
                    "id": current_comment_id,
                    "comment_id": current_comment_id,
                    "parent_comment_id": explicit_parent_id,
                    "text": text,
                    "author": author or "Facebook User",
                    "author_id": author_id,
                    "author_profile": self._comment_author_profile(payload),
                    "likes": self._comment_likes(payload),
                    "likes_text": str(self._comment_likes(payload)),
                    "time": self._comment_time(payload),
                    "platform": "facebook",
                    "video_id": content_id,
                    "reply_count": self._int_from_value(payload.get("reply_count") or payload.get("replies")),
                    "is_reply": current_is_reply,
                }
            elif current_comment_id in comments_by_id and current_is_reply:
                existing = comments_by_id[current_comment_id]
                existing["is_reply"] = True
                if explicit_parent_id and not existing.get("parent_comment_id"):
                    existing["parent_comment_id"] = explicit_parent_id

        reply_container_keys = {
            "replies",
            "reply_comments",
            "comment_replies",
            "display_replies",
            "replies_connection",
            "child_comments",
        }
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                normalized_key = str(key).casefold()
                child_is_reply = (
                    current_is_reply
                    or normalized_key in reply_container_keys
                    or (
                        normalized_key == "display_comments"
                        and bool(current_comment_id)
                    )
                )
                child_parent_id = (
                    current_comment_id
                    if child_is_reply and current_comment_id
                    else explicit_parent_id
                )
                self._collect_comments_from_json(
                    value,
                    content_id,
                    comments_by_id,
                    depth + 1,
                    child_is_reply,
                    child_parent_id,
                )

    async def _extract_links_from_dom(self, page: Page) -> List[Dict[str, str]]:
        return await page.evaluate(
            """
            () => {
              const wanted = [
                /\\/watch\\/?\\?.*[?&]v=/,
                /\\/reel\\//,
                /\\/videos\\//,
                /\\/posts\\//,
                /\\/permalink\\//,
                /\\/groups\\/[^/]+\\/posts\\//,
                /\\/story\\.php\\?/,
                /\\/photo\\.php\\?/,
                /[?&](story_fbid|fbid)=/
              ];
              const seen = new Set();
              const results = [];
              for (const link of Array.from(document.querySelectorAll('a[href]'))) {
                let href = link.href || '';
                if (!href || !href.includes('facebook.com')) continue;
                if (/\\/search\\//.test(href) || /\\/notifications\\//.test(href)) continue;
                if (/[?&]notif_/.test(href) || /[?&]__rsidv2__=/.test(href)) continue;
                if (!wanted.some(pattern => pattern.test(href))) continue;
                href = href.split('#')[0];
                const text = (link.innerText || link.getAttribute('aria-label') || '').trim();
                if (/^Unread/i.test(text)) continue;
                const article = link.closest('[role="article"], article, div');
                const nearby = article ? (article.innerText || '').trim() : '';
                const title = (text || nearby || document.title || '').slice(0, 500);
                const key = href.replace(/([?&])(__cft__|__tn__|mibextid|ref|refid|eid)=[^&]*/g, '$1');
                if (seen.has(key)) continue;
                seen.add(key);
                results.push({url: href, title});
              }
              return results;
            }
            """
        )

    async def _extract_dom_comments(
        self,
        page: Page,
        content_id: str,
        comments_by_id: Dict[str, Dict[str, Any]],
    ) -> None:
        raw_comments = await page.evaluate(
            """
            () => {
              const ui = new Set([
                'like', 'reply', 'share', 'send', 'all comments', 'most relevant',
                'view more comments', 'view previous comments', 'see more', 'edited',
                'top comments', 'newest', 'oldest'
              ]);
              const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
              const rows = [];
              const candidates = Array.from(document.querySelectorAll(
                '[role="article"], div[aria-label*="Comment"], div[aria-label*="comment"], div[id*="comment"], div[data-sigil*="comment"], li'
              ));
              const seen = new Set();
              for (const article of candidates) {
                const raw = article.innerText || article.textContent || '';
                if (!raw || raw.length > 5000) continue;
                const label = article.getAttribute('aria-label') || '';
                if (!/comment/i.test(label + ' ' + raw) && !/\\b(like|reply|suka|balas)\\b/i.test(raw)) continue;
                const lines = raw.split(/\\n|\\r/).map(clean).filter(Boolean);
                const filtered = lines.filter(line => {
                  const lower = line.toLowerCase();
                  if (ui.has(lower)) return false;
                  if (/^\\d+[smhdw]$/.test(lower)) return false;
                  if (/^\\d+$/.test(lower)) return false;
                  return true;
                });
                if (filtered.length < 2) continue;
                const author = filtered[0].slice(0, 160);
                const text = filtered.slice(1, 5).join(' ').slice(0, 2000);
                if (!text || text.length < 2) continue;
                const key = `${author}\\u001f${text}`;
                if (seen.has(key)) continue;
                seen.add(key);
                rows.push({author, text});
              }
              return rows;
            }
            """
        )
        for raw in raw_comments:
            self._store_text_comment(
                comments_by_id,
                content_id,
                raw.get("author") or "Facebook User",
                raw.get("text") or "",
            )

    def _basic_url_variants(self, url: str) -> List[str]:
        parsed = urlparse(self._normalize_url(url))
        if not parsed.netloc:
            return []
        variants = []
        for host in ("mbasic.facebook.com", "m.facebook.com"):
            variants.append(urlunparse(("https", host, parsed.path, "", parsed.query, "")))
        return variants

    async def _extract_basic_dom_comments(
        self,
        page: Page,
        content_id: str,
        comments_by_id: Dict[str, Dict[str, Any]],
    ) -> None:
        raw_comments = await page.evaluate(
            """
            () => {
              const uiPatterns = [
                /^like$/i, /^reply$/i, /^share$/i, /^suka$/i, /^balas$/i,
                /^edited$/i, /^see translation$/i, /^terjemahkan$/i,
                /^view more comments$/i, /^view previous comments$/i,
                /^see more comments$/i, /^more comments$/i,
                /^log in$/i, /^sign up$/i, /^create new account$/i,
                /^\\d+[smhdw]$/i, /^\\d+$/
              ];
              const actionPattern = /\\b(like|reply|suka|balas|edited|replies|comments?)\\b/i;
              const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
              const rows = [];
              const candidates = Array.from(document.querySelectorAll(
                'div[id*="comment"], div[id^="ufi_"], div[data-sigil*="comment"], article, li'
              ));
              const seen = new Set();
              for (const node of candidates) {
                const raw = node.innerText || node.textContent || '';
                if (!raw || raw.length > 5000 || !actionPattern.test(raw)) continue;
                const lines = raw.split(/\\n|\\r/).map(clean).filter(Boolean);
                const filtered = lines.filter(line => !uiPatterns.some(pattern => pattern.test(line)));
                if (filtered.length < 2) continue;
                const author = filtered[0].slice(0, 160);
                const text = filtered.slice(1, 6).join(' ').slice(0, 2500);
                const key = `${author}\\u001f${text}`;
                if (!text || seen.has(key)) continue;
                seen.add(key);
                rows.push({author, text});
              }
              return rows;
            }
            """
        )
        for raw in raw_comments:
            self._store_text_comment(
                comments_by_id,
                content_id,
                raw.get("author") or "Facebook User",
                raw.get("text") or "",
            )

    async def _next_basic_comments_url(self, page: Page) -> str:
        try:
            return await page.evaluate(
                """
                () => {
                  const patterns = [
                    /view more comments/i,
                    /view previous comments/i,
                    /see more comments/i,
                    /more comments/i,
                    /view more replies/i,
                    /see more replies/i
                  ];
                  for (const link of Array.from(document.querySelectorAll('a[href]'))) {
                    const text = (link.innerText || link.textContent || '').trim();
                    if (patterns.some(pattern => pattern.test(text))) return link.href || '';
                  }
                  return '';
                }
                """
            )
        except Exception:
            return ""

    async def _extract_basic_comments(
        self,
        page: Page,
        url: str,
        content_id: str,
        comments_by_id: Dict[str, Dict[str, Any]],
        max_comments: int,
    ) -> None:
        max_rounds = self._env_int("FACEBOOK_BASIC_COMMENT_ROUNDS", 4)
        for basic_url in self._basic_url_variants(url):
            try:
                await page.goto(basic_url, wait_until="domcontentloaded", timeout=45000)
                await self._dismiss_popups(page)
                await asyncio.sleep(self._env_float("FACEBOOK_BASIC_WAIT_SECONDS", 1.5))
            except Exception as exc:
                self.logger.debug(f"Facebook basic fallback failed to load {basic_url}: {exc}")
                continue

            visited = {page.url}
            for _ in range(max_rounds):
                before = len(comments_by_id)
                await self._extract_basic_dom_comments(page, content_id, comments_by_id)
                if max_comments and max_comments > 0 and len(comments_by_id) >= max_comments:
                    return
                next_url = await self._next_basic_comments_url(page)
                if not next_url or next_url in visited:
                    break
                visited.add(next_url)
                try:
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(self._env_float("FACEBOOK_BASIC_WAIT_SECONDS", 1.0))
                except Exception:
                    break
                if len(comments_by_id) == before and len(visited) > 2:
                    break
            if comments_by_id:
                return

    async def _wait_for_response_tasks(self, response_tasks: Set[asyncio.Task], timeout: float = 3.0) -> None:
        if not response_tasks:
            return
        try:
            await asyncio.wait(list(response_tasks), timeout=timeout)
        except Exception:
            return

    async def _load_more_comments(
        self,
        page: Page,
        max_rounds: int,
        *,
        comments_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
        max_comments: int = 0,
        response_tasks: Optional[Set[asyncio.Task]] = None,
        api_only: bool = False,
    ) -> None:
        patterns = [
            r"^comments?$",
            r"^comment$",
            r"^komentar$",
            r"view more comments",
            r"view previous comments",
            r"see more comments",
            r"lihat komentar lainnya",
            r"lihat komentar sebelumnya",
            r"lihat komentar lain",
            r"komentar lainnya",
            r"view more replies",
            r"see more replies",
            r"view .* replies",
            r"lihat .* balasan",
            r"more comments",
            r"all comments",
            r"semua komentar",
            r"most relevant",
            r"paling relevan",
            r"newest",
            r"terbaru",
        ]
        stall_limit = self._env_int("FACEBOOK_COMMENT_STALL_ROUNDS", 3 if api_only else 2)
        rounds_without_new = 0
        min_rounds = min(3, max_rounds) if api_only else 1

        for round_index in range(max_rounds):
            before = len(comments_by_id or {})
            clicked = False
            for pattern in patterns:
                try:
                    button = page.get_by_role("button", name=re.compile(pattern, re.I)).first
                    if await button.count():
                        await button.click(timeout=1500)
                        clicked = True
                        await asyncio.sleep(1)
                except Exception:
                    pass
                try:
                    text = page.get_by_text(re.compile(pattern, re.I)).first
                    if await text.count():
                        await text.click(timeout=1500)
                        clicked = True
                        await asyncio.sleep(1)
                except Exception:
                    pass
            try:
                await page.evaluate(
                    """
                    () => {
                      const scrollables = Array.from(document.querySelectorAll('div, main, section, aside, [role="dialog"]'))
                        .filter(el => el.scrollHeight > el.clientHeight + 200)
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                      for (const el of scrollables.slice(0, 8)) {
                        el.scrollTop = el.scrollHeight;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                      }
                      const commentish = scrollables.filter(el => {
                        const label = (el.getAttribute('aria-label') || '').toLowerCase();
                        const text = (el.innerText || '').toLowerCase();
                        return label.includes('comment') || label.includes('komentar') || text.includes('view more comments') || text.includes('lihat komentar');
                      });
                      for (const el of commentish.slice(0, 5)) {
                        el.scrollTop = el.scrollHeight;
                        el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 1800 }));
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                      }
                    }
                    """
                )
            except Exception:
                pass
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(1.2 if clicked else 1.8)
            if response_tasks is not None:
                await self._wait_for_response_tasks(response_tasks, self._env_float("FACEBOOK_API_RESPONSE_WAIT_SECONDS", 2.5))

            current = len(comments_by_id or {})
            if max_comments and max_comments > 0 and current >= max_comments:
                break
            if comments_by_id is not None:
                if current <= before:
                    rounds_without_new += 1
                else:
                    rounds_without_new = 0
                if round_index + 1 >= min_rounds and rounds_without_new >= stall_limit:
                    break

    def _graph_token(self) -> str:
        return (os.environ.get("FACEBOOK_GRAPH_TOKEN") or os.environ.get("FB_GRAPH_TOKEN") or "").strip()

    def _graph_version(self) -> str:
        version = (os.environ.get("FACEBOOK_GRAPH_VERSION") or "v23.0").strip().lstrip("/")
        return version or "v23.0"

    def _normalize_graph_comment(self, raw: Dict[str, Any], content_id: str) -> Dict[str, Any]:
        author = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        comment_id = str(raw.get("id") or self._stable_id(content_id, author.get("name"), raw.get("message")))
        parent = raw.get("parent") if isinstance(raw.get("parent"), dict) else {}
        return {
            "id": comment_id,
            "comment_id": comment_id,
            "parent_comment_id": str(parent.get("id") or ""),
            "text": raw.get("message") or raw.get("text") or "",
            "author": author.get("name") or "Facebook User",
            "author_id": str(author.get("id") or ""),
            "author_profile": {
                "user_id": author.get("id") or "",
                "username": author.get("username") or "",
                "display_name": author.get("name") or "",
            },
            "likes": self._int_from_value(raw.get("like_count")),
            "likes_text": str(self._int_from_value(raw.get("like_count"))),
            "time": raw.get("created_time") or "",
            "platform": "facebook",
            "video_id": content_id,
            "reply_count": self._int_from_value(raw.get("comment_count")),
            "is_reply": bool(parent.get("id")),
        }

    def _extract_comments_graph_api_sync(
        self,
        video_data: Dict[str, Any],
        max_comments: int = 100,
    ) -> Dict[str, Any]:
        token = self._graph_token()
        if not token:
            return {}

        import requests

        url = self._normalize_url(video_data.get("url") or "")
        content_id = (
            str(video_data.get("graph_id") or "")
            or str(video_data.get("video_id") or "")
            or self.extract_content_id(url, fallback=False)
        )
        if not content_id:
            return {}

        base_url = f"https://graph.facebook.com/{self._graph_version()}/{content_id}"
        page_size = max(1, min(100, max_comments if max_comments and max_comments > 0 else 100))
        fields = (
            "id,message,description,created_time,permalink_url,from{name,id},"
            f"comments.limit({page_size}).order(reverse_chronological)"
            ".summary(true){id,message,created_time,from{name,id},like_count,comment_count,parent{id}},"
            "reactions.limit(0).summary(true),shares"
        )
        params = {
            "fields": fields,
            "access_token": token,
        }
        session = requests.Session()
        response = session.get(base_url, params=params, timeout=30)
        payload = response.json()
        if response.status_code >= 400 or "error" in payload:
            error = payload.get("error") if isinstance(payload, dict) else {}
            message = error.get("message") if isinstance(error, dict) else str(payload)
            return {
                "video_id": content_id,
                "url": url,
                "caption": video_data.get("title", ""),
                "username": video_data.get("username", "Facebook User"),
                "comments": [],
                "error": f"graph_api: {message}",
            }

        comments_payload = payload.get("comments") if isinstance(payload.get("comments"), dict) else {}
        comments_summary = comments_payload.get("summary") if isinstance(comments_payload.get("summary"), dict) else {}
        reactions = payload.get("reactions") if isinstance(payload.get("reactions"), dict) else {}
        reactions_summary = reactions.get("summary") if isinstance(reactions.get("summary"), dict) else {}
        shares = payload.get("shares") if isinstance(payload.get("shares"), dict) else {}
        reported_comment_count = self._optional_int_from_value(comments_summary.get("total_count"))
        like_count = self._optional_int_from_value(reactions_summary.get("total_count"))
        share_count = self._optional_int_from_value(shares.get("count"))
        comments = [
            self._normalize_graph_comment(comment, content_id)
            for comment in comments_payload.get("data", [])
            if isinstance(comment, dict) and (comment.get("message") or comment.get("text"))
        ]
        next_url = (comments_payload.get("paging") or {}).get("next")
        while next_url and (not max_comments or len(comments) < max_comments):
            remaining = max_comments - len(comments) if max_comments and max_comments > 0 else 100
            if remaining <= 0:
                break
            response = session.get(next_url, timeout=30)
            page = response.json()
            if response.status_code >= 400 or "error" in page:
                break
            for comment in page.get("data", []):
                if not isinstance(comment, dict) or not (comment.get("message") or comment.get("text")):
                    continue
                comments.append(self._normalize_graph_comment(comment, content_id))
                if max_comments and len(comments) >= max_comments:
                    break
            next_url = (page.get("paging") or {}).get("next")

        comments_seen_in_response = len(comments)
        comment_limit_reached = bool(
            max_comments and max_comments > 0 and comments_seen_in_response >= max_comments
        )
        comments_exhausted = not bool(next_url)
        if max_comments and max_comments > 0:
            comments = comments[:max_comments]

        caption = payload.get("description") or payload.get("message") or video_data.get("title") or "Facebook"
        author = payload.get("from") if isinstance(payload.get("from"), dict) else {}
        return {
            "video_id": content_id,
            "url": payload.get("permalink_url") or url,
            "caption": caption,
            "username": author.get("name") or video_data.get("username") or "Facebook User",
            "creator_id": author.get("id") or video_data.get("creator_id") or "",
            "creator_profile": {
                "user_id": author.get("id") or video_data.get("creator_id") or "",
                "username": author.get("username") or video_data.get("username") or "",
                "display_name": author.get("name") or video_data.get("username") or "",
            },
            "published_at": payload.get("created_time") or video_data.get("published_at") or "",
            "content_type": video_data.get("content_type") or "post",
            "view_count": video_data.get("view_count"),
            "like_count": like_count if like_count is not None else video_data.get("like_count"),
            "reported_comment_count": (
                reported_comment_count
                if reported_comment_count is not None
                else video_data.get("reported_comment_count")
            ),
            "share_count": share_count if share_count is not None else video_data.get("share_count"),
            "follower_count": video_data.get("follower_count"),
            "metric_availability": {
                **dict(video_data.get("metric_availability") or {}),
                "likes": "available" if like_count is not None else (video_data.get("metric_availability") or {}).get("likes", "missing_from_public_response"),
                "reported_comments": "available" if reported_comment_count is not None else (video_data.get("metric_availability") or {}).get("reported_comments", "missing_from_public_response"),
                "shares": "available" if share_count is not None else (video_data.get("metric_availability") or {}).get("shares", "missing_from_public_response"),
                "saves": "not_publicly_exposed",
            },
            "comments": comments,
            "error": "",
            "source": "graph_api",
            "metadata_method": "facebook_graph_api",
            "comment_method": "facebook_graph_api",
            "comments_seen_in_response": comments_seen_in_response,
            "comment_limit_reached": comment_limit_reached,
            "comments_exhausted": comments_exhausted,
        }

    async def search(
        self,
        query: str,
        max_videos: int = 10,
        context: Optional[BrowserContext] = None,
    ) -> List[Dict[str, Any]]:
        from ..raw_contract import transport_mode

        self.logger.info(f"Searching Facebook for '{query}'...")
        max_posts = max_videos if max_videos and max_videos > 0 else 0
        api_only = transport_mode() == "api-only"

        if self.is_facebook_url(query) and self.extract_content_id(query):
            url = self._normalize_url(query)
            content_id = self.extract_content_id(url)
            return [{
                "video_id": content_id,
                "url": url,
                "title": "Direct Facebook URL",
                "username": self._creator_from_url(url),
                "discovery_method": "direct_url",
                "metadata_method": "facebook_direct_url",
            }]

        manage_context = context is None
        playwright = browser = None
        if manage_context:
            playwright, browser, context = await self._new_context()
        page = await context.new_page()
        response_tasks: Set[asyncio.Task] = set()
        api_posts_by_id: Dict[str, Dict[str, Any]] = {}

        async def capture_search_response(response) -> None:
            if "facebook.com" not in response.url or "/api/graphql" not in response.url:
                return
            try:
                payload = await response.json()
            except Exception:
                try:
                    payload = self._parse_jsonish_payload(await response.text())
                except Exception:
                    return
            self._collect_posts_from_json(payload, api_posts_by_id)

        def schedule_search_response(response) -> None:
            task = asyncio.create_task(capture_search_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", schedule_search_response)
        try:
            posts: List[Dict[str, Any]] = []
            seen: Set[str] = set()
            max_rounds = self._env_int("FACEBOOK_SEARCH_SCROLL_ROUNDS", 24 if not max_posts else 10)
            stall_limit = self._env_int("FACEBOOK_SEARCH_STALL_ROUNDS", 4 if not max_posts else 3)

            for search_url in self._search_urls_for_query(query):
                self.logger.info(f"Searching Facebook surface: {search_url}")
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                    await self._dismiss_popups(page)
                    await asyncio.sleep(self._env_float("FACEBOOK_INITIAL_WAIT_SECONDS", 3.0))
                except Exception as exc:
                    self.logger.warning(f"Facebook search surface failed: {search_url}: {exc}")
                    continue

                rounds_without_new = 0
                for _ in range(max_rounds):
                    found = [] if api_only else await self._extract_links_from_dom(page)
                    found = [*api_posts_by_id.values(), *found]
                    before = len(posts)
                    for raw in found:
                        url = self._normalize_url(raw.get("url", ""))
                        content_id = self.extract_content_id(url, fallback=False)
                        if not url or not content_id:
                            continue
                        if content_id in seen:
                            if raw.get("discovery_method") == "facebook_graphql_response":
                                existing = next(
                                    (post for post in posts if post.get("video_id") == content_id),
                                    None,
                                )
                                if existing is not None:
                                    for key, value in raw.items():
                                        if value not in (None, "", 0, [], {}):
                                            existing[key] = value
                                    existing["discovery_method"] = "facebook_graphql_response"
                                    existing["metadata_method"] = "facebook_graphql_response"
                            continue
                        seen.add(content_id)
                        posts.append({
                            **raw,
                            "video_id": content_id,
                            "url": url,
                            "title": raw.get("title", ""),
                            "username": raw.get("username") or self._creator_from_url(url),
                            "search_surface": search_url,
                            "discovery_method": raw.get("discovery_method") or "browser_dom",
                            "metadata_method": raw.get("metadata_method") or "browser_dom_card",
                        })
                        if max_posts and len(posts) >= max_posts:
                            break
                    if max_posts and len(posts) >= max_posts:
                        break
                    rounds_without_new = rounds_without_new + 1 if len(posts) == before else 0
                    if rounds_without_new >= stall_limit:
                        break
                    await page.mouse.wheel(0, 1800)
                    await asyncio.sleep(self._env_float("FACEBOOK_SEARCH_SCROLL_DELAY_SECONDS", 1.5))
                if max_posts and len(posts) >= max_posts:
                    break

            if response_tasks:
                _, pending = await asyncio.wait(response_tasks, timeout=10)
                for task in pending:
                    task.cancel()
            for content_id, raw in api_posts_by_id.items():
                if content_id in seen:
                    continue
                seen.add(content_id)
                posts.append(raw)
                if max_posts and len(posts) >= max_posts:
                    break

            self.logger.info(f"Found {len(posts)} Facebook posts for query '{query}'")
            if api_only and not posts:
                raise RuntimeError("Facebook API-only discovery captured no GraphQL post records")
            return posts
        finally:
            for task in list(response_tasks):
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
        url = self._normalize_url(video_data.get("url") or "")
        content_id = video_data.get("video_id") or self.extract_content_id(url, fallback=True)
        extraction_mode = self._extraction_mode()
        self.logger.info(f"Extracting comments from Facebook post {content_id or url}...")

        if extraction_mode != "dom" and self._graph_token():
            try:
                graph_result = await asyncio.to_thread(
                    self._extract_comments_graph_api_sync,
                    video_data,
                    max_comments,
                )
                if graph_result and not graph_result.get("error"):
                    self.logger.info(
                        f"Facebook Graph API fetched {len(graph_result.get('comments', []))} comments"
                    )
                    return graph_result
                if graph_result and graph_result.get("error"):
                    self.logger.warning(
                        f"Facebook Graph API failed; falling back to browser: {graph_result.get('error')}"
                    )
            except Exception as exc:
                self.logger.warning(f"Facebook Graph API failed; falling back to browser: {exc}")

        comments_by_id: Dict[str, Dict[str, Any]] = {}
        response_tasks: Set[asyncio.Task] = set()
        api_state: Dict[str, Any] = {
            "request": {},
            "next_cursor": "",
            "candidate_request_count": 0,
            "comment_response_count": 0,
            "cursor_update_count": 0,
            "locked_feedback_id": None,
        }

        manage_context = context is None
        playwright = browser = None
        if manage_context:
            playwright, browser, context = await self._new_context()
        page = await context.new_page()

        def capture_request(request) -> None:
            try:
                if "/api/graphql" not in request.url:
                    return
                post_data = request.post_data or ""
                if not self._looks_like_comment_graphql_body(post_data):
                    return
                api_state["candidate_request_count"] = int(api_state.get("candidate_request_count") or 0) + 1
                api_state["request"] = {
                    "url": request.url,
                    "post_data": post_data,
                    "headers": self._comment_request_headers(request.headers),
                }
            except Exception:
                return

        async def capture_response(response) -> None:
            response_url = response.url
            if "facebook.com" not in response_url:
                return
            if not any(marker in response_url for marker in ("/api/graphql", "/ajax/", "/ufi/", "/comments/")):
                return
            try:
                payload = await response.json()
            except Exception:
                try:
                    payload = self._parse_jsonish_payload(await response.text())
                except Exception:
                    return
            try:
                request = response.request
                post_data = request.post_data or ""
                if post_data:
                    import urllib.parse
                    import json
                    parsed = urllib.parse.parse_qs(post_data)
                    if "variables" in parsed:
                        variables = json.loads(parsed["variables"][0])
                        feedback_id = variables.get("feedbackID") or variables.get("targetID") or variables.get("feedback_id")
                        if feedback_id:
                            locked = api_state.get("locked_feedback_id")
                            if not locked:
                                api_state["locked_feedback_id"] = feedback_id
                                locked = feedback_id
                            if locked and locked != feedback_id:
                                return
            except Exception:
                pass

            before_count = len(comments_by_id)
            self._collect_comments_from_json(payload, content_id, comments_by_id)
            added_comments = len(comments_by_id) > before_count
            try:
                request = response.request
                post_data = request.post_data or ""
                cursor = self._extract_comment_page_cursor(payload)
                is_comment_request = self._looks_like_comment_graphql_body(post_data)
                if added_comments:
                    api_state["comment_response_count"] = int(api_state.get("comment_response_count") or 0) + 1
                if post_data and (is_comment_request or added_comments or cursor):
                    api_state["request"] = {
                        "url": request.url,
                        "post_data": post_data,
                        "headers": self._comment_request_headers(request.headers),
                    }
                if cursor:
                    api_state["next_cursor"] = cursor
                    api_state["cursor_update_count"] = int(api_state.get("cursor_update_count") or 0) + 1
            except Exception:
                return

        def schedule_capture(response) -> None:
            task = asyncio.create_task(capture_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        if extraction_mode != "dom":
            page.on("request", capture_request)
            page.on("response", schedule_capture)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self._dismiss_popups(page)
            await asyncio.sleep(self._env_float("FACEBOOK_POST_INITIAL_WAIT_SECONDS", 3.0))

            meta = await self._page_meta(page)
            canonical_url = self._normalize_url(meta.get("url") or url)
            content_id = content_id or self.extract_content_id(canonical_url, fallback=True)
            caption = video_data.get("caption") or video_data.get("title") or self._caption_from_meta(meta)
            username = video_data.get("username") or self._creator_from_url(canonical_url) or "Facebook User"

            if extraction_mode == "browser-api":
                default_api_rounds = 30 if not max_comments or max_comments <= 0 else max(8, min(30, (max_comments // 10) + 6))
                rounds = self._env_int("FACEBOOK_API_COMMENT_ROUNDS", default_api_rounds)
            else:
                rounds = 10 if not max_comments or max_comments <= 0 else max(2, min(10, (max_comments // 25) + 1))
            await self._load_more_comments(
                page,
                rounds,
                comments_by_id=comments_by_id if extraction_mode != "dom" else None,
                max_comments=max_comments,
                response_tasks=response_tasks if extraction_mode != "dom" else None,
                api_only=extraction_mode == "browser-api",
            )

            if response_tasks:
                _, pending = await asyncio.wait(response_tasks, timeout=10)
                for task in pending:
                    task.cancel()

            if (
                extraction_mode != "dom"
                and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments)
            ):
                await self._replay_comment_api_pages(
                    page,
                    api_state,
                    content_id,
                    comments_by_id,
                    max_comments,
                )

            browser_api_count = len(comments_by_id)

            if (
                extraction_mode != "browser-api"
                and (not comments_by_id or not max_comments or len(comments_by_id) < max_comments)
            ):
                await self._extract_dom_comments(page, content_id, comments_by_id)

            if (
                extraction_mode != "browser-api"
                and (not comments_by_id or not max_comments or len(comments_by_id) < max_comments)
            ):
                await self._extract_basic_comments(
                    page,
                    canonical_url,
                    content_id,
                    comments_by_id,
                    max_comments,
                )

            comments_seen_in_response = len(comments_by_id)
            comments = list(comments_by_id.values())
            if max_comments and max_comments > 0:
                comments = comments[:max_comments]

            self.logger.info(f"Extracted {len(comments)} Facebook comments from {canonical_url}")
            source = "browser_api" if browser_api_count and extraction_mode == "browser-api" else extraction_mode
            dom_comments_used = extraction_mode != "browser-api" and comments_seen_in_response > browser_api_count
            comment_method = (
                "facebook_browser_graphql"
                if browser_api_count or extraction_mode == "browser-api"
                else "browser_dom"
            )
            comment_limit_reached = bool(
                max_comments and max_comments > 0 and comments_seen_in_response >= max_comments
            )
            comments_exhausted = (
                not bool(api_state.get("next_cursor"))
                if int(api_state.get("candidate_request_count") or 0) > 0
                else None
            )
            return {
                "video_id": content_id,
                "url": canonical_url,
                "caption": caption,
                "username": username,
                "creator_id": video_data.get("creator_id") or "",
                "published_at": video_data.get("published_at") or video_data.get("published") or "",
                "content_type": video_data.get("content_type") or "post",
                "view_count": video_data.get("view_count"),
                "like_count": video_data.get("like_count"),
                "reported_comment_count": video_data.get("reported_comment_count"),
                "share_count": video_data.get("share_count"),
                "save_count": video_data.get("save_count"),
                "follower_count": video_data.get("follower_count"),
                "metric_availability": dict(video_data.get("metric_availability") or {}),
                "comments": comments,
                "error": "",
                "source": source,
                "discovery_method": video_data.get("discovery_method", "direct_url"),
                "metadata_method": video_data.get("metadata_method") or "facebook_page_meta",
                "comment_method": comment_method,
                "comments_seen_in_response": comments_seen_in_response,
                "comment_limit_reached": comment_limit_reached,
                "comments_exhausted": comments_exhausted,
                "fallback_used": dom_comments_used,
                "browser_api_comment_count": browser_api_count,
                "browser_api_replay_attempts": int(api_state.get("replay_attempts") or 0),
                "browser_api_replay_new_comments": int(api_state.get("replay_new_comments") or 0),
                "browser_api_replay_error": api_state.get("replay_error", ""),
                "browser_api_cursor_present": bool(api_state.get("next_cursor")),
                "browser_api_candidate_request_count": int(api_state.get("candidate_request_count") or 0),
                "browser_api_comment_response_count": int(api_state.get("comment_response_count") or 0),
                "browser_api_cursor_update_count": int(api_state.get("cursor_update_count") or 0),
            }
        except Exception as e:
            self.logger.error(f"Error extracting Facebook comments for {url}: {e}")
            return {
                "video_id": content_id,
                "url": url,
                "caption": video_data.get("title", ""),
                "username": video_data.get("username", "Facebook User"),
                "comments": [],
                "error": str(e),
            }
        finally:
            await page.close()
            if manage_context and not self._external_context:
                await context.close()
                if browser:
                    await browser.close()
                await playwright.stop()
            elif manage_context and playwright:
                await playwright.stop()

    def format_output(self, raw_comments: List[Any]) -> List[Dict[str, Any]]:
        return raw_comments
