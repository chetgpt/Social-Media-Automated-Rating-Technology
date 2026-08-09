from typing import Any, Dict, Iterable, List, Optional, Set
from .base_scraper import BaseScraper
from playwright.async_api import BrowserContext, Page, async_playwright
from urllib.parse import parse_qs, quote, urlencode, urlparse
import asyncio
import hashlib
import json
import os
import re


class InstagramScraper(BaseScraper):
    POST_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
    INSTAGRAM_HOST_RE = re.compile(r"(^|\.)instagram\.com$", re.I)
    POST_ROOT_DOC_ID = "28000200952919027"
    COMMENTS_DOC_ID = "26864966453197043"

    def __init__(self):
        super().__init__("instagram")
        self._last_graphql_template: Optional[Dict[str, Any]] = None
        self._external_context = False
        self._cdp_browser = None
        self._profile_metrics_cache: Dict[str, Dict[str, Any]] = {}
        self._profile_metrics_requests = 0

    def extract_shortcode(self, url: str) -> str:
        match = self.POST_RE.search(url or "")
        return match.group(1) if match else ""

    def shortcode_to_media_id(self, shortcode: str) -> Optional[str]:
        shortcode = (shortcode or "").strip()
        if not shortcode:
            return None
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        value = 0
        for char in shortcode:
            index = alphabet.find(char)
            if index < 0:
                return None
            value = value * 64 + index
        return str(value)

    def is_instagram_url(self, value: str) -> bool:
        try:
            parsed = urlparse(value)
            return bool(parsed.netloc and self.INSTAGRAM_HOST_RE.search(parsed.netloc))
        except Exception:
            return False

    def is_post_url(self, value: str) -> bool:
        return bool(self.extract_shortcode(value))

    def _storage_state_path(self) -> Optional[str]:
        path = os.environ.get("INSTAGRAM_STORAGE_STATE") or os.environ.get("IG_STORAGE_STATE")
        if path and os.path.exists(path):
            return path
        return None

    def _user_data_dir(self) -> Optional[str]:
        path = os.environ.get("INSTAGRAM_USER_DATA_DIR") or os.environ.get("IG_USER_DATA_DIR")
        if path:
            os.makedirs(path, exist_ok=True)
            return path
        return None

    def _headless(self) -> bool:
        value = os.environ.get("INSTAGRAM_HEADLESS", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _profile_directory(self) -> Optional[str]:
        value = os.environ.get("INSTAGRAM_PROFILE_DIRECTORY") or os.environ.get("IG_PROFILE_DIRECTORY")
        return value.strip() if value and value.strip() else None

    def _browser_channel(self) -> Optional[str]:
        value = os.environ.get("INSTAGRAM_BROWSER_CHANNEL") or os.environ.get("IG_BROWSER_CHANNEL")
        return value.strip() if value and value.strip() else None

    def _cdp_url(self) -> Optional[str]:
        value = os.environ.get("INSTAGRAM_CDP_URL") or os.environ.get("IG_CDP_URL")
        return value.strip() if value and value.strip() else None

    def _user_agent(self) -> str:
        value = os.environ.get("INSTAGRAM_USER_AGENT") or os.environ.get("IG_USER_AGENT")
        if value and value.strip():
            return value.strip()
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

    def _viewport(self) -> Dict[str, int]:
        width = self._env_int("INSTAGRAM_VIEWPORT_WIDTH", 1366, 320)
        height = self._env_int("INSTAGRAM_VIEWPORT_HEIGHT", 900, 320)
        return {"width": width, "height": height}

    def _env_int(self, name: str, default: int, minimum: int = 1) -> int:
        value = os.environ.get(name, "").strip()
        if not value:
            return default
        try:
            return max(minimum, int(value))
        except ValueError:
            self.logger.warning(f"Ignoring invalid {name}={value!r}")
            return default

    async def _new_context(self):
        playwright = await async_playwright().start()
        self._external_context = False
        self._cdp_browser = None
        kwargs = {
            "viewport": self._viewport(),
            "user_agent": self._user_agent(),
            "locale": "en-US",
        }

        cdp_url = self._cdp_url()
        if cdp_url:
            self.logger.info(f"Connecting to existing Instagram browser over CDP: {cdp_url}")
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            self._external_context = True
            self._cdp_browser = browser
            if browser.contexts:
                return playwright, None, browser.contexts[0]
            context = await browser.new_context(**kwargs)
            return playwright, None, context

        user_data_dir = self._user_data_dir()
        if user_data_dir:
            self.logger.info(f"Using Instagram persistent browser profile at {user_data_dir}")
            args = []
            launch_kwargs = {}
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
            self.logger.info(f"Using Instagram storage state from {storage_state}")
        context = await browser.new_context(**kwargs)
        return playwright, browser, context

    def _graphql_template_from_request(self, request) -> Optional[Dict[str, Any]]:
        try:
            if request.method != "POST":
                return None
            if "instagram.com" not in request.url or "/graphql" not in request.url:
                return None
            parsed = parse_qs(request.post_data or "", keep_blank_values=True)
            if not parsed:
                return None
            form = {key: values[0] if values else "" for key, values in parsed.items()}
            try:
                variables = json.loads(form.get("variables") or "{}")
            except Exception:
                variables = {}
            return {
                "endpoint": request.url.split("?")[0],
                "headers": dict(request.headers),
                "form": form,
                "variables": variables,
            }
        except Exception:
            return None

    async def _ensure_instagram_origin(self, page: Page) -> None:
        try:
            parsed = urlparse(page.url)
            if parsed.netloc and self.INSTAGRAM_HOST_RE.search(parsed.netloc):
                return
        except Exception:
            pass
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
        await self._dismiss_popups(page)

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"https://www.instagram.com/{url.lstrip('@').strip('/')}/"
        return url.split("?")[0].split("#")[0]

    def _username_from_url(self, url: str) -> str:
        if not self.is_instagram_url(url):
            return ""
        first_segment = urlparse(url).path.strip("/").split("/")[0]
        if first_segment and first_segment not in {"p", "reel", "tv", "explore"}:
            return first_segment
        return ""

    def _search_url_for_query(self, query: str) -> str:
        query = (query or "").strip()
        if self.is_instagram_url(query):
            return self._normalize_url(query)

        if query.startswith("@"):
            return f"https://www.instagram.com/{query[1:].strip('/')}/"

        if query.startswith("#"):
            tag = re.sub(r"\s+", "", query[1:])
            return f"https://www.instagram.com/explore/tags/{quote(tag)}/"

        return f"https://www.instagram.com/explore/search/keyword/?q={quote(query)}"

    def _profile_username_from_query(self, query: str) -> str:
        query = (query or "").strip()
        if query.startswith("@"):
            return query[1:].strip("/").lower()
        if not self.is_instagram_url(query):
            return ""
        parsed = urlparse(query)
        first_segment = parsed.path.strip("/").split("/")[0].lower()
        if first_segment and first_segment not in {"p", "reel", "tv", "explore"}:
            return first_segment
        return ""

    async def _dismiss_popups(self, page: Page) -> None:
        labels = [
            "Allow all cookies",
            "Decline optional cookies",
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

    async def _extract_post_links_from_dom(self, page: Page) -> List[Dict[str, str]]:
        return await page.evaluate(
            """
            () => {
              const links = Array.from(document.querySelectorAll(
                'a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]'
              ));
              const seen = new Set();
              const posts = [];
              for (const link of links) {
                const href = link.href;
                const match = href.match(/\\/(?:p|reel|tv)\\/([A-Za-z0-9_-]+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);
                const img = link.querySelector("img");
                const label = link.getAttribute("aria-label") || "";
                posts.push({
                  video_id: match[1],
                  url: href.split("?")[0].split("#")[0],
                  title: (img && img.alt) || label || document.title || "",
                  username: (() => {
                    const first = new URL(href).pathname.split("/").filter(Boolean)[0] || "";
                    return ["p", "reel", "tv", "explore"].includes(first) ? "" : first;
                  })()
                });
              }
              return posts;
            }
            """
        )

    async def search(self, query: str, max_videos: int = 10, context: Optional[BrowserContext] = None) -> List[Dict[str, Any]]:
        from ..raw_contract import transport_mode

        self.logger.info(f"Searching Instagram for '{query}'...")
        max_posts = max_videos if max_videos and max_videos > 0 else 0
        api_only = transport_mode() == "api-only"

        if self.is_instagram_url(query) and self.is_post_url(query):
            url = self._normalize_url(query)
            return [{
                "video_id": self.extract_shortcode(url),
                "url": url,
                "title": "Direct Instagram post",
                "username": "",
                "discovery_method": "direct_url",
                "metadata_method": "instagram_direct_api",
            }]

        manage_context = context is None
        playwright = browser = None
        if manage_context:
            playwright, browser, context = await self._new_context()

        page = await context.new_page()
        graphql_templates: List[Dict[str, Any]] = []
        media_by_shortcode: Dict[str, Dict[str, Any]] = {}
        response_tasks: Set[asyncio.Task] = set()

        def capture_request(request) -> None:
            template = self._graphql_template_from_request(request)
            if template:
                graphql_templates.append(template)

        async def capture_response(response) -> None:
            try:
                if "instagram.com" not in response.url:
                    return
                if not any(marker in response.url for marker in ("/graphql", "/api/graphql", "/api/v1/")):
                    return
                payload = await response.json()
                for item in self._iter_media_items(payload):
                    shortcode = self._shortcode_from_media_item(item)
                    media_id = item.get("id") or item.get("pk")
                    if shortcode and media_id and shortcode not in media_by_shortcode:
                        media_by_shortcode[shortcode] = item
            except Exception:
                return

        def schedule_response(response) -> None:
            task = asyncio.create_task(capture_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("request", capture_request)
        page.on("response", schedule_response)
        try:
            search_url = self._search_url_for_query(query)
            profile_username = self._profile_username_from_query(query)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            await self._dismiss_popups(page)
            try:
                await page.wait_for_selector(
                    'a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]',
                    timeout=15000,
                )
            except Exception:
                await asyncio.sleep(3)
            if profile_username:
                await asyncio.sleep(5)

            videos: List[Dict[str, Any]] = []
            seen: Set[str] = set()
            rounds_without_new = 0
            max_rounds = self._env_int(
                "INSTAGRAM_SEARCH_SCROLL_ROUNDS",
                30 if not max_posts else 12,
            )
            stall_limit = self._env_int(
                "INSTAGRAM_SEARCH_STALL_ROUNDS",
                4 if not max_posts else 3,
            )

            for _ in range(max_rounds):
                before = len(videos)
                before_api = len(media_by_shortcode)
                if not api_only:
                    found = await self._extract_post_links_from_dom(page)
                    if profile_username:
                        own_posts = [
                            post for post in found
                            if f"instagram.com/{profile_username}/" in post.get("url", "").lower()
                        ]
                        other_posts = [
                            post for post in found
                            if f"instagram.com/{profile_username}/" not in post.get("url", "").lower()
                        ]
                        if not own_posts and not videos:
                            await page.mouse.wheel(0, 1000)
                            await asyncio.sleep(2)
                            continue
                        found = own_posts + other_posts
                    for post in found:
                        video_id = post.get("video_id")
                        if not video_id or video_id in seen:
                            continue
                        seen.add(video_id)
                        post.setdefault("discovery_method", "browser_dom")
                        post.setdefault("metadata_method", "browser_dom_card")
                        videos.append(post)
                        if max_posts and len(videos) >= max_posts:
                            break

                discovered_count = len(media_by_shortcode) if api_only else len(videos)
                previous_count = before_api if api_only else before
                if max_posts and discovered_count >= max_posts:
                    break

                if discovered_count == previous_count:
                    rounds_without_new += 1
                else:
                    rounds_without_new = 0

                if rounds_without_new >= stall_limit:
                    break

                await page.mouse.wheel(0, 1800)
                await asyncio.sleep(1.25)

            if response_tasks:
                _, pending = await asyncio.wait(response_tasks, timeout=10)
                for task in pending:
                    task.cancel()
                if pending:
                    self.logger.debug(f"Cancelled {len(pending)} slow Instagram search response capture task(s)")

            for post in videos:
                video_id = post.get("video_id") or self.extract_shortcode(post.get("url", ""))
                media_item = media_by_shortcode.get(video_id)
                if not media_item:
                    media_id = self.shortcode_to_media_id(video_id)
                    if media_id and not post.get("media_id"):
                        post["media_id"] = media_id
                    continue
                media_id = media_item.get("id") or media_item.get("pk")
                if media_id:
                    post["media_id"] = str(media_id)
                caption = self._caption_from_media_item(media_item)
                username = self._username_from_media_item(media_item)
                if caption and (not post.get("title") or post.get("title") == "Instagram"):
                    post["title"] = caption
                if username and not post.get("username"):
                    post["username"] = username
                post["reported_comment_count"] = self._comment_count_from_media_item(media_item)
                post.update(self._media_metrics(media_item))
                post["url"] = self._canonical_url_from_media_item(media_item, post.get("url", ""), video_id)
                post["discovery_method"] = "instagram_graphql_response"
                post["metadata_method"] = "instagram_graphql_response"

            for shortcode, media_item in media_by_shortcode.items():
                if max_posts and len(videos) >= max_posts:
                    break
                if shortcode in seen:
                    continue
                username = self._username_from_media_item(media_item)
                if profile_username and username.casefold() != profile_username.casefold():
                    continue
                media_id = media_item.get("id") or media_item.get("pk")
                if not media_id:
                    media_id = self.shortcode_to_media_id(shortcode)
                    if not media_id:
                        continue
                seen.add(shortcode)
                media_metrics = self._media_metrics(media_item)
                videos.append({
                    "video_id": shortcode,
                    "media_id": str(media_id),
                    "url": self._canonical_url_from_media_item(media_item, "", shortcode),
                    "title": self._caption_from_media_item(media_item),
                    "username": username,
                    "reported_comment_count": self._comment_count_from_media_item(media_item),
                    "discovery_method": "instagram_graphql_response",
                    "metadata_method": "instagram_graphql_response",
                    **media_metrics,
                })

            self.logger.info(f"Found {len(videos)} Instagram posts for query '{query}'")
            if api_only and not videos:
                raise RuntimeError("Instagram API-only discovery captured no media records")
            if graphql_templates:
                self._last_graphql_template = graphql_templates[-1]
            return videos
        except Exception as e:
            self.logger.error(f"Error searching Instagram: {e}")
            if api_only:
                raise
            return []
        finally:
            await page.close()
            if manage_context and not self._external_context:
                await context.close()
                if browser:
                    await browser.close()
                await playwright.stop()
            elif manage_context and playwright:
                await playwright.stop()

    async def _page_meta(self, page: Page) -> Dict[str, str]:
        return await page.evaluate(
            """
            () => {
              const meta = (selector) => document.querySelector(selector)?.content || "";
              return {
                title: meta('meta[property="og:title"]') || document.title || "",
                description: meta('meta[property="og:description"]') || "",
                url: meta('meta[property="og:url"]') || location.href
              };
            }
            """
        )

    def _caption_from_meta(self, meta: Dict[str, str]) -> str:
        description = (meta.get("description") or "").strip()
        title = (meta.get("title") or "").strip()
        if description:
            return description
        return title

    def _username_from_meta(self, meta: Dict[str, str]) -> str:
        title = meta.get("title") or ""
        match = re.search(r"@([A-Za-z0-9_.]+)", title)
        if match:
            return match.group(1)
        match = re.search(r"^([^(@]+)\s+\(@", title)
        if match:
            return match.group(1).strip()
        return ""

    def _caption_from_media_item(self, item: Dict[str, Any]) -> str:
        caption = item.get("caption")
        if isinstance(caption, dict):
            text = caption.get("text") or caption.get("caption_text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        if isinstance(caption, str) and caption.strip():
            return caption.strip()
        for key in ("caption_text", "accessibility_caption", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _username_from_media_item(self, item: Dict[str, Any]) -> str:
        owner = item.get("owner") or item.get("user") or item.get("from") or {}
        if isinstance(owner, dict):
            username = owner.get("username") or owner.get("full_name")
            if isinstance(username, str) and username.strip():
                return username.strip()
        username = item.get("username")
        return username.strip() if isinstance(username, str) else ""

    def _canonical_url_from_media_item(self, item: Dict[str, Any], fallback_url: str, video_id: str) -> str:
        for key in ("permalink", "share_url"):
            value = item.get(key)
            if isinstance(value, str) and self.is_instagram_url(value):
                return self._normalize_url(value)

        code = item.get("code") or video_id
        if not code:
            return fallback_url

        product_type = str(item.get("product_type") or item.get("media_product_type") or "").lower()
        is_reel = product_type in {"clips", "reels", "reel"} or item.get("clips_metadata") is not None
        kind = "reel" if is_reel else "p"
        return f"https://www.instagram.com/{kind}/{code}/"

    def _stable_comment_id(self, text: str, author: str, video_id: str) -> str:
        raw = f"{video_id}|{author}|{text}".encode("utf-8", errors="ignore")
        return hashlib.sha1(raw).hexdigest()[:20]

    def _parse_guarded_json(self, text: str) -> Any:
        if text.startswith("for (;;);"):
            text = text[len("for (;;);"):]
        return json.loads(text)

    def _find_media_item(self, payload: Any) -> Optional[Dict[str, Any]]:
        if isinstance(payload, dict):
            for key in ("items", "media", "item"):
                value = payload.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and (item.get("pk") or item.get("id") or item.get("code")):
                            return item
                if isinstance(value, dict) and (value.get("pk") or value.get("id") or value.get("code")):
                    return value
            for value in payload.values():
                found = self._find_media_item(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = self._find_media_item(value)
                if found:
                    return found
        return None

    def _iter_media_items(self, payload: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("shortcode")
            has_media_id = payload.get("id") or payload.get("pk")
            if isinstance(code, str) and code and has_media_id:
                yield payload

            for value in payload.values():
                yield from self._iter_media_items(value)
        elif isinstance(payload, list):
            for value in payload:
                yield from self._iter_media_items(value)

    def _shortcode_from_media_item(self, item: Dict[str, Any]) -> str:
        code = item.get("code") or item.get("shortcode")
        if isinstance(code, str) and code:
            return code
        for key in ("permalink", "share_url", "url"):
            value = item.get(key)
            if isinstance(value, str):
                shortcode = self.extract_shortcode(value)
                if shortcode:
                    return shortcode
        return ""

    def _owner_from_node(self, node: Dict[str, Any]) -> Dict[str, Any]:
        owner = node.get("owner") or node.get("user") or node.get("from") or {}
        return owner if isinstance(owner, dict) else {}

    def _like_count_from_node(self, node: Dict[str, Any]) -> int:
        for key in ("like_count", "comment_like_count", "likes_count", "likes"):
            value = node.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        edge = node.get("edge_liked_by") or node.get("edge_media_preview_like")
        if isinstance(edge, dict):
            count = edge.get("count")
            if isinstance(count, int):
                return count
        return 0

    def _reply_count_from_node(self, node: Dict[str, Any]) -> int:
        for key in ("child_comment_count", "reply_count", "num_tail_child_comments"):
            value = node.get(key)
            if isinstance(value, int):
                return value
        edge = node.get("edge_threaded_comments")
        if isinstance(edge, dict):
            count = edge.get("count")
            if isinstance(count, int):
                return count
        return 0

    def _comment_count_from_media_item(self, item: Dict[str, Any]) -> Optional[int]:
        for key in ("comment_count", "comments_count"):
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)

        edge = item.get("edge_media_to_comment") or item.get("edge_media_preview_comment")
        if isinstance(edge, dict):
            count = edge.get("count")
            if isinstance(count, int):
                return count
            if isinstance(count, str) and count.isdigit():
                return int(count)
        return None

    def _media_metrics(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        user = item.get("user") or item.get("owner") or {}
        if not isinstance(user, dict):
            user = {}
        clips = item.get("clips_metadata") if isinstance(item.get("clips_metadata"), dict) else {}
        music_info = clips.get("music_info") if isinstance(clips.get("music_info"), dict) else {}
        music_asset = music_info.get("music_asset_info") if isinstance(music_info.get("music_asset_info"), dict) else {}
        original_sound = clips.get("original_sound_info") if isinstance(clips.get("original_sound_info"), dict) else {}
        ig_artist = original_sound.get("ig_artist") if isinstance(original_sound.get("ig_artist"), dict) else {}
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        image_versions = item.get("image_versions2") if isinstance(item.get("image_versions2"), dict) else {}
        candidates = image_versions.get("candidates") if isinstance(image_versions.get("candidates"), list) else []
        thumbnail_url = candidates[0].get("url", "") if candidates and isinstance(candidates[0], dict) else ""

        def nested_value(*keys: str) -> Any:
            stack: List[Any] = [item]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    for key in keys:
                        if key in current and current.get(key) not in (None, ""):
                            return current.get(key)
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)
            return None

        def direct_value(container: Dict[str, Any], *keys: str) -> Any:
            for key in keys:
                if key in container and container.get(key) not in (None, ""):
                    return container.get(key)
            return None

        view_count = nested_value(
            "play_count",
            "video_play_count",
            "ig_play_count",
            "video_view_count",
            "view_count",
        )
        like_count = direct_value(item, "like_count", "likes_count")
        if like_count is None:
            like_edge = item.get("edge_media_preview_like") or item.get("edge_liked_by") or {}
            if isinstance(like_edge, dict):
                like_count = direct_value(like_edge, "count")
        share_count = direct_value(item, "reshare_count", "share_count")
        save_count = direct_value(item, "save_count")
        follower_count = direct_value(user, "follower_count")
        if follower_count is None:
            followed_by = user.get("edge_followed_by") if isinstance(user.get("edge_followed_by"), dict) else {}
            follower_count = direct_value(followed_by, "count")
        content_type = item.get("media_type") or item.get("product_type") or ""
        is_photo = str(content_type).casefold() in {"1", "photo", "image"}
        metric_availability = {
            "views": (
                "available"
                if view_count is not None
                else "not_applicable"
                if is_photo
                else "missing_from_public_response"
            ),
            "likes": "available" if like_count is not None else "missing_from_public_response",
            "shares": "available" if share_count is not None else "not_publicly_exposed",
            "saves": "available" if save_count is not None else "not_publicly_exposed",
            "followers": "available" if follower_count is not None else "missing_from_public_response",
        }
        return {
            "published_at": item.get("taken_at") or item.get("taken_at_timestamp") or item.get("created_at") or "",
            "content_type": content_type,
            "view_count": view_count,
            "like_count": like_count,
            "share_count": share_count,
            "save_count": save_count,
            "creator_id": user.get("id") or user.get("pk") or "",
            "creator_display_name": user.get("full_name") or "",
            "creator_verified": user.get("is_verified"),
            "creator_profile": {
                "user_id": user.get("id") or user.get("pk") or "",
                "username": user.get("username") or "",
                "display_name": user.get("full_name") or "",
                "bio": user.get("biography") or "",
                "website": user.get("external_url") or "",
                "avatar_url": (
                    user.get("profile_pic_url_hd")
                    or user.get("profile_pic_url")
                    or ""
                ),
                "verified": user.get("is_verified"),
                "protected": user.get("is_private"),
                "account_type": user.get("account_type") or "",
                "category": (
                    user.get("category_name")
                    or user.get("business_category_name")
                    or ""
                ),
                "follower_count": follower_count,
                "following_count": (
                    user.get("following_count")
                    or (
                        user.get("edge_follow", {}).get("count")
                        if isinstance(user.get("edge_follow"), dict)
                        else None
                    )
                ),
            },
            "follower_count": follower_count,
            "metric_availability": metric_availability,
            "music_id": music_asset.get("audio_id") or original_sound.get("audio_asset_id") or "",
            "music_title": music_asset.get("title") or original_sound.get("original_audio_title") or "",
            "music_author": music_asset.get("display_artist") or ig_artist.get("full_name") or "",
            "duration_seconds": item.get("video_duration") or (original_sound.get("duration_in_ms", 0) / 1000 if original_sound.get("duration_in_ms") else 0),
            "thumbnail_url": thumbnail_url,
            "content_location_id": location.get("id") or location.get("pk") or "",
            "content_location_name": location.get("name") or location.get("short_name") or "",
            "content_location_latitude": location.get("lat"),
            "content_location_longitude": location.get("lng"),
        }

    async def _fetch_profile_metrics(self, page: Page, username: str) -> Dict[str, Any]:
        username = (username or "").strip().lstrip("@").casefold()
        if not username or username in {"instagram", "instagram user"}:
            return {}
        if username in self._profile_metrics_cache:
            return dict(self._profile_metrics_cache[username])
        enabled = os.environ.get("INSTAGRAM_PROFILE_METRICS_ENABLED", "1").strip().lower() not in {
            "0", "false", "no", "off"
        }
        try:
            limit = max(0, int(os.environ.get("INSTAGRAM_PROFILE_METRICS_MAX_PROFILES", "50")))
        except ValueError:
            limit = 50
        if not enabled or (limit and self._profile_metrics_requests >= limit):
            return {}

        self._profile_metrics_requests += 1
        endpoint = (
            "https://www.instagram.com/api/v1/users/web_profile_info/?username="
            + quote(username)
        )
        try:
            result = await page.evaluate(
                """
                async (endpoint) => {
                  const response = await fetch(endpoint, {
                    method: "GET",
                    credentials: "include",
                    headers: {
                      "accept": "application/json, text/plain, */*",
                      "x-asbd-id": "129477",
                      "x-ig-app-id": "936619743392459",
                      "x-requested-with": "XMLHttpRequest"
                    }
                  });
                  return {status: response.status, ok: response.ok, text: await response.text()};
                }
                """,
                endpoint,
            )
            if not result.get("ok"):
                self._profile_metrics_cache[username] = {}
                return {}
            payload = self._parse_guarded_json(result.get("text") or "")
            data = payload.get("data") if isinstance(payload, dict) else {}
            user = data.get("user") if isinstance(data, dict) else {}
            if not isinstance(user, dict):
                user = {}
            followed_by = user.get("edge_followed_by") if isinstance(user.get("edge_followed_by"), dict) else {}
            follower_count = user.get("follower_count")
            if follower_count is None:
                follower_count = followed_by.get("count")
            metrics = {
                "follower_count": follower_count,
                "creator_id": user.get("id") or "",
                "creator_display_name": user.get("full_name") or "",
                "creator_verified": user.get("is_verified"),
                "creator_profile": {
                    "user_id": user.get("id") or "",
                    "username": user.get("username") or username,
                    "display_name": user.get("full_name") or "",
                    "bio": user.get("biography") or "",
                    "website": user.get("external_url") or "",
                    "avatar_url": (
                        user.get("profile_pic_url_hd")
                        or user.get("profile_pic_url")
                        or ""
                    ),
                    "verified": user.get("is_verified"),
                    "protected": user.get("is_private"),
                    "account_type": user.get("account_type") or "",
                    "category": (
                        user.get("category_name")
                        or user.get("business_category_name")
                        or ""
                    ),
                    "follower_count": follower_count,
                    "following_count": (
                        user.get("following_count")
                        or (
                            user.get("edge_follow", {}).get("count")
                            if isinstance(user.get("edge_follow"), dict)
                            else None
                        )
                    ),
                    "content_count": (
                        user.get("edge_owner_to_timeline_media", {}).get("count")
                        if isinstance(user.get("edge_owner_to_timeline_media"), dict)
                        else None
                    ),
                },
            }
            if follower_count is not None:
                metrics["metric_availability"] = {"followers": "available"}
            self._profile_metrics_cache[username] = metrics
            return dict(metrics)
        except Exception as exc:
            self.logger.debug(f"Instagram profile metrics lookup failed for {username}: {exc}")
            self._profile_metrics_cache[username] = {}
            return {}

    def _comment_from_node(self, node: Dict[str, Any], video_id: str, is_reply: bool = False) -> Optional[Dict[str, Any]]:
        text = node.get("text") or node.get("comment_text") or node.get("body")
        if not isinstance(text, str) or not text.strip():
            return None

        raw_id = node.get("id") or node.get("pk") or node.get("comment_id")
        owner = self._owner_from_node(node)
        author = owner.get("username") or owner.get("full_name") or node.get("username") or ""
        author_id = owner.get("id") or owner.get("pk") or node.get("user_id") or ""

        # Caption objects can look comment-ish but usually do not carry a comment id.
        if not raw_id and not author:
            return None

        comment_id = str(raw_id) if raw_id else self._stable_comment_id(text.strip(), str(author), video_id)
        likes = self._like_count_from_node(node)

        return {
            "id": comment_id,
            "comment_id": comment_id,
            "text": text.strip(),
            "author": author,
            "author_id": str(author_id) if author_id else "",
            "author_profile": {
                "user_id": owner.get("id") or owner.get("pk") or "",
                "username": owner.get("username") or "",
                "display_name": owner.get("full_name") or "",
                "bio": owner.get("biography") or "",
                "avatar_url": (
                    owner.get("profile_pic_url")
                    or owner.get("profile_picture")
                    or ""
                ),
                "verified": owner.get("is_verified"),
                "protected": owner.get("is_private"),
            },
            "likes": likes,
            "likes_text": str(likes),
            "time": node.get("created_at") or node.get("created_at_utc") or node.get("taken_at") or "",
            "platform": "instagram",
            "video_id": video_id,
            "reply_count": self._reply_count_from_node(node),
            "is_reply": is_reply,
        }

    def _iter_edges(self, node: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(node, dict):
            edges = node.get("edges")
            if isinstance(edges, list):
                for edge in edges:
                    if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                        yield edge["node"]

    def _collect_comments_from_json(
        self,
        payload: Any,
        video_id: str,
        comments: Dict[str, Dict[str, Any]],
        is_reply: bool = False,
    ) -> None:
        if isinstance(payload, dict):
            comment = self._comment_from_node(payload, video_id, is_reply=is_reply)
            if comment:
                existing = comments.get(comment["comment_id"])
                if existing and existing.get("is_reply"):
                    comment["is_reply"] = True
                comments[comment["comment_id"]] = comment

            reply_containers = [
                payload.get("edge_threaded_comments"),
                payload.get("preview_child_comments"),
                payload.get("child_comments"),
                payload.get("replies"),
            ]
            for container in reply_containers:
                if isinstance(container, dict):
                    for reply_node in self._iter_edges(container):
                        self._collect_comments_from_json(reply_node, video_id, comments, is_reply=True)
                elif isinstance(container, list):
                    for reply_node in container:
                        self._collect_comments_from_json(reply_node, video_id, comments, is_reply=True)

            for value in payload.values():
                self._collect_comments_from_json(value, video_id, comments, is_reply=is_reply)

        elif isinstance(payload, list):
            for value in payload:
                self._collect_comments_from_json(value, video_id, comments, is_reply=is_reply)

    def _comments_page_info(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                connection = data.get("xdt_api__v1__media__media_id__comments__connection")
                if isinstance(connection, dict) and isinstance(connection.get("page_info"), dict):
                    return connection["page_info"]

            page_info = payload.get("page_info")
            if isinstance(page_info, dict) and (
                "has_next_page" in page_info or "end_cursor" in page_info
            ):
                return page_info

            for value in payload.values():
                found = self._comments_page_info(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = self._comments_page_info(value)
                if found:
                    return found
        return {}

    async def _page_fetch_graphql(
        self,
        page: Page,
        template: Dict[str, Any],
        variables: Dict[str, Any],
        friendly_name: str = "PolarisPostCommentsPaginationQuery",
        doc_id: Optional[str] = None,
    ) -> Optional[Any]:
        endpoint = template.get("endpoint") or "https://www.instagram.com/api/graphql"
        headers = {}
        for key, value in (template.get("headers") or {}).items():
            lower = key.lower()
            if lower in {
                "accept",
                "content-type",
                "x-asbd-id",
                "x-csrftoken",
                "x-fb-friendly-name",
                "x-fb-lsd",
                "x-ig-app-id",
                "x-instagram-ajax",
                "x-requested-with",
            }:
                headers[key] = value
        headers["content-type"] = "application/x-www-form-urlencoded"
        headers["x-fb-friendly-name"] = friendly_name

        body = dict(template.get("form") or {})
        body.update({
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name,
            "variables": json.dumps(variables, separators=(",", ":")),
            "server_timestamps": "true",
            "doc_id": doc_id or body.get("doc_id") or self.COMMENTS_DOC_ID,
        })

        result = await page.evaluate(
            """
            async ({ endpoint, headers, body }) => {
              const response = await fetch(endpoint, {
                method: 'POST',
                credentials: 'include',
                headers,
                body: new URLSearchParams(body).toString(),
              });
              return {
                status: response.status,
                ok: response.ok,
                text: await response.text(),
              };
            }
            """,
            {"endpoint": endpoint, "headers": headers, "body": body},
        )
        if not result.get("ok"):
            self.logger.debug(f"Instagram direct GraphQL returned HTTP {result.get('status')}")
            return None
        try:
            payload = self._parse_guarded_json(result.get("text") or "")
        except Exception as exc:
            self.logger.debug(f"Instagram direct GraphQL parse failed: {exc}")
            return None
        if isinstance(payload, dict) and payload.get("error"):
            self.logger.debug(f"Instagram direct GraphQL error: {payload.get('errorSummary') or payload.get('error')}")
            return None
        return payload

    async def _fetch_media_item_direct_api(
        self,
        page: Page,
        template: Dict[str, Any],
        shortcode: str,
    ) -> Optional[Dict[str, Any]]:
        if not shortcode:
            return None
        variables = {
            "shortcode": shortcode,
            "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
        }
        payload = await self._page_fetch_graphql(
            page,
            template,
            variables,
            friendly_name="PolarisPostRootQuery",
            doc_id=self.POST_ROOT_DOC_ID,
        )
        if not payload:
            return None
        return self._find_media_item(payload)

    def _comments_template_from_media_item(
        self,
        template: Dict[str, Any],
        media_item: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        media_id = media_item.get("id") or media_item.get("pk")
        if not media_id:
            return None

        form = dict(template.get("form") or {})
        form["fb_api_req_friendly_name"] = "PolarisPostCommentsPaginationQuery"
        form["doc_id"] = self.COMMENTS_DOC_ID

        variables = {
            "after": None,
            "before": None,
            "first": self._env_int("INSTAGRAM_COMMENTS_PAGE_SIZE", 10, minimum=1),
            "last": None,
            "media_id": str(media_id),
            "sort_order": "popular",
            "__relay_internal__pv__PolarisIsLoggedInrelayprovider": True,
        }

        return {
            "endpoint": template.get("endpoint") or "https://www.instagram.com/api/graphql",
            "headers": dict(template.get("headers") or {}),
            "form": form,
            "variables": variables,
        }

    async def _fetch_comments_direct_api(
        self,
        page: Page,
        template: Dict[str, Any],
        video_id: str,
        comments: Dict[str, Dict[str, Any]],
        max_comments: int,
        state: Optional[Dict[str, Any]] = None,
    ) -> int:
        state = state if isinstance(state, dict) else {}
        state.update({"pages": 0, "exhausted": False, "cursor_present": False, "stopped_reason": "not_started"})
        variables = dict(template.get("variables") or {})
        if not variables.get("media_id"):
            state["stopped_reason"] = "missing_media_id"
            return 0

        added = 0
        seen_cursors: Set[str] = set()
        page_size = variables.get("first") if isinstance(variables.get("first"), int) else 10
        max_api_pages = self._env_int("INSTAGRAM_COMMENT_API_MAX_PAGES", 200, minimum=1)
        max_pages = (
            max_api_pages
            if not max_comments or max_comments <= 0
            else max(2, min(max_api_pages, (max_comments // max(1, page_size)) + 3))
        )

        for _ in range(max_pages):
            state["pages"] = int(state.get("pages") or 0) + 1
            payload = await self._page_fetch_graphql(
                page,
                template,
                variables,
                friendly_name="PolarisPostCommentsPaginationQuery",
                doc_id=self.COMMENTS_DOC_ID,
            )
            if not payload:
                state["stopped_reason"] = "empty_or_failed_response"
                break

            before = len(comments)
            self._collect_comments_from_json(payload, video_id, comments)
            added += max(0, len(comments) - before)

            if max_comments and max_comments > 0 and len(comments) >= max_comments:
                state["stopped_reason"] = "comment_limit"
                break

            page_info = self._comments_page_info(payload)
            cursor = page_info.get("end_cursor") if isinstance(page_info, dict) else ""
            has_next_page = page_info.get("has_next_page") if isinstance(page_info, dict) else None
            state["cursor_present"] = bool(cursor)
            if has_next_page is False:
                state["exhausted"] = True
                state["stopped_reason"] = "no_next_page"
                break
            if not cursor:
                state["stopped_reason"] = "missing_cursor"
                break
            if cursor in seen_cursors:
                state["stopped_reason"] = "repeated_cursor"
                break
            seen_cursors.add(cursor)
            variables["after"] = cursor
        else:
            state["stopped_reason"] = "max_pages"

        return added

    async def _fetch_comments_rest_api(
        self,
        page: Page,
        media_id: str,
        video_id: str,
        comments: Dict[str, Dict[str, Any]],
        max_comments: int,
        state: Optional[Dict[str, Any]] = None,
    ) -> int:
        state = state if isinstance(state, dict) else {}
        state.update({"pages": 0, "exhausted": False, "cursor_present": False, "stopped_reason": "not_started"})
        if not media_id:
            state["stopped_reason"] = "missing_media_id"
            return 0

        added = 0
        cursor = None
        seen_cursors: Set[str] = set()
        page_size = self._env_int("INSTAGRAM_REST_COMMENTS_PAGE_SIZE", 50, minimum=1)
        max_api_pages = self._env_int("INSTAGRAM_REST_COMMENT_API_MAX_PAGES", 100, minimum=1)
        max_pages = (
            max_api_pages
            if not max_comments or max_comments <= 0
            else max(2, min(max_api_pages, (max_comments // max(1, page_size)) + 3))
        )

        for _ in range(max_pages):
            state["pages"] = int(state.get("pages") or 0) + 1
            params = {
                "can_support_threading": "true",
                "permalink_enabled": "false",
                "count": str(page_size),
            }
            if cursor:
                params["min_id"] = cursor
            endpoint = f"https://www.instagram.com/api/v1/media/{media_id}/comments/?{urlencode(params)}"
            result = await page.evaluate(
                """
                async (endpoint) => {
                  const response = await fetch(endpoint, {
                    method: "GET",
                    credentials: "include",
                    headers: {
                      "accept": "application/json, text/plain, */*",
                      "x-asbd-id": "129477",
                      "x-ig-app-id": "936619743392459",
                      "x-requested-with": "XMLHttpRequest"
                    }
                  });
                  return {
                    status: response.status,
                    ok: response.ok,
                    text: await response.text(),
                  };
                }
                """,
                endpoint,
            )
            if not result.get("ok"):
                self.logger.debug(f"Instagram REST comments returned HTTP {result.get('status')}")
                state["stopped_reason"] = f"http_{result.get('status')}"
                break
            try:
                payload = self._parse_guarded_json(result.get("text") or "")
            except Exception as exc:
                self.logger.debug(f"Instagram REST comments parse failed: {exc}")
                state["stopped_reason"] = "parse_failed"
                break

            before = len(comments)
            self._collect_comments_from_json(payload, video_id, comments)
            page_added = max(0, len(comments) - before)
            added += page_added

            if max_comments and max_comments > 0 and len(comments) >= max_comments:
                state["stopped_reason"] = "comment_limit"
                break

            next_cursor = ""
            if isinstance(payload, dict):
                next_cursor = payload.get("next_min_id") or payload.get("next_max_id") or ""
            state["cursor_present"] = bool(next_cursor)
            if not next_cursor:
                state["exhausted"] = True
                state["stopped_reason"] = "no_next_cursor"
                break
            if next_cursor in seen_cursors:
                state["stopped_reason"] = "repeated_cursor"
                break
            if page_added == 0:
                state["stopped_reason"] = "no_new_comments"
                break
            seen_cursors.add(next_cursor)
            cursor = str(next_cursor)
        else:
            state["stopped_reason"] = "max_pages"

        return added

    async def _extract_dom_comments(self, page: Page, video_id: str) -> List[Dict[str, Any]]:
        raw_comments = await page.evaluate(
            """
            () => {
              const items = [];
              const seen = new Set();

              const profileUsernameFrom = (root) => {
                const links = Array.from(root.querySelectorAll('a[href^="/"], a[href*="instagram.com/"]'));
                for (const link of links) {
                  let path = "";
                  try {
                    path = new URL(link.href, location.origin).pathname;
                  } catch {
                    path = link.getAttribute("href") || "";
                  }
                  const parts = path.split("/").filter(Boolean);
                  if (
                    parts.length === 1 &&
                    !["p", "reel", "tv", "explore", "accounts"].includes(parts[0])
                  ) {
                    return (link.textContent || parts[0] || "").trim();
                  }
                }
                return "";
              };

              for (const anchor of Array.from(document.querySelectorAll('a[href*="/c/"]'))) {
                const href = anchor.href;
                const match = href.match(/\\/c\\/([^/?#]+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);

                let root = anchor.closest("li") || anchor.parentElement;
                for (let i = 0; root && i < 5; i++) {
                  const text = root.innerText || "";
                  if (text.includes("Reply") || text.includes("Like")) break;
                  root = root.parentElement;
                }

                const text = (root && root.innerText) || "";
                if (!text || text.length > 1500) continue;
                items.push({
                  id: match[1],
                  username: profileUsernameFrom(root || anchor.parentElement),
                  text
                });
              }

              if (items.length) return items;

              return Array.from(document.querySelectorAll("article ul li")).map((li) => {
                const username = li.querySelector('a[href^="/"]')?.textContent?.trim() || "";
                const text = li.innerText || "";
                return { username, text };
              }).filter((item) => item.text && item.text.length > 2);
            }
            """
        )

        comments: Dict[str, Dict[str, Any]] = {}
        for item in raw_comments:
            raw_text = re.sub(r"\r\n?", "\n", item.get("text", "")).strip()
            username = (item.get("username") or "").strip()
            if not raw_text or "Log in" in raw_text or "Sign up" in raw_text:
                continue

            lines = [
                line.strip()
                for line in raw_text.split("\n")
                if line.strip()
            ]
            cleaned_lines = []
            for line in lines:
                lower = line.lower()
                if username and lower == username.lower():
                    continue
                if lower in {"like", "reply", "see translation", "hide replies", "view replies"}:
                    continue
                if re.fullmatch(r"\d+[smhdw]", lower):
                    continue
                if re.fullmatch(r"\d+", line):
                    continue
                if re.fullmatch(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}", lower):
                    continue
                cleaned_lines.append(line)

            text = cleaned_lines[0] if cleaned_lines else raw_text
            if not text or len(text) > 1000:
                continue

            comment_id = str(item.get("id") or "") or self._stable_comment_id(text, username, video_id)
            comments[comment_id] = {
                "id": comment_id,
                "comment_id": comment_id,
                "text": text,
                "author": username,
                "author_id": "",
                "likes": 0,
                "likes_text": "0",
                "time": "",
                "platform": "instagram",
                "video_id": video_id,
                "reply_count": 0,
                "is_reply": False,
            }

        return list(comments.values())

    async def _load_more_comments(self, page: Page, max_rounds: int) -> None:
        button_patterns = [
            r"view all .*comments",
            r"view previous comments",
            r"view more comments",
            r"load more comments",
            r"view replies",
            r"view .*replies",
            r"show replies",
            r"show more comments",
            r"more comments",
        ]

        for _ in range(max_rounds):
            clicked = False
            for pattern in button_patterns:
                try:
                    locator = page.get_by_role("button", name=re.compile(pattern, re.I)).first
                    if await locator.count():
                        await locator.click(timeout=1500)
                        clicked = True
                        await asyncio.sleep(1)
                except Exception:
                    pass

                try:
                    locator = page.get_by_text(re.compile(pattern, re.I)).first
                    if await locator.count():
                        await locator.click(timeout=1500)
                        clicked = True
                        await asyncio.sleep(1)
                except Exception:
                    pass

            await page.mouse.wheel(0, 1200)
            try:
                await page.evaluate(
                    """
                    () => {
                      const candidates = Array.from(document.querySelectorAll('div, main, section, article'))
                        .filter(el => el.scrollHeight > el.clientHeight + 200)
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                      for (const el of candidates.slice(0, 5)) {
                        el.scrollTop = el.scrollHeight;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                      }
                    }
                    """
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            if not clicked:
                await asyncio.sleep(0.5)

    async def extract_comments(
        self,
        video_data: Dict[str, Any],
        max_comments: int = 100,
        context: Optional[BrowserContext] = None,
    ) -> Dict[str, Any]:
        url = video_data.get("url") or ""
        video_id = video_data.get("video_id") or self.extract_shortcode(url)
        if not video_id and url:
            video_id = self.extract_shortcode(url)
        self.logger.info(f"Extracting comments from Instagram post {video_id or url}...")

        comments_by_id: Dict[str, Dict[str, Any]] = {}
        response_tasks: Set[asyncio.Task] = set()
        graphql_templates: List[Dict[str, Any]] = []
        comment_api_templates: List[Dict[str, Any]] = []

        manage_context = context is None
        playwright = browser = None
        if manage_context:
            playwright, browser, context = await self._new_context()
        page = await context.new_page()

        async def capture_response(response):
            response_url = response.url
            if not any(marker in response_url for marker in ("/graphql/query", "/api/graphql", "/comments/")):
                return
            try:
                payload = await response.json()
                self._collect_comments_from_json(payload, video_id, comments_by_id)
            except Exception:
                return

        def schedule_capture(response):
            task = asyncio.create_task(capture_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        def capture_request(request):
            template = self._graphql_template_from_request(request)
            if not template:
                return
            graphql_templates.append(template)
            form = template.get("form") or {}
            if form.get("fb_api_req_friendly_name") == "PolarisPostCommentsPaginationQuery":
                comment_api_templates.append(template)

        page.on("request", capture_request)
        page.on("response", schedule_capture)
        dom_comments_used = False
        pagination_states: List[Dict[str, Any]] = []
        rest_attempted = False

        try:
            if not url and video_id:
                url = f"https://www.instagram.com/p/{video_id}/"
            url = self._normalize_url(url)
            navigation_error = None
            page_loaded = False

            meta: Dict[str, str] = {}
            canonical_url = self._normalize_url(meta.get("url") or url)
            video_id = video_id or self.extract_shortcode(canonical_url)
            caption = video_data.get("title") or self._caption_from_meta(meta)
            username = (
                video_data.get("username")
                or self._username_from_url(canonical_url)
                or self._username_from_meta(meta)
            )

            media_item = None
            api_first_enabled = os.environ.get("INSTAGRAM_API_FIRST", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            direct_only_enabled = os.environ.get("INSTAGRAM_DIRECT_ONLY", "0").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            graphql_template = self._last_graphql_template
            api_first_used = False
            provided_media_id = video_data.get("media_id")

            if api_first_enabled and graphql_template and video_id and provided_media_id:
                try:
                    await self._ensure_instagram_origin(page)
                    media_item = {
                        "id": str(provided_media_id),
                        "pk": str(provided_media_id),
                        "code": video_id,
                    }
                    reported_count = video_data.get("reported_comment_count")
                    if isinstance(reported_count, int):
                        media_item["comment_count"] = reported_count
                    media_caption = video_data.get("title") or ""
                    media_username = video_data.get("username") or ""
                    if media_caption and (not caption or caption == "Instagram"):
                        caption = media_caption
                    if media_username and (not username or username == "Instagram User"):
                        username = media_username
                    canonical_url = self._canonical_url_from_media_item(media_item, canonical_url, video_id)

                    direct_template = self._comments_template_from_media_item(graphql_template, media_item)
                    if direct_template:
                        direct_state: Dict[str, Any] = {}
                        added = await self._fetch_comments_direct_api(
                            page,
                            direct_template,
                            video_id,
                            comments_by_id,
                            max_comments,
                            direct_state,
                        )
                        pagination_states.append({"method": "graphql", **direct_state})
                        api_first_used = True
                        self.logger.info(
                            f"Instagram direct media_id fetched {added} comments"
                            + (f" (post reports {reported_count})" if reported_count is not None else "")
                        )
                        if (
                            isinstance(reported_count, int)
                            and len(comments_by_id) < reported_count
                            and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments)
                        ):
                            rest_state: Dict[str, Any] = {}
                            rest_added = await self._fetch_comments_rest_api(
                                page,
                                str(provided_media_id),
                                video_id,
                                comments_by_id,
                                max_comments,
                                rest_state,
                            )
                            rest_attempted = True
                            pagination_states.append({"method": "rest", **rest_state})
                            if rest_added:
                                self.logger.info(f"Instagram REST comments fallback added {rest_added} comments")
                except Exception as exc:
                    self.logger.debug(f"Instagram direct media_id lookup failed: {exc}")

            if api_first_enabled and graphql_template and video_id and not media_item:
                try:
                    await self._ensure_instagram_origin(page)
                    media_item = await self._fetch_media_item_direct_api(page, graphql_template, video_id)
                    if media_item:
                        api_first_used = True
                        media_caption = self._caption_from_media_item(media_item)
                        media_username = self._username_from_media_item(media_item)
                        if media_caption and (not caption or caption == "Instagram"):
                            caption = media_caption
                        if media_username and (not username or username == "Instagram User"):
                            username = media_username
                        canonical_url = self._canonical_url_from_media_item(media_item, canonical_url, video_id)

                        direct_template = self._comments_template_from_media_item(graphql_template, media_item)
                        if direct_template:
                            direct_state = {}
                            added = await self._fetch_comments_direct_api(
                                page,
                                direct_template,
                                video_id,
                                comments_by_id,
                                max_comments,
                                direct_state,
                            )
                            pagination_states.append({"method": "graphql", **direct_state})
                            media_comment_count = self._comment_count_from_media_item(media_item)
                            self.logger.info(
                                f"Instagram API-first fetched {added} comments"
                                + (f" (post reports {media_comment_count})" if media_comment_count is not None else "")
                            )
                            media_id = media_item.get("id") or media_item.get("pk")
                            if (
                                isinstance(media_comment_count, int)
                                and len(comments_by_id) < media_comment_count
                                and media_id
                                and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments)
                            ):
                                rest_state = {}
                                rest_added = await self._fetch_comments_rest_api(
                                    page,
                                    str(media_id),
                                    video_id,
                                    comments_by_id,
                                    max_comments,
                                    rest_state,
                                )
                                rest_attempted = True
                                pagination_states.append({"method": "rest", **rest_state})
                                if rest_added:
                                    self.logger.info(f"Instagram REST comments fallback added {rest_added} comments")
                    else:
                        self.logger.debug("Instagram API-first media lookup returned no media")
                except Exception as exc:
                    self.logger.debug(f"Instagram API-first lookup failed: {exc}")

            media_comment_count = self._comment_count_from_media_item(media_item or {}) if media_item else None
            if direct_only_enabled and media_item:
                should_visit_post = False
            else:
                should_visit_post = (
                    not media_item
                    or (not comments_by_id and (media_comment_count is None or media_comment_count > 0))
                )
            if should_visit_post:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await self._dismiss_popups(page)
                    page_loaded = True
                except Exception as exc:
                    navigation_error = exc
                    self.logger.warning(f"Instagram post page failed to load; trying direct API fallback: {exc}")
                    try:
                        await self._ensure_instagram_origin(page)
                    except Exception as origin_exc:
                        self.logger.debug(f"Instagram origin recovery failed: {origin_exc}")

                meta = await self._page_meta(page) if page_loaded else {}
                canonical_url = self._normalize_url(meta.get("url") or canonical_url)
                video_id = video_id or self.extract_shortcode(canonical_url)
                caption = caption or self._caption_from_meta(meta)
                username = username or self._username_from_url(canonical_url) or self._username_from_meta(meta)

            graphql_template = (comment_api_templates[-1] if comment_api_templates else None) or (
                graphql_templates[-1] if graphql_templates else None
            ) or graphql_template or self._last_graphql_template
            if graphql_template and video_id and (not api_first_used or should_visit_post):
                try:
                    media_item = await self._fetch_media_item_direct_api(page, graphql_template, video_id)
                    if media_item:
                        media_caption = self._caption_from_media_item(media_item)
                        media_username = self._username_from_media_item(media_item)
                        if media_caption and (not caption or caption == "Instagram"):
                            caption = media_caption
                        if media_username and (not username or username == "Instagram User"):
                            username = media_username
                        canonical_url = self._canonical_url_from_media_item(media_item, canonical_url, video_id)
                    direct_template = self._comments_template_from_media_item(graphql_template, media_item or {})
                    if direct_template:
                        before = len(comments_by_id)
                        direct_state = {}
                        added = await self._fetch_comments_direct_api(
                            page,
                            direct_template,
                            video_id,
                            comments_by_id,
                            max_comments,
                            direct_state,
                        )
                        pagination_states.append({"method": "graphql", **direct_state})
                        media_comment_count = self._comment_count_from_media_item(media_item or {})
                        if added:
                            self.logger.info(
                                f"Instagram direct GraphQL fetched {added} comments early"
                                + (f" (post reports {media_comment_count})" if media_comment_count is not None else "")
                            )
                        media_id = media_item.get("id") or media_item.get("pk") if isinstance(media_item, dict) else None
                        if (
                            isinstance(media_comment_count, int)
                            and len(comments_by_id) < media_comment_count
                            and media_id
                            and not rest_attempted
                            and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments)
                        ):
                            rest_state = {}
                            rest_added = await self._fetch_comments_rest_api(
                                page,
                                str(media_id),
                                video_id,
                                comments_by_id,
                                max_comments,
                                rest_state,
                            )
                            rest_attempted = True
                            pagination_states.append({"method": "rest", **rest_state})
                            if rest_added:
                                self.logger.info(f"Instagram REST comments fallback added {rest_added} comments")
                        if len(comments_by_id) == before:
                            self.logger.debug("Instagram early direct GraphQL did not add comments; falling back to UI pagination")
                except Exception as exc:
                    self.logger.debug(f"Instagram early direct GraphQL failed: {exc}")

            if navigation_error and not media_item and not comments_by_id:
                raise RuntimeError(f"post navigation failed and direct media lookup returned no media: {navigation_error}")

            rounds = 10 if not max_comments or max_comments <= 0 else max(2, min(10, (max_comments // 25) + 1))
            if page_loaded and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments):
                await self._load_more_comments(page, rounds)

            if response_tasks:
                _, pending = await asyncio.wait(response_tasks, timeout=10)
                for task in pending:
                    task.cancel()
                if pending:
                    self.logger.debug(f"Cancelled {len(pending)} slow Instagram response capture task(s)")

            if comment_api_templates and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments):
                direct_state = {}
                added = await self._fetch_comments_direct_api(
                    page,
                    comment_api_templates[-1],
                    video_id,
                    comments_by_id,
                    max_comments,
                    direct_state,
                )
                pagination_states.append({"method": "graphql", **direct_state})
                if added:
                    self.logger.info(f"Instagram direct GraphQL added {added} comments")

            media_comment_count = self._comment_count_from_media_item(media_item or {}) if media_item else media_comment_count
            media_id = media_item.get("id") or media_item.get("pk") if isinstance(media_item, dict) else video_data.get("media_id")
            if (
                isinstance(media_comment_count, int)
                and len(comments_by_id) < media_comment_count
                and media_id
                and not rest_attempted
                and (not max_comments or max_comments <= 0 or len(comments_by_id) < max_comments)
            ):
                rest_state = {}
                rest_added = await self._fetch_comments_rest_api(
                    page,
                    str(media_id),
                    video_id,
                    comments_by_id,
                    max_comments,
                    rest_state,
                )
                rest_attempted = True
                pagination_states.append({"method": "rest", **rest_state})
                if rest_added:
                    self.logger.info(f"Instagram final REST pass added {rest_added} comments")

            if page_loaded and not comments_by_id:
                for comment in await self._extract_dom_comments(page, video_id):
                    comments_by_id[comment["comment_id"]] = comment
                    dom_comments_used = True

            comments_seen_in_response = len(comments_by_id)
            comments = list(comments_by_id.values())
            if max_comments and max_comments > 0:
                comments = comments[:max_comments]

            comment_limit_reached = bool(
                max_comments
                and max_comments > 0
                and comments_seen_in_response >= max_comments
            )
            if comment_limit_reached:
                comments_exhausted = False
            elif isinstance(media_comment_count, int):
                comments_exhausted = comments_seen_in_response >= media_comment_count
            elif pagination_states:
                comments_exhausted = bool(pagination_states[-1].get("exhausted"))
            else:
                comments_exhausted = None
            if dom_comments_used:
                comment_method = "browser_dom"
            elif api_first_used or direct_only_enabled or graphql_template:
                comment_method = "instagram_direct_api"
            else:
                comment_method = "browser_network_api"

            media_metrics = self._media_metrics(media_item) if isinstance(media_item, dict) else {}
            for key in (
                "published_at",
                "content_type",
                "view_count",
                "like_count",
                "share_count",
                "save_count",
                "follower_count",
                "creator_id",
                "creator_display_name",
                "creator_verified",
            ):
                if media_metrics.get(key) in (None, "") and video_data.get(key) not in (None, ""):
                    media_metrics[key] = video_data.get(key)
            availability = dict(video_data.get("metric_availability") or {})
            for metric, status in (media_metrics.get("metric_availability") or {}).items():
                if status == "available" or availability.get(metric) != "available":
                    availability[metric] = status
            profile_metrics = await self._fetch_profile_metrics(page, username or "")
            if profile_metrics.get("follower_count") is not None:
                media_metrics["follower_count"] = profile_metrics["follower_count"]
                availability["followers"] = "available"
            for key in ("creator_id", "creator_display_name", "creator_verified"):
                if media_metrics.get(key) in (None, "") and profile_metrics.get(key) not in (None, ""):
                    media_metrics[key] = profile_metrics.get(key)
            creator_profile = dict(media_metrics.get("creator_profile") or {})
            for key, value in (profile_metrics.get("creator_profile") or {}).items():
                if value not in (None, "", [], {}):
                    creator_profile[key] = value
            if creator_profile:
                media_metrics["creator_profile"] = creator_profile
            media_metrics["metric_availability"] = availability

            self.logger.info(f"Extracted {len(comments)} Instagram comments from {canonical_url}")
            return {
                "video_id": video_id,
                "media_id": str(media_item.get("id") or media_item.get("pk")) if isinstance(media_item, dict) and (media_item.get("id") or media_item.get("pk")) else video_data.get("media_id"),
                "url": canonical_url,
                "caption": caption,
                "username": username or "Instagram User",
                "reported_comment_count": media_comment_count,
                **media_metrics,
                "discovery_method": video_data.get("discovery_method", "direct_url"),
                "metadata_method": "instagram_graphql_api" if media_item else "instagram_page_meta",
                "comment_method": comment_method,
                "comments_seen_in_response": comments_seen_in_response,
                "comment_limit_reached": comment_limit_reached,
                "comments_exhausted": comments_exhausted,
                "pagination_requests": sum(int(state.get("pages") or 0) for state in pagination_states),
                "pagination_cursor_present": any(
                    bool(state.get("cursor_present")) and not bool(state.get("exhausted"))
                    for state in pagination_states
                ),
                "pagination_stopped_reasons": [
                    f"{state.get('method')}:{state.get('stopped_reason')}"
                    for state in pagination_states
                ],
                "fallback_used": dom_comments_used,
                "comments": comments,
                "error": "",
            }
        except Exception as e:
            self.logger.error(f"Error extracting Instagram comments for {url}: {e}")
            return {
                "video_id": video_id,
                "url": url,
                "caption": video_data.get("title", ""),
                "username": video_data.get("username", "Instagram User"),
                "discovery_method": video_data.get("discovery_method", "direct_url"),
                "metadata_method": "unknown",
                "comment_method": "instagram_direct_api",
                "comments_seen_in_response": 0,
                "comment_limit_reached": False,
                "comments_exhausted": None,
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
