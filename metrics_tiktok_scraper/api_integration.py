"""
TikTok API Integration Module

This module integrates the programmatic TikTok API with the browser-based scraper.
It provides functionality to:
1. Extract security tokens from the browser session
2. Use those tokens for direct API calls
3. Fall back to browser automation when API calls fail
"""

import os
import json
import sys
import re
import asyncio
import logging
import time
import urllib.parse
from typing import Dict, List, Optional, Any, Tuple, Set

# Add parent directory to path to import tiktok_api.py
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

try:
    from tiktok_api import TikTokAPI
except ImportError:
    print("[ERROR] Could not import TikTokAPI. Ensure tiktok_api.py is in the parent directory.")
    # Define a mock class to avoid errors if the import failed
    class TikTokAPI:
        def __init__(self, *args, **kwargs):
            pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('api_integration')


class TikTokSearchSessionError(RuntimeError):
    """Raised when TikTok search is inaccessible, not legitimately empty."""


TIKTOK_SEARCH_API_PATHS = (
    "/api/search/item/full/",
    "/api/search/general/full/",
)


def sanitize_search_keyword(value: Any) -> str:
    """Remove quote operators because TikTok search treats them literally."""
    keyword = str(value or "").strip()
    keyword = keyword.replace('"', " ").replace("“", " ").replace("”", " ")
    keyword = keyword.replace("'", " ").replace("‘", " ").replace("’", " ")
    return re.sub(r"\s+", " ", keyword).strip()

class TikTokAPIIntegration:
    """Integrates TikTokAPI with browser-based scraper."""

    def __init__(
        self,
        enable_api: bool = True,
        debug: bool = False,
        persist_session_secrets: bool = False,
    ):
        """Initialize the integration.

        Args:
            enable_api: Whether to enable API-based comment fetching
            debug: Enable debug logging
            persist_session_secrets: Opt-in legacy local credential storage.
                Disabled by default and prohibited for ENGAGE.
        """
        self.enable_api = enable_api
        self.api = None
        self.persist_session_secrets = bool(persist_session_secrets)
        self.cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "data", "tiktok_cookies.json")
        self.tokens_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "data", "tiktok_tokens.json")

        if self.persist_session_secrets:
            os.makedirs(os.path.dirname(self.cookies_file), exist_ok=True)

        # Set up logging
        if debug:
            logging.getLogger('api_integration').setLevel(logging.DEBUG)
            logging.getLogger('tiktok_api').setLevel(logging.DEBUG)

    async def extract_security_tokens_from_browser(self, page) -> Dict[str, str]:
        """Extract security tokens from browser session.

        Args:
            page: Playwright page object

        Returns:
            Dictionary containing security tokens
        """
        try:
            # Extract msToken from localStorage - use await properly
            ms_token = await page.evaluate("""() => {
                return localStorage.getItem('msToken') || '';
            }""")

            # Extract other cookies that might be relevant - use await properly
            cookies = await page.context.cookies()
            cookie_dict = {cookie['name']: cookie['value'] for cookie in cookies}

            # Extract some additional data that might be needed for API calls - use await properly
            user_agent = await page.evaluate("""() => navigator.userAgent""")

            # Extract additional security tokens from TikTok's page - use await properly
            additional_tokens = await page.evaluate("""() => {
                const tokens = {};

                // Try to extract X-Bogus token generator if present
                try {
                    // Look for X-Bogus in network requests
                    const xbogus = window.byted_acrawler && window.byted_acrawler.sign;
                    tokens.has_xbogus = !!xbogus;
                } catch (e) {
                    tokens.has_xbogus = false;
                }

                // Try to extract verifyFp / _signature
                try {
                    tokens.verifyFp = localStorage.getItem('s_v_web_id') || '';
                    tokens.ttwid = document.cookie.split('; ').find(row => row.startsWith('ttwid='))?.split('=')[1] || '';
                } catch (e) {
                    console.error("Error extracting verify tokens:", e);
                }

                // Extract any CSRF token if present
                try {
                    tokens.csrf_token = document.cookie.split('; ').find(row => row.startsWith('csrf_session_id='))?.split('=')[1] || '';
                } catch (e) {}

                return tokens;
            }""")

            # Create tokens dictionary
            tokens = {
                "msToken": ms_token,
                "user_agent": user_agent,
                # Add security cookies that are often needed for API calls
                "ttwid": cookie_dict.get('ttwid', ''),
                "csrf_session_id": cookie_dict.get('csrf_session_id', ''),
                "csrf_token": additional_tokens.get('csrf_token', ''),
                "verifyFp": additional_tokens.get('verifyFp', ''),
                "s_v_web_id": cookie_dict.get('s_v_web_id', additional_tokens.get('verifyFp', '')),
                "has_xbogus": additional_tokens.get('has_xbogus', False),
                "timestamp": int(time.time())
            }

            if self.persist_session_secrets:
                with open(self.tokens_file, 'w', encoding='utf-8') as f:
                    json.dump(tokens, f)

            logger.info("Extracted security tokens from browser for in-memory use")
            logger.debug(
                "Security token fields present: %s",
                sorted(key for key, value in tokens.items() if value not in (None, "")),
            )

            return tokens

        except Exception as e:
            logger.error(f"Failed to extract security tokens from browser: {e}")
            return {}

    async def extract_cookies_from_browser(self, page) -> Dict[str, str]:
        """Extract cookies from browser session.

        Args:
            page: Playwright page object

        Returns:
            Dictionary containing cookies
        """
        try:
            # Get all cookies from the browser - use await properly
            cookies = await page.context.cookies()

            # Convert cookies list to dictionary
            cookie_dict = {cookie['name']: cookie['value'] for cookie in cookies}

            if self.persist_session_secrets:
                with open(self.cookies_file, 'w', encoding='utf-8') as f:
                    json.dump(cookie_dict, f)

            logger.info(f"Extracted {len(cookie_dict)} cookies from browser")
            return cookie_dict

        except Exception as e:
            logger.error(f"Failed to extract cookies from browser: {e}")
            return {}

    async def initialize_api(self, page=None):
        """Initialize the TikTok API.

        Args:
            page: Optional Playwright page object to extract tokens/cookies

        Returns:
            bool: Whether API initialization was successful
        """
        if not self.enable_api:
            logger.info("API is disabled. Using browser-based scraping only.")
            return False

        try:
            # Extract tokens and cookies from browser if available
            tokens = {}
            cookies = {}

            if page:
                tokens = await self.extract_security_tokens_from_browser(page)
                cookies = await self.extract_cookies_from_browser(page)

            # Otherwise try to load from files
            elif self.persist_session_secrets and os.path.exists(self.tokens_file):
                with open(self.tokens_file, 'r') as f:
                    tokens = json.load(f)
                logger.info(f"Loaded security tokens from {self.tokens_file}")

                if os.path.exists(self.cookies_file):
                    with open(self.cookies_file, 'r') as f:
                        cookies = json.load(f)
                    logger.info(f"Loaded cookies from {self.cookies_file}")

            # Initialize the API with the extracted/loaded tokens
            self.api = TikTokAPI(
                user_agent=tokens.get('user_agent'),
                # We can pass device_id, but it's typically generated
                # Add custom token initialization if needed
            )

            # Set cookies directly on the session
            for name, value in cookies.items():
                self.api.session.cookies.set(name, value)

            # Update API tokens with our extracted tokens
            if 'msToken' in tokens and tokens['msToken']:
                self.api.ms_token = tokens['msToken']
                logger.info("Set msToken on API")

            # Set additional tokens
            if 'verifyFp' in tokens and tokens['verifyFp']:
                self.api.verify_fp = tokens['verifyFp']
                logger.info("Set verifyFp on API")

            if 's_v_web_id' in tokens and tokens['s_v_web_id']:
                self.api.s_v_web_id = tokens['s_v_web_id']
                logger.info("Set s_v_web_id on API")

            if 'csrf_token' in tokens and tokens['csrf_token']:
                self.api.csrf_token = tokens['csrf_token']
                logger.info("Set csrf_token on API")

            # Test if the API connection works by making a small request
            self.api.session.headers.update({
                'User-Agent': tokens.get('user_agent', ''),
                'Referer': 'https://www.tiktok.com/',
                'Origin': 'https://www.tiktok.com'
            })

            logger.info("TikTok API initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize TikTok API: {e}")
            self.api = None
            return False

    async def get_comments_for_video(self, video_id: str, video_url: str = None, max_comments: int = 0) -> List[Dict[str, Any]]:
        """Get comments for a video using the API if possible.

        Args:
            video_id: TikTok video ID
            video_url: Optional video URL (used to extract video_id if not provided)
            max_comments: Maximum number of comments to fetch

        Returns:
            List of comment objects
        """
        if not self.enable_api or not self.api:
            logger.info("API disabled or not initialized. Cannot get comments.")
            return []

        try:
            # Extract video_id from URL if needed
            if not video_id and video_url:
                video_id = self.extract_video_id_from_url(video_url)

            if not video_id:
                logger.error("No video ID provided and could not extract from URL")
                return []

            logger.info(f"Fetching comments for video {video_id} using API")
            comments = await self.api.get_all_comments(video_id, max_comments=max_comments)

            logger.info(f"Successfully fetched {len(comments)} comments via API")
            return comments

        except Exception as e:
            logger.error(f"Error fetching comments via API: {e}")
            return []

    def _build_search_variants(self, keyword: str) -> List[str]:
        """Build related search phrases to improve video discovery coverage."""
        normalized = sanitize_search_keyword(keyword)
        if not normalized:
            return []

        variants = [normalized]
        lower = normalized.lower()
        tokens = normalized.split()
        year_tokens = [token for token in tokens if re.fullmatch(r"20\d{2}", token)]
        no_year = " ".join(token for token in tokens if token not in year_tokens).strip()

        if no_year and no_year.lower() != lower:
            variants.append(no_year)

        # TikTok web search returns a small ranked page per query. For Pesparawi,
        # nearby event/location modifiers uncover additional relevant result sets.
        if "pesparawi" in lower:
            base = "pesparawi"
            modifiers = [
                "2026",
                "nasional",
                "nasional 2026",
                "papua",
                "papua 2026",
                "jayapura",
                "maluku",
                "indonesia",
            ]
            for modifier in modifiers:
                variants.append(f"{base} {modifier}".strip())

        unique = []
        seen = set()
        for variant in variants:
            key = variant.lower()
            if key not in seen:
                unique.append(variant)
                seen.add(key)
        return unique

    def _extract_video_from_search_item(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a video id/url/caption tuple from one search result row."""
        if not isinstance(row, dict):
            return None

        item = row.get("item") or row.get("itemStruct") or row.get("aweme_info") or row.get("awemeInfo") or row
        if not isinstance(item, dict):
            return None

        video_id = str(item.get("id") or item.get("aweme_id") or item.get("item_id") or "")
        if not video_id:
            return None

        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        username = (
            author.get("unique_id")
            or author.get("uniqueId")
            or author.get("sec_uid")
            or "unknown"
        )
        caption = item.get("desc") or item.get("description") or ""
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        author_stats = item.get("authorStats") or item.get("author_stats") or {}
        if not isinstance(author_stats, dict):
            author_stats = {}
        music = item.get("music") if isinstance(item.get("music"), dict) else {}
        video = item.get("video") if isinstance(item.get("video"), dict) else {}

        def present(container: Dict[str, Any], *keys: str) -> tuple[Any, bool]:
            for key in keys:
                if key in container and container.get(key) not in (None, ""):
                    return container.get(key), True
            return None, False

        follower_count, followers_present = present(author_stats, "followerCount", "follower_count")
        view_count, views_present = present(stats, "playCount", "play_count")
        like_count, likes_present = present(stats, "diggCount", "digg_count")
        comment_count, comments_present = present(stats, "commentCount", "comment_count")
        share_count, shares_present = present(stats, "shareCount", "share_count")
        save_count, saves_present = present(stats, "collectCount", "collect_count")

        return {
            "id": video_id,
            "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
            "caption": caption,
            "username": username if username != "unknown" else "",
            "creator_id": author.get("id") or author.get("uid") or author.get("secUid") or author.get("sec_uid") or "",
            "creator_display_name": author.get("nickname") or author.get("display_name") or "",
            "creator_verified": author.get("verified"),
            "creator_profile": {
                "user_id": author.get("id") or author.get("uid") or "",
                "username": author.get("unique_id") or author.get("uniqueId") or "",
                "display_name": author.get("nickname") or author.get("display_name") or "",
                "bio": author.get("signature") or "",
                "verified": author.get("verified"),
                "avatar_url": (
                    author.get("avatarLarger")
                    or author.get("avatarMedium")
                    or author.get("avatarThumb")
                    or ""
                ),
                "follower_count": follower_count,
                "following_count": (
                    author_stats.get("followingCount")
                    or author_stats.get("following_count")
                ),
                "content_count": (
                    author_stats.get("videoCount")
                    or author_stats.get("video_count")
                ),
                "likes_count": (
                    author_stats.get("heartCount")
                    or author_stats.get("heart")
                    or author_stats.get("likes_count")
                ),
            },
            "follower_count": follower_count,
            "create_time": item.get("create_time") or item.get("createTime") or "",
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "share_count": share_count,
            "save_count": save_count,
            "metric_availability": {
                "views": "available" if views_present else "missing_from_public_response",
                "likes": "available" if likes_present else "missing_from_public_response",
                "reported_comments": "available" if comments_present else "missing_from_public_response",
                "shares": "available" if shares_present else "missing_from_public_response",
                "saves": "available" if saves_present else "missing_from_public_response",
                "followers": "available" if followers_present else "missing_from_public_response",
            },
            "music_id": music.get("id") or music.get("mid") or "",
            "music_title": music.get("title") or music.get("musicName") or "",
            "music_author": music.get("authorName") or music.get("author_name") or "",
            "duration_seconds": video.get("duration") or 0,
            "thumbnail_url": video.get("cover") or video.get("originCover") or video.get("dynamicCover") or "",
            "content_type": "video",
        }

    @staticmethod
    def _merge_oembed_metadata(video: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """Merge public post metadata without fetching the comment thread."""
        title = str(payload.get("title") or "").strip()
        if not title:
            return False
        video["title"] = title
        video["caption"] = title
        author_name = str(payload.get("author_name") or "").strip()
        author_url = str(payload.get("author_url") or "").strip()
        handle_match = re.search(r"tiktok\.com/@([^/?#]+)", author_url)
        if handle_match:
            video["username"] = handle_match.group(1)
        elif not video.get("username") and author_name:
            video["username"] = author_name.lstrip("@")
        if author_name and not video.get("creator_display_name"):
            video["creator_display_name"] = author_name
        thumbnail_url = str(payload.get("thumbnail_url") or "").strip()
        if thumbnail_url and not video.get("thumbnail_url"):
            video["thumbnail_url"] = thumbnail_url
        video["metadata_method"] = "tiktok_oembed"
        video["metadata_hydration_method"] = "tiktok_oembed"
        return True

    async def hydrate_video_candidates(self, page, videos: List[Dict[str, Any]]) -> Dict[str, int]:
        """Hydrate weak TikTok cards through the public oEmbed metadata endpoint."""
        candidates = [video for video in videos if video.get("url")]
        limit_text = os.environ.get("TIKTOK_METADATA_HYDRATION_LIMIT", "0").strip()
        try:
            limit = max(0, int(limit_text or 0))
        except ValueError:
            limit = 0
        if limit:
            candidates = candidates[:limit]
        stats = {"eligible": len(videos), "attempted": 0, "hydrated": 0, "failed": 0}
        if not candidates:
            return stats

        try:
            timeout_ms = max(1000, int(float(os.environ.get("TIKTOK_METADATA_TIMEOUT_SECONDS", "8")) * 1000))
        except ValueError:
            timeout_ms = 8000
        try:
            concurrency = max(1, int(os.environ.get("TIKTOK_METADATA_CONCURRENCY", "4")))
        except ValueError:
            concurrency = 4
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch(video: Dict[str, Any]) -> bool:
            endpoint = "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(
                str(video.get("url") or ""),
                safe="",
            )
            async with semaphore:
                stats["attempted"] += 1
                try:
                    response = await page.request.get(endpoint, timeout=timeout_ms)
                    if not response.ok:
                        stats["failed"] += 1
                        return False
                    payload = await response.json()
                    if not isinstance(payload, dict) or not self._merge_oembed_metadata(video, payload):
                        stats["failed"] += 1
                        return False
                    stats["hydrated"] += 1
                    return True
                except Exception as exc:
                    stats["failed"] += 1
                    logger.debug(f"TikTok metadata hydration failed for {video.get('url')}: {exc}")
                    return False

        # Probe a few records first. If the endpoint is unavailable for this
        # session, avoid turning a cheap preflight into hundreds of timeouts.
        probe_count = min(3, len(candidates))
        probe_succeeded = False
        for video in candidates[:probe_count]:
            if await fetch(video):
                probe_succeeded = True
                break
        if not probe_succeeded:
            return stats

        attempted_ids = {id(video) for video in candidates[:stats["attempted"]]}
        remaining = [video for video in candidates if id(video) not in attempted_ids]
        if remaining:
            await asyncio.gather(*(fetch(video) for video in remaining))
        return stats

    async def _search_session_diagnostics(self, page) -> Dict[str, Any]:
        """Return non-sensitive signals that distinguish empty search from a bad session."""
        diagnostics = {
            "authenticated": False,
            "cookie_count": 0,
            "page_no_results": False,
            "login_prompt": False,
        }
        try:
            cookies = await page.context.cookies(["https://www.tiktok.com"])
            cookie_names = {str(cookie.get("name") or "") for cookie in cookies}
            diagnostics["cookie_count"] = len(cookie_names)
            diagnostics["authenticated"] = bool(
                {"sessionid", "sessionid_ss", "sid_tt"} & cookie_names
            )
        except Exception:
            pass

        try:
            body_text = (await page.locator("body").inner_text(timeout=3000)).casefold()
            diagnostics["page_no_results"] = "no results found" in body_text
            diagnostics["login_prompt"] = "log in" in body_text or "sign up" in body_text
        except Exception:
            pass
        return diagnostics

    @staticmethod
    def _search_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return result rows from either current TikTok search response shape."""
        if not isinstance(data, dict):
            return []
        rows = data.get("item_list") or data.get("itemList") or data.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("item_list") or rows.get("itemList") or rows.get("data") or []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def _capture_search_api_pages(
        self,
        page,
        keyword: str,
        max_pages: int,
    ) -> List[Dict[str, Any]]:
        """Capture search JSON that TikTok signs and issues from its own web app.

        Scrolling is only used to trigger pagination. All candidate metadata is
        read from network responses, so this remains an API-derived transport
        and does not depend on rendered result cards.
        """
        keyword = sanitize_search_keyword(keyword)
        target_url = f"https://www.tiktok.com/search/video?q={urllib.parse.quote(keyword)}"
        captured: List[Dict[str, Any]] = []
        seen_urls: Set[str] = set()
        tasks: Set[asyncio.Task] = set()
        response_event = asyncio.Event()

        async def capture(response) -> None:
            if not any(path in response.url for path in TIKTOK_SEARCH_API_PATHS):
                return
            if response.url in seen_urls:
                return
            seen_urls.add(response.url)
            try:
                text = await response.text()
            except Exception:
                text = ""
            try:
                data = json.loads(text) if text else None
            except json.JSONDecodeError:
                data = None
            captured.append({
                "url": response.url,
                "http": response.status,
                "bodyLen": len(text),
                "data": data,
            })
            response_event.set()

        def schedule(response) -> None:
            if not any(path in response.url for path in TIKTOK_SEARCH_API_PATHS):
                return
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        try:
            page.on("response", schedule)
        except Exception:
            return []
        try:
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                logger.debug("TikTok search navigation warning for '%s': %s", keyword, exc)

            try:
                await asyncio.wait_for(response_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

            stall_rounds = 0
            while len(captured) < max(1, max_pages) and stall_rounds < 3:
                before = len(captured)
                response_event.clear()
                try:
                    await page.mouse.wheel(0, 2400)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=2.5)
                except asyncio.TimeoutError:
                    pass
                if len(captured) == before:
                    stall_rounds += 1
                else:
                    stall_rounds = 0

            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=5)
                for task in pending:
                    task.cancel()
        finally:
            try:
                page.remove_listener("response", schedule)
            except Exception:
                pass
            for task in list(tasks):
                task.cancel()

        return captured[:max(1, max_pages)]

    async def _get_search_template_url(self, page, keyword: str) -> Optional[str]:
        """Find TikTok's current full-search API request URL from the page."""
        keyword = sanitize_search_keyword(keyword)
        urls = await page.evaluate("""() => {
            const paths = ['/api/search/item/full/', '/api/search/general/full/'];
            return performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(name => paths.some(path => name.includes(path)));
        }""")
        if urls:
            return urls[-1]

        search_url = f"https://www.tiktok.com/search/video?q={urllib.parse.quote(keyword)}"
        captured = []

        async def on_response(response):
            if any(path in response.url for path in TIKTOK_SEARCH_API_PATHS):
                captured.append(response.url)

        page.on("response", on_response)
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(8000)

        if captured:
            return captured[-1]

        urls = await page.evaluate("""() => {
            const paths = ['/api/search/item/full/', '/api/search/general/full/'];
            return performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(name => paths.some(path => name.includes(path)));
        }""")
        return urls[-1] if urls else None

    def _search_url_with_updates(self, template_url: str, updates: Dict[str, Any]) -> str:
        """Build a search API URL from the browser template with fresh params."""
        # msToken is session-bound but not tied to a specific cursor. Signatures
        # cover the query string and must not be reused after parameter changes.
        sensitive = {"X-Bogus", "X-Gnarly"}
        parsed = urllib.parse.urlsplit(template_url)
        pairs = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key not in sensitive
        ]

        output = []
        seen = set()
        for key, value in pairs:
            if key in updates:
                output.append((key, str(updates[key])))
                seen.add(key)
            else:
                output.append((key, value))

        for key, value in updates.items():
            if key not in seen:
                output.append((key, str(value)))

        return urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(output),
            parsed.fragment,
        ))

    def _extract_search_id(self, data: Dict[str, Any]) -> str:
        """Extract the search pagination id TikTok expects after page one."""
        if not isinstance(data, dict):
            return ""

        log_pb = data.get("log_pb") if isinstance(data.get("log_pb"), dict) else {}
        extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
        return (
            str(log_pb.get("impr_id") or "")
            or str(extra.get("logid") or "")
            or str(extra.get("search_request_id") or "")
        )

    async def discover_search_videos(
        self,
        page,
        keyword: str,
        max_offsets: int = 25,
        include_related_queries: bool = False,
    ) -> List[Dict[str, str]]:
        """Discover TikTok videos by calling the browser-backed search API."""
        normalized = sanitize_search_keyword(keyword)
        variants = self._build_search_variants(keyword) if include_related_queries else [normalized]
        variants = [variant for variant in variants if variant]
        videos_by_id: Dict[str, Dict[str, str]] = {}

        logger.info(
            "Capturing TikTok-signed search responses for %s exact query%s, up to %s pages each",
            len(variants),
            " variants" if include_related_queries else "",
            max_offsets,
        )
        capture_seen = False
        valid_capture_seen = False
        capture_errors: List[Dict[str, Any]] = []
        for variant in variants:
            captured_pages = await self._capture_search_api_pages(page, variant, max_offsets)
            if not captured_pages:
                continue
            capture_seen = True
            for page_number, result in enumerate(captured_pages, start=1):
                data = result.get("data") if isinstance(result, dict) else None
                status_code = data.get("status_code", data.get("statusCode", 0)) if isinstance(data, dict) else None
                if not isinstance(data, dict) or status_code != 0:
                    capture_errors.append(result)
                    continue
                valid_capture_seen = True
                rows = self._search_rows(data)
                for row in rows:
                    video = self._extract_video_from_search_item(row)
                    if video:
                        video.setdefault("discovery_method", "tiktok_search_api_live_capture")
                        video.setdefault("discovery_source", "tiktok_search_api")
                        video.setdefault("metadata_method", "tiktok_search_api_response")
                        videos_by_id.setdefault(video["id"], video)
                logger.info(
                    "Captured Search API '%s' page %s with %s rows; total unique=%s",
                    variant,
                    page_number,
                    len(rows),
                    len(videos_by_id),
                )

        if valid_capture_seen:
            videos = list(videos_by_id.values())
            logger.info("Discovered %s unique TikTok videos from live Search API responses", len(videos))
            return videos

        if capture_seen:
            first_error = capture_errors[0] if capture_errors else {}
            diagnostics = await self._search_session_diagnostics(page)
            raise TikTokSearchSessionError(
                "TikTok emitted search requests but returned no valid JSON: "
                f"http={first_error.get('http')}, body_len={first_error.get('bodyLen')}, "
                f"authenticated={diagnostics['authenticated']}, "
                f"page_no_results={diagnostics['page_no_results']}, "
                f"login_prompt={diagnostics['login_prompt']}. "
                "This is a blocked or invalid search session, not a confirmed zero-result query."
            )

        logger.warning("No live TikTok Search API response was captured; trying exact signed replay fallback")
        template_url = await self._get_search_template_url(page, keyword)
        if not template_url:
            diagnostics = await self._search_session_diagnostics(page)
            raise TikTokSearchSessionError(
                "TikTok search session is unusable: no search API request was captured "
                f"(authenticated={diagnostics['authenticated']}, "
                f"page_no_results={diagnostics['page_no_results']}, "
                f"login_prompt={diagnostics['login_prompt']})."
            )

        template_params = urllib.parse.parse_qs(urllib.parse.urlsplit(template_url).query)
        template_keyword = " ".join((template_params.get("keyword") or [""])[0].split()).strip()
        logger.info(
            "Discovering TikTok search videos using %s exact query%s, up to %s pages each",
            len(variants),
            " variants" if include_related_queries else "",
            max_offsets,
        )

        fetch_js = """async ({url}) => {
            const response = await fetch(url, { credentials: 'include' });
            const text = await response.text();
            let data = null;
            try { data = JSON.parse(text); } catch (e) {}
            return {
                http: response.status,
                bodyLen: text.length,
                data
            };
        }"""

        for variant in variants:
            cursor = 0
            search_id = ""

            for page_number in range(1, max_offsets + 1):
                updates = {
                    "keyword": variant,
                    "offset": cursor,
                    "cursor": cursor,
                    "count": 12,
                }
                if search_id:
                    updates["search_id"] = search_id

                exact_signed_replay = (
                    page_number == 1
                    and variant.casefold() == (template_keyword or normalized).casefold()
                    and not search_id
                )
                url = template_url if exact_signed_replay else self._search_url_with_updates(template_url, updates)

                try:
                    result = await page.evaluate(fetch_js, {"url": url})
                except Exception as e:
                    logger.warning(f"Search API fetch failed for '{variant}' page {page_number} cursor {cursor}: {e}")
                    break

                data = result.get("data") if isinstance(result, dict) else None
                status_code = data.get("status_code", data.get("statusCode", 0)) if isinstance(data, dict) else None
                if (not isinstance(data, dict) or status_code != 0) and page_number == 1:
                    logger.info("Retrying the first TikTok search API response once")
                    await page.wait_for_timeout(1500)
                    try:
                        result = await page.evaluate(fetch_js, {"url": url})
                    except Exception as e:
                        logger.warning(f"Search API retry failed for '{variant}': {e}")
                    data = result.get("data") if isinstance(result, dict) else None
                    status_code = data.get("status_code", data.get("statusCode", 0)) if isinstance(data, dict) else None
                if not isinstance(data, dict) or status_code != 0:
                    logger.warning(
                        "Search API returned invalid data for '%s' page %s: http=%s status_code=%s body_len=%s signed_replay=%s",
                        variant,
                        page_number,
                        result.get("http") if isinstance(result, dict) else None,
                        status_code,
                        result.get("bodyLen") if isinstance(result, dict) else None,
                        exact_signed_replay,
                    )
                    diagnostics = await self._search_session_diagnostics(page)
                    raise TikTokSearchSessionError(
                        f"TikTok search access failed for '{variant}': "
                        f"http={result.get('http') if isinstance(result, dict) else None}, "
                        f"body_len={result.get('bodyLen') if isinstance(result, dict) else None}, "
                        f"status_code={status_code}, "
                        f"authenticated={diagnostics['authenticated']}, "
                        f"page_no_results={diagnostics['page_no_results']}, "
                        f"login_prompt={diagnostics['login_prompt']}. "
                        "This is a blocked or invalid search session, not a confirmed zero-result query."
                    )
                    break

                rows = self._search_rows(data)
                if not rows:
                    logger.info(f"Search API returned no rows for '{variant}' page {page_number} cursor {cursor}")
                    break

                if not search_id:
                    search_id = self._extract_search_id(data)
                    if search_id:
                        logger.info(f"Captured TikTok search_id for '{variant}' pagination")

                for row in rows:
                    video = self._extract_video_from_search_item(row)
                    if video:
                        video.setdefault("discovery_method", "tiktok_search_api")
                        video.setdefault("discovery_source", "tiktok_search_api")
                        video.setdefault("metadata_method", "tiktok_search_api")
                        videos_by_id.setdefault(video["id"], video)

                has_more = bool(data.get("has_more", data.get("hasMore", False)))
                logger.info(
                    "Search API '%s' page %s cursor %s returned %s rows; has_more=%s; total unique=%s",
                    variant,
                    page_number,
                    cursor,
                    len(rows),
                    has_more,
                    len(videos_by_id),
                )

                if not has_more:
                    break

                next_cursor = data.get("cursor")
                if next_cursor is None:
                    next_cursor = cursor + len(rows)
                try:
                    next_cursor = int(next_cursor)
                except (TypeError, ValueError):
                    next_cursor = cursor + len(rows)

                if next_cursor <= cursor:
                    logger.info(f"Search API cursor did not advance for '{variant}' page {page_number}; stopping")
                    break

                cursor = next_cursor

                await page.wait_for_timeout(200)

        videos = list(videos_by_id.values())
        logger.info(f"Discovered {len(videos)} unique TikTok videos from search API")
        return videos

    async def get_comments_for_multiple_videos(self, page, videos: List[Dict[str, str]], max_comments: int = 0, concurrency: int = 3) -> Dict[str, Dict[str, Any]]:
        """Fetch comments for multiple videos concurrently.

        Args:
            videos: List of dicts with 'id' and 'url' keys
            max_comments: Maximum comments per video
            concurrency: Max number of concurrent video fetching tasks

        Returns:
            Dict mapping video_id to dict containing comments, caption, and username
        """
        if not self.enable_api or not self.api:
            return {
                v.get("id", ""): {
                    "comments": [],
                    "caption": "",
                    "username": "",
                    "ok": False,
                    "complete": False,
                    "exhausted": False,
                    "limit_reached": False,
                    "has_more": True,
                    "error": "api_not_initialized",
                    "source": "direct_api",
                }
                for v in videos
            }

        semaphore = asyncio.Semaphore(max(1, concurrency))
        results = {}

        async def fetch_video_direct(video):
            vid = video.get("id")
            vurl = video.get("url")
            if not vid and vurl:
                vid = self.extract_video_id_from_url(vurl)

            if not vid:
                return "", {
                    "comments": [],
                    "caption": "",
                    "username": "",
                    "ok": False,
                    "complete": False,
                    "exhausted": False,
                    "limit_reached": False,
                    "has_more": True,
                    "error": "missing_video_id",
                    "source": "direct_api",
                }

            caption = video.get("caption", "")
            username = video.get("username", "")
            if not username and vurl:
                username_match = re.search(r'/@([^/?]+)', vurl)
                if username_match:
                    username = username_match.group(1)

            async with semaphore:
                try:
                    logger.info(f"Direct API fetching video {vid}")
                    result = await self.api.get_all_comments(
                        vid,
                        max_comments=max_comments,
                        return_status=True,
                    )
                    if not isinstance(result, dict):
                        raise RuntimeError("TikTok API returned a legacy comment list without completion status")
                    return vid, {
                        "comments": result.get("comments", []),
                        "caption": caption,
                        "username": username,
                        "ok": bool(result.get("ok")),
                        "complete": bool(result.get("complete")),
                        "exhausted": bool(result.get("exhausted")),
                        "limit_reached": bool(result.get("limit_reached")),
                        "has_more": bool(result.get("has_more")),
                        "error": result.get("error", ""),
                        "source": "direct_api",
                    }
                except Exception as e:
                    logger.error(f"Error concurrently fetching video {vid} via direct API: {e}")
                    return vid, {
                        "comments": [],
                        "caption": caption,
                        "username": username,
                        "ok": False,
                        "complete": False,
                        "exhausted": False,
                        "limit_reached": False,
                        "has_more": True,
                        "error": str(e),
                        "source": "direct_api",
                    }

        completed = await asyncio.gather(*(fetch_video_direct(v) for v in videos))

        for vid, data in completed:
            if vid:
                results[vid] = data

        return results

        semaphore = asyncio.Semaphore(concurrency)
        results = {}

        async def fetch_video(video):
            vid = video.get("id")
            vurl = video.get("url")
            if not vid and vurl:
                vid = self.extract_video_id_from_url(vurl)

            if not vid or not vurl:
                return vid, []

            async with semaphore:
                try:
                    logger.info(f"Opening background tab for video {vid}")
                    new_page = await page.context.new_page()
                    try:
                        from playwright_stealth import Stealth
                        await Stealth().apply_stealth_async(new_page)
                        logger.info("Applied Playwright stealth to background tab.")
                    except ImportError:
                        logger.warning("playwright_stealth not installed, skipping stealth mode.")

                    comments = []

                    caption = ""
                    username = ""

                    async def handle_resp(r):
                        if "/api/comment/list/" in r.url and r.ok and r.request.method == "GET":
                            try:
                                data = await r.json()
                                if data.get("comments"):
                                    comments.extend(data["comments"])
                            except:
                                pass

                    new_page.on("response", handle_resp)
                    await new_page.goto(vurl, timeout=20000)

                    # Wait for video page to load
                    await new_page.wait_for_timeout(3000)

                    # Try to extract caption and username
                    try:
                        caption = await new_page.evaluate("""() => {
                            const el = document.querySelector('[data-e2e="video-desc"]');
                            return el ? el.textContent : "";
                        }""")
                        username_el = await new_page.evaluate("""() => {
                            const el = document.querySelector('[data-e2e="video-author-uniqueid"]');
                            return el ? el.textContent : "";
                        }""")
                        if username_el:
                            username = username_el
                        elif "/@" in vurl:
                            username = vurl.split("/@")[1].split("/")[0]
                    except:
                        if "/@" in vurl:
                            username = vurl.split("/@")[1].split("/")[0]

                    # Try to open comments section safely
                    comment_container_selector = (
                        ".css-7whb78-DivCommentListContainer, "
                        ".css-1qp5gj2-DivCommentListContainer, "
                        "div[class*=\"-DivCommentListContainer\"], "
                        "div[class*=\"comment-list\"]"
                    )
                    comment_container = new_page.locator(comment_container_selector).first

                    try:
                        # Wait a bit for page to stabilize
                        await new_page.wait_for_timeout(2000)

                        try:
                            # Check if container is already visible (common on standalone pages)
                            await comment_container.wait_for(state="visible", timeout=3000)
                        except:
                            # If not visible, look for the comment button to open it
                            comment_btn = new_page.locator('div[data-e2e="browse-comment-icon"], [data-e2e="comment-icon"]').first
                            try:
                                await comment_btn.wait_for(state="visible", timeout=5000)
                            except Exception as e:
                                logger.warning("🚨 TIMEOUT OR BLOCK DETECTED! 🚨 Please check the Edge browser window and resolve any CAPTCHA/login walls within 60 seconds!")

                                # DUMP HTML FOR DEBUGGING
                                try:
                                    html_content = await new_page.content()
                                    with open(f"tiktok_dom_dump.html", "w", encoding="utf-8") as f:
                                        f.write(html_content)
                                    logger.info("Saved TikTok DOM to tiktok_dom_dump.html for inspection.")
                                except Exception as dump_err:
                                    logger.error(f"Failed to dump HTML: {dump_err}")

                                try:
                                    # Wait up to 60 seconds for the user to solve it and the comment button to appear
                                    await comment_btn.wait_for(state="visible", timeout=60000)
                                    logger.info("✅ Block cleared! Continuing...")
                                except Exception:
                                    logger.error("Failed to clear block in time or comment button still not visible.")
                                    raise e

                            await comment_btn.click(timeout=3000)
                            await new_page.wait_for_timeout(2000)

                        # Once opened/visible, attempt scrolling to trigger API requests
                        max_scrolls = 50
                        no_new_comments_count = 0
                        last_comment_count = 0

                        logger.info(f"[API Tab] {vid}: Starting to scroll...")
                        for scroll_attempt in range(max_scrolls):
                            await new_page.evaluate("""() => {
                                let containers = document.querySelectorAll('div[class*="-DivCommentListContainer"]');
                                if (containers.length > 0) {
                                    containers[containers.length - 1].scrollTop = containers[containers.length - 1].scrollHeight;
                                } else {
                                    window.scrollBy(0, 1000);
                                }
                            }""")

                            # Wait for API requests to be intercepted and comments to populate
                            await new_page.wait_for_timeout(1500)

                            current_comment_count = len(comments)
                            if current_comment_count == last_comment_count:
                                no_new_comments_count += 1
                                logger.info(f"[API Tab] {vid}: Scroll {scroll_attempt+1}/{max_scrolls} - No new comments. ({no_new_comments_count}/3)")
                            else:
                                no_new_comments_count = 0
                                logger.info(f"[API Tab] {vid}: Scroll {scroll_attempt+1}/{max_scrolls} - Collected {current_comment_count} comments so far.")

                            last_comment_count = current_comment_count

                            if no_new_comments_count >= 3:
                                logger.info(f"[API Tab] {vid}: Stopping early after 3 consecutive scrolls without new comments.")
                                break

                    except Exception as err:
                        logger.warning(f"Failed to open or scroll comments for {vid}: {err}")
                        try:
                            # Take screenshot for debugging
                            import os
                            os.makedirs(os.path.join("comments_data", "logs", "screenshots"), exist_ok=True)
                            await new_page.screenshot(path=os.path.join("comments_data", "logs", "screenshots", f"failed_btn_{vid}.png"))
                        except Exception as ss_err:
                            logger.error(f"Failed to take debug screenshot: {ss_err}")

                    await new_page.close()

                    # Deduplicate comments by cid
                    unique_comments = {c.get("cid"): c for c in comments if isinstance(c, dict) and "cid" in c}
                    return vid, {
                        "comments": list(unique_comments.values()),
                        "caption": caption,
                        "username": username
                    }

                except Exception as e:
                    logger.error(f"Error concurrently fetching video {vid} via tab: {e}")
                    try:
                        await new_page.close()
                    except:
                        pass
                    return vid, {"comments": [], "caption": "", "username": ""}

        tasks = [fetch_video(v) for v in videos]
        completed = await asyncio.gather(*tasks)

        for vid, data in completed:
            if vid:
                results[vid] = data

        return results

    async def refresh_tokens(self, page) -> bool:
        """Refresh security tokens from the browser session.

        Args:
            page: Playwright page object

        Returns:
            bool: Whether tokens were successfully refreshed
        """
        try:
            # Extract fresh tokens and cookies - use await properly
            tokens = await self.extract_security_tokens_from_browser(page)
            cookies = await self.extract_cookies_from_browser(page)

            if not tokens or not cookies:
                logger.warning("Could not extract fresh tokens/cookies")
                return False

            # Re-initialize API with fresh tokens
            if self.api:
                # Update cookies
                for name, value in cookies.items():
                    self.api.session.cookies.set(name, value)

                # Update tokens
                if 'msToken' in tokens and tokens['msToken']:
                    self.api.ms_token = tokens['msToken']

                if 'verifyFp' in tokens and tokens['verifyFp']:
                    self.api.verify_fp = tokens['verifyFp']

                if 's_v_web_id' in tokens and tokens['s_v_web_id']:
                    self.api.s_v_web_id = tokens['s_v_web_id']

                if 'csrf_token' in tokens and tokens['csrf_token']:
                    self.api.csrf_token = tokens['csrf_token']

                # Update headers
                self.api.session.headers.update({
                    'User-Agent': tokens.get('user_agent', ''),
                    'Referer': 'https://www.tiktok.com/',
                    'Origin': 'https://www.tiktok.com'
                })

                logger.info("Tokens refreshed successfully")
                return True
            else:
                # If API not initialized, do a full init
                return await self.initialize_api(page)

        except Exception as e:
            logger.error(f"Failed to refresh tokens: {e}")
            return False

    def extract_video_id_from_url(self, url: str) -> Optional[str]:
        """Extract video ID or photo ID from TikTok URL.

        Args:
            url: TikTok video or photo URL

        Returns:
            Content ID if found, None otherwise
        """
        if not self.api:
            # Fallback extraction if API not initialized
            patterns = [
                r'video/(\d+)',
                r'/v/(\d+)',
                r'photo/(\d+)'  # Add pattern for photo URLs
            ]

            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    return match.group(1)

            return None

        # If API is available, try to use its method, but add our own photo extraction if needed
        video_id = self.api.extract_video_id(url)
        if not video_id:
            # API might not handle photo URLs, so we'll do it manually
            photo_match = re.search(r'photo/(\d+)', url)
            if photo_match:
                return photo_match.group(1)
        return video_id

    async def detect_video_id_from_page(self, page) -> Optional[str]:
        """Extract content ID (video or photo) from the current page URL.

        Args:
            page: Playwright page object

        Returns:
            Content ID if found, None otherwise
        """
        try:
            # Get the current URL - use await properly
            url = await page.evaluate("window.location.href")

            # Try to extract content ID using regex
            content_id = self.extract_video_id_from_url(url)

            # If that fails, try to extract it from the page content
            if not content_id:
                # Look for a meta tag or JSON-LD data containing the content ID - use await properly
                content_id = await page.evaluate("""() => {
                    // Try to find it in meta tags
                    const metaContent = document.querySelector('meta[property="og:url"]')?.content;
                    if (metaContent) {
                        // Check for video ID
                        let match = metaContent.match(/video\\/(\\\\d+)/);
                        if (match) return match[1];

                        // Check for photo ID
                        match = metaContent.match(/photo\\/(\\\\d+)/);
                        if (match) return match[1];
                    }

                    // Try to find it in JSON-LD data
                    const jsonLD = document.querySelector('script[type="application/ld+json"]')?.textContent;
                    if (jsonLD) {
                        try {
                            const data = JSON.parse(jsonLD);
                            // Look for ID in various properties
                            return data.videoId || data.photoId || data.video?.videoId || data.photo?.photoId || null;
                        } catch (e) {
                            return null;
                        }
                    }

                    return null;
                }""")

            if content_id:
                logger.info(f"Detected content ID: {content_id}")
            else:
                logger.warning("Could not detect content ID from page")

            return content_id

        except Exception as e:
            logger.error(f"Error detecting content ID: {e}")
            return None

# Helper function to process existing comments from API format to scraper format
def process_api_comments(api_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Process comments from API format to match the format expected by the scraper,
    including nested replies.

    Args:
        api_comments: List of comments (and potentially replies) from the API

    Returns:
        List of comments in the format expected by the scraper, with nested replies.
    """

    def process_single_comment(comment_data: Dict[str, Any]) -> Dict[str, Any]:
        """Processes a single comment or reply."""
        raw_user = comment_data.get("user") if isinstance(comment_data.get("user"), dict) else {}
        processed = {
            'id': comment_data.get('cid', ''),
            'text': comment_data.get('text', ''),
            'create_time': comment_data.get('create_time', 0),
            'digg_count': comment_data.get('digg_count', 0),
            'reply_count': comment_data.get('reply_comment_total', 0), # Top-level might have this
            'user': {
                'id': raw_user.get('uid') or raw_user.get('id') or '',
                'sec_uid': raw_user.get('sec_uid') or raw_user.get('secUid') or '',
                'unique_id': raw_user.get('unique_id') or raw_user.get('uniqueId') or '',
                'nickname': raw_user.get('nickname', ''),
                'signature': raw_user.get('signature', ''),
                'avatar_url': (
                    raw_user.get('avatar_larger')
                    or raw_user.get('avatarLarger')
                    or raw_user.get('avatar_thumb')
                    or raw_user.get('avatarThumb')
                    or ''
                ),
                'verified': raw_user.get('verified'),
                'follower_count': raw_user.get('follower_count'),
                'following_count': raw_user.get('following_count')
            },
            'replies': [] # Initialize replies list
        }

        # Check for nested replies (assuming key is 'reply_comment')
        # The actual key might differ based on the tiktok_api.py library
        nested_replies_data = comment_data.get('reply_comment', [])
        if nested_replies_data and isinstance(nested_replies_data, list):
            for reply_data in nested_replies_data:
                 # Recursively process each reply
                 # Note: Replies might not have 'reply_comment_total' themselves
                processed['replies'].append(process_single_comment(reply_data))

        return processed

    processed_comments = []
    for comment in api_comments:
        processed_comments.append(process_single_comment(comment))

    return processed_comments
