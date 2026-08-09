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
import itertools
import logging
import time
import urllib.parse
from typing import Dict, List, Optional, Any, Tuple, Set

from .tiktok_subtitles import (
    collect_tiktok_transcript,
    empty_tiktok_transcript_result,
    extract_tiktok_item_from_html,
    extract_tiktok_subtitle_manifest,
)

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


class TikTokCreatorProfileCollectionError(RuntimeError):
    """Raised when an exact TikTok creator inventory cannot be verified."""


TIKTOK_SEARCH_API_PATHS = (
    "/api/search/item/full/",
    "/api/search/general/full/",
)

TIKTOK_CREATOR_POST_API_PATHS = (
    "/api/post/item_list/",
)

TIKTOK_CREATOR_IDENTITY_API_PATHS = (
    "/api/user/detail/",
)


def sanitize_search_keyword(value: Any) -> str:
    """Remove quote operators because TikTok search treats them literally."""
    keyword = str(value or "").strip()
    keyword = keyword.replace('"', " ").replace("“", " ").replace("”", " ")
    keyword = keyword.replace("'", " ").replace("‘", " ").replace("’", " ")
    return re.sub(r"\s+", " ", keyword).strip()


def normalize_tiktok_creator_target(value: Any) -> Tuple[str, str]:
    """Return an exact handle and canonical profile URL.

    A post URL is deliberately not accepted as a profile target.  That keeps
    creator discovery explicit and prevents a caller from accidentally
    widening an ENGAGE run from one post URL through an inferred identity.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("TikTok creator handle or profile URL is required")

    handle = raw
    if re.match(r"^https?://", raw, flags=re.IGNORECASE):
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").casefold()
        if host not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            raise ValueError("TikTok creator profile URL must use tiktok.com")
        segments = [
            urllib.parse.unquote(segment)
            for segment in parsed.path.split("/")
            if segment
        ]
        if len(segments) != 1 or not segments[0].startswith("@"):
            raise ValueError("Expected a TikTok creator profile URL, not a post or tab URL")
        handle = segments[0]
    elif "/" in raw or "?" in raw or "#" in raw:
        raise ValueError("TikTok creator target must be an exact handle or profile URL")

    handle = handle.lstrip("@").strip()
    if (
        not handle
        or len(handle) > 64
        or not re.fullmatch(r"[A-Za-z0-9._]+", handle)
    ):
        raise ValueError("Invalid TikTok creator handle")
    return handle, f"https://www.tiktok.com/@{handle}"

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
            persist_session_secrets: Opt-in legacy behavior that writes
                browser-derived cookies/tokens to local files. Disabled by
                default and prohibited for ENGAGE.
        """
        self.enable_api = enable_api
        self.api = None
        self.last_search_diagnostics: Dict[str, Any] = {}
        self.last_creator_profile_diagnostics: Dict[str, Any] = {}
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

    def _build_search_variants(
        self,
        keyword: str,
        *,
        target_count: int = 0,
    ) -> List[str]:
        """Build enough tightly related searches for a target candidate count.

        TikTok web search commonly exposes only one page of roughly 12 posts
        for a query. Large exact-count LISTEN/ENGAGE requests therefore cannot
        rely on pagination alone. Every generic expansion below retains the
        complete normalized topic, which increases discovery breadth without
        silently changing the requested subject.
        """
        normalized = sanitize_search_keyword(keyword)
        if not normalized:
            return []

        variants = [normalized]
        current_year = time.localtime().tm_year
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

        # Broad ENGAGE topics need enough tightly related search surfaces to
        # satisfy larger exact-count requests. TikTok currently exposes only
        # one ranked result page for many individual web-search queries, so
        # these subtopics provide additional on-topic discovery without
        # treating unrelated "trending" content as valid evidence.
        if lower == "ai tools":
            variants.extend(
                [
                    f"AI tools {current_year}",
                    "free AI tools",
                    "AI productivity tools",
                    "AI video tools",
                    "AI editing tools",
                    "AI design tools",
                    "AI coding tools",
                    "AI automation tools",
                    "AI tools for business",
                    "AI study tools",
                ]
            )

        def unique_variants(values: List[str]) -> List[str]:
            unique: List[str] = []
            seen: Set[str] = set()
            for variant in values:
                cleaned = sanitize_search_keyword(variant)
                key = cleaned.casefold()
                if cleaned and key not in seen:
                    unique.append(cleaned)
                    seen.add(key)
            return unique

        seeded = unique_variants(variants)
        requested_candidates = max(0, int(target_count or 0))
        if not requested_candidates:
            return seeded
        if requested_candidates <= 12:
            return seeded[:1]

        # Use a conservative six new IDs per query when sizing the plan.
        # Actual result pages are usually larger, and discovery stops as soon
        # as target_count unique IDs have been observed.
        variant_goal = max(
            len(seeded),
            ((requested_candidates + 5) // 6) + 4,
        )
        suffixes = [
            "latest",
            "trending",
            "viral",
            "tutorial",
            "tips",
            "ideas",
            "explained",
            "review",
            "examples",
            "beginner",
            "advanced",
            "guide",
            "workflow",
            "setup",
            "mistakes",
            "comparison",
            "results",
            "case study",
            "news",
            "update",
            "challenge",
            "community",
            "step by step",
            "best practices",
            "techniques",
            "inspiration",
            "before and after",
            "recommendations",
            "cara",
            "terbaru",
            "ide",
            "contoh",
            "pemula",
            "panduan",
            "hasil",
            "tren",
        ]
        prefixes = [
            "how to",
            "best",
            "top",
            "learn",
            "trying",
            "testing",
            "reviewing",
            "why",
        ]
        expanded = list(seeded)
        seen_expanded = {variant.casefold() for variant in expanded}

        def extend_until_goal(values) -> bool:
            for value in values:
                cleaned = sanitize_search_keyword(value)
                key = cleaned.casefold()
                if cleaned and key not in seen_expanded:
                    expanded.append(cleaned)
                    seen_expanded.add(key)
                if len(expanded) >= variant_goal:
                    return True
            return len(expanded) >= variant_goal

        if extend_until_goal(
            f"{normalized} {suffix}" for suffix in suffixes
        ):
            return expanded
        if extend_until_goal(
            f"{prefix} {normalized}" for prefix in prefixes
        ):
            return expanded
        if extend_until_goal(
            f"{normalized} {suffix} {current_year}" for suffix in suffixes
        ):
            return expanded
        if extend_until_goal(
            f"{prefix} {normalized} {suffix}"
            for prefix in prefixes
            for suffix in suffixes
        ):
            return expanded

        # Keep expanding the same complete topic instead of imposing a fixed
        # query-count ceiling. Combinations are generated lazily and stop at
        # the request-derived goal, so a request above 50 (or above the former
        # 64-query frontier) is not silently truncated.
        for depth in range(2, len(suffixes) + 1):
            if extend_until_goal(
                f"{normalized} {' '.join(modifiers)}"
                for modifiers in itertools.combinations(suffixes, depth)
            ):
                break
        return expanded

    @staticmethod
    def _tiktok_item_visual_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
        """Return terminal, metadata-only photo evidence without inventing text."""
        photo_containers: List[Any] = [
            item.get("imagePost"),
            item.get("imagePostInfo"),
            item.get("image_post_info"),
            item.get("image_post"),
        ]
        slides: List[Any] = []
        for container in photo_containers:
            if isinstance(container, dict):
                for key in ("images", "imageList", "image_list"):
                    value = container.get(key)
                    if isinstance(value, list):
                        slides = value
                        break
            elif isinstance(container, list):
                slides = container
            if slides:
                break
        if not slides and isinstance(item.get("images"), list):
            slides = item["images"]

        type_hint = str(
            item.get("contentType")
            or item.get("content_type")
            or item.get("type")
            or ""
        ).casefold()
        photo_structure_observed = any(
            value not in (None, "", [], {}) for value in photo_containers
        ) or isinstance(item.get("images"), list)
        is_photo = (
            "photo" in type_hint
            or "image" in type_hint
            or photo_structure_observed
        )
        slide_count = len(slides) if is_photo else 0
        # Inventory/rehydration responses prove how many image slides TikTok
        # exposed, but they do not semantically describe those images.  Record
        # that explicit terminal limitation so captionless photos can be
        # counted without pretending that visual text was extracted.
        status = "unavailable" if is_photo else "not_provided"
        return {
            "content_type": "photo" if is_photo else "video",
            "visual_slide_count": slide_count,
            "visual_evidence_status": status,
            "visual_evidence_terminal": True,
            "photo_slide_count": slide_count,
            "photo_visual_evidence_status": status,
            "photo_visual_evidence_terminal": True,
        }

    def _extract_video_from_search_item(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a TikTok video/photo id, URL, and public metadata tuple."""
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
        subtitle_manifest = extract_tiktok_subtitle_manifest(item)
        visual_evidence = self._tiktok_item_visual_evidence(item)

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
            "url": (
                f"https://www.tiktok.com/@{username}/"
                f"{visual_evidence['content_type']}/{video_id}"
            ),
            "caption": caption,
            "username": username if username != "unknown" else "",
            "creator_id": author.get("id") or author.get("uid") or author.get("secUid") or author.get("sec_uid") or "",
            "creator_user_id": author.get("id") or author.get("uid") or "",
            "creator_sec_uid": author.get("secUid") or author.get("sec_uid") or "",
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
            **visual_evidence,
            "subtitle_track_count": len(subtitle_manifest.get("tracks") or []),
            "_tiktok_subtitle_manifest": subtitle_manifest,
        }

    @staticmethod
    def _creator_profile_item(row: Any) -> Dict[str, Any]:
        """Unwrap one supported TikTok profile item response shape."""
        if not isinstance(row, dict):
            return {}
        for key in ("item", "itemStruct", "aweme_info", "awemeInfo"):
            item = row.get(key)
            if isinstance(item, dict):
                return item
        return row

    @staticmethod
    def _creator_profile_rows(payload: Any) -> List[Dict[str, Any]]:
        """Read post rows without recursively treating unrelated objects as posts."""
        if not isinstance(payload, dict):
            return []
        containers = [payload]
        nested = payload.get("data")
        if isinstance(nested, dict):
            containers.append(nested)
        elif isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
        for container in containers:
            for key in ("itemList", "item_list", "items"):
                rows = container.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        return []

    @staticmethod
    def _creator_profile_has_more(payload: Any) -> Optional[bool]:
        """Return hasMore only when TikTok explicitly supplied the field."""
        if not isinstance(payload, dict):
            return None
        containers = [payload]
        if isinstance(payload.get("data"), dict):
            containers.append(payload["data"])
        for container in containers:
            for key in ("hasMore", "has_more"):
                if key not in container:
                    continue
                value = container.get(key)
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return bool(value)
                normalized = str(value or "").strip().casefold()
                if normalized in {"0", "false", "no", "off"}:
                    return False
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                return None
        return None

    @staticmethod
    def _creator_profile_payload_ok(payload: Any, http_status: Any = 200) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            if int(http_status) != 200:
                return False
        except (TypeError, ValueError):
            return False
        status = payload.get("statusCode", payload.get("status_code", 0))
        return status in (None, "", 0, "0")

    @staticmethod
    def _creator_profile_flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    @staticmethod
    def _creator_identity_fields(user: Any) -> Dict[str, str]:
        user = user if isinstance(user, dict) else {}
        return {
            "handle": str(
                user.get("uniqueId") or user.get("unique_id") or ""
            ).strip(),
            "id": str(user.get("id") or user.get("uid") or "").strip(),
            "sec_uid": str(
                user.get("secUid") or user.get("sec_uid") or ""
            ).strip(),
        }

    @classmethod
    def _creator_profile_identity_nodes(cls, payload: Any) -> List[Dict[str, str]]:
        """Extract public profile identities from a user-detail response."""
        identities: List[Dict[str, str]] = []
        seen: Set[Tuple[str, str, str]] = set()

        def walk(value: Any, depth: int = 0) -> None:
            if depth > 12:
                return
            if isinstance(value, dict):
                identity = cls._creator_identity_fields(value)
                marker = (
                    identity["handle"].casefold(),
                    identity["id"],
                    identity["sec_uid"],
                )
                if identity["handle"] and marker not in seen:
                    seen.add(marker)
                    identities.append(identity)
                for child in value.values():
                    walk(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    walk(child, depth + 1)

        walk(payload)
        return identities

    @staticmethod
    def _creator_identity_conflicts(
        bound: Dict[str, str],
        observed: Dict[str, str],
    ) -> bool:
        return any(
            bound.get(key)
            and observed.get(key)
            and bound[key] != observed[key]
            for key in ("id", "sec_uid")
        )

    @staticmethod
    def _bind_creator_identity(
        bound: Dict[str, str],
        observed: Dict[str, str],
    ) -> None:
        for key in ("id", "sec_uid"):
            if not bound.get(key) and observed.get(key):
                bound[key] = observed[key]

    @staticmethod
    def _normalize_creator_cursor(value: Any) -> str:
        normalized = str(value if value is not None else "").strip()
        if re.fullmatch(r"\d+", normalized):
            return str(int(normalized))
        return normalized

    @classmethod
    def _creator_profile_next_cursor(cls, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        containers = [payload]
        if isinstance(payload.get("data"), dict):
            containers.append(payload["data"])
        for container in containers:
            for key in ("cursor", "nextCursor", "next_cursor"):
                if key in container:
                    return cls._normalize_creator_cursor(container.get(key))
        return ""

    @classmethod
    def _creator_profile_request_metadata(cls, url: Any) -> Dict[str, Any]:
        """Keep cursor binding fields while dropping tokens/signatures."""
        raw_url = str(url or "")
        try:
            parsed = urllib.parse.urlsplit(raw_url)
            parsed_query = urllib.parse.parse_qs(
                parsed.query,
                keep_blank_values=True,
            )
        except Exception:
            return {"url": "", "query": {}}
        lower_query = {
            str(key).casefold(): values
            for key, values in parsed_query.items()
        }

        def first(*names: str) -> str:
            for name in names:
                values = lower_query.get(name.casefold()) or []
                if values:
                    return str(values[0] or "").strip()
            return ""

        query = {
            "sec_uid": first("secUid", "sec_uid"),
            "user_id": first("userId", "user_id", "uid"),
            "cursor": cls._normalize_creator_cursor(first("cursor")),
            "count": first("count"),
            "unique_id": first("uniqueId", "unique_id"),
        }
        query = {key: value for key, value in query.items() if value != ""}
        safe_pairs: List[Tuple[str, str]] = []
        for key, output_key in (
            ("sec_uid", "secUid"),
            ("user_id", "userId"),
            ("cursor", "cursor"),
            ("count", "count"),
            ("unique_id", "uniqueId"),
        ):
            if query.get(key):
                safe_pairs.append((output_key, query[key]))
        safe_url = urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(safe_pairs),
                "",
            )
        )
        return {"url": safe_url, "query": query}

    @classmethod
    def _captured_creator_identity_binding(
        cls,
        post_pages: List[Dict[str, Any]],
        identity_pages: List[Dict[str, Any]],
        expected_handle: str,
    ) -> Dict[str, Any]:
        expected_key = expected_handle.casefold()
        bound = {"id": "", "sec_uid": ""}
        handle_verified = False
        conflicts = 0
        mismatched_handles: Set[str] = set()

        def observe(identity: Dict[str, str]) -> None:
            nonlocal handle_verified, conflicts
            observed_handle = identity.get("handle", "")
            if observed_handle.casefold() != expected_key:
                if observed_handle:
                    mismatched_handles.add(observed_handle)
                return
            if cls._creator_identity_conflicts(bound, identity):
                conflicts += 1
                return
            handle_verified = True
            cls._bind_creator_identity(bound, identity)

        for packet in identity_pages:
            payload = packet.get("data") if isinstance(packet, dict) else None
            for identity in cls._creator_profile_identity_nodes(payload):
                observe(identity)
        for packet in post_pages:
            payload = packet.get("data") if isinstance(packet, dict) else None
            if not cls._creator_profile_payload_ok(
                payload,
                packet.get("http") if isinstance(packet, dict) else None,
            ):
                continue
            for row in cls._creator_profile_rows(payload):
                item = cls._creator_profile_item(row)
                identity = cls._creator_identity_fields(item.get("author"))
                if identity.get("handle"):
                    observe(identity)

        binding_status = "verified"
        if conflicts:
            binding_status = "identity_conflict"
        elif not handle_verified:
            binding_status = "handle_unverified"
        elif not bound.get("sec_uid") and not bound.get("id"):
            binding_status = "stable_identity_missing"
        return {
            "bound_identity": bound,
            "handle_verified": handle_verified,
            "identity_conflicts": conflicts,
            "mismatched_handles": sorted(mismatched_handles, key=str.casefold),
            "binding_status": binding_status,
            "binding_verified": binding_status == "verified",
        }

    @classmethod
    def _creator_profile_cursor_chain(
        cls,
        post_pages: List[Dict[str, Any]],
        identity_pages: List[Dict[str, Any]],
        expected_handle: str,
    ) -> Dict[str, Any]:
        """Select one target-bound 0→next-cursor chain and verify its terminal."""
        ordered = sorted(
            (packet for packet in post_pages if isinstance(packet, dict)),
            key=lambda row: row.get("_sequence", 0),
        )
        binding = cls._captured_creator_identity_binding(
            ordered,
            identity_pages,
            expected_handle,
        )
        bound = binding["bound_identity"]
        diagnostics: Dict[str, Any] = {
            **binding,
            "pages": [],
            "cursor_chain": [],
            "target_bound_pages": 0,
            "unrelated_identity_pages": 0,
            "missing_request_binding_pages": 0,
            "missing_request_cursor_pages": 0,
            "terminal_verified": False,
            "has_more": None,
            "stop_reason": "not_started",
        }
        if not binding["binding_verified"]:
            diagnostics["stop_reason"] = (
                "creator_identity_conflict"
                if binding["identity_conflicts"]
                else "missing_creator_stable_identity_binding"
            )
            return diagnostics

        expected_cursor = "0"
        visited: Set[str] = set()
        accepted_pages: List[Dict[str, Any]] = []
        chain_rows: List[Dict[str, Any]] = []
        broken_reason = ""

        for packet in ordered:
            payload = packet.get("data")
            if not cls._creator_profile_payload_ok(payload, packet.get("http")):
                continue
            query = packet.get("query") or packet.get("request_query") or {}
            if not isinstance(query, dict) or not query:
                metadata = cls._creator_profile_request_metadata(
                    packet.get("url") or packet.get("request_url")
                )
                query = metadata["query"]
            request_sec_uid = str(query.get("sec_uid") or "").strip()
            request_user_id = str(query.get("user_id") or "").strip()
            supplied_binding = bool(request_sec_uid or request_user_id)
            binding_comparisons: List[bool] = []
            if request_sec_uid and bound.get("sec_uid"):
                binding_comparisons.append(request_sec_uid == bound["sec_uid"])
            if request_user_id and bound.get("id"):
                binding_comparisons.append(request_user_id == bound["id"])
            if not supplied_binding or not binding_comparisons:
                diagnostics["missing_request_binding_pages"] += 1
                continue
            if not all(binding_comparisons):
                diagnostics["unrelated_identity_pages"] += 1
                continue
            diagnostics["target_bound_pages"] += 1

            request_cursor = cls._normalize_creator_cursor(query.get("cursor"))
            if not request_cursor:
                diagnostics["missing_request_cursor_pages"] += 1
                if not accepted_pages:
                    broken_reason = "missing_initial_request_cursor"
                continue
            if request_cursor in visited:
                broken_reason = "cursor_cycle"
                break
            if request_cursor != expected_cursor:
                broken_reason = (
                    "missing_initial_cursor_zero"
                    if not accepted_pages
                    else "cursor_gap"
                )
                break

            has_more = cls._creator_profile_has_more(payload)
            next_cursor = cls._creator_profile_next_cursor(payload)
            accepted_pages.append(packet)
            visited.add(request_cursor)
            chain_rows.append(
                {
                    "request_cursor": request_cursor,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "sequence": packet.get("_sequence", 0),
                }
            )
            diagnostics["has_more"] = has_more
            if has_more is None:
                broken_reason = "missing_has_more"
                break
            if has_more is False:
                diagnostics["terminal_verified"] = True
                diagnostics["stop_reason"] = "source_exhausted"
                break
            if not next_cursor:
                broken_reason = "missing_next_cursor"
                break
            if next_cursor == request_cursor or next_cursor in visited:
                broken_reason = "cursor_cycle"
                break
            expected_cursor = next_cursor

        diagnostics["pages"] = accepted_pages
        diagnostics["cursor_chain"] = chain_rows
        if not diagnostics["terminal_verified"]:
            diagnostics["stop_reason"] = broken_reason or (
                "no_target_bound_cursor_chain"
                if not accepted_pages
                else "frontier_not_terminal"
            )
        return diagnostics

    @classmethod
    def _captured_exact_owner_ids(
        cls,
        post_pages: List[Dict[str, Any]],
        identity_pages: List[Dict[str, Any]],
        expected_handle: str,
    ) -> Set[str]:
        """Conservatively count exact-owner IDs while deciding whether to scroll."""
        expected_key = expected_handle.casefold()
        bound = {"id": "", "sec_uid": ""}
        handle_verified = False
        for packet in identity_pages:
            payload = packet.get("data") if isinstance(packet, dict) else None
            for identity in cls._creator_profile_identity_nodes(payload):
                if identity["handle"].casefold() != expected_key:
                    continue
                if cls._creator_identity_conflicts(bound, identity):
                    continue
                handle_verified = True
                cls._bind_creator_identity(bound, identity)

        observed_ids: Set[str] = set()
        for packet in post_pages:
            payload = packet.get("data") if isinstance(packet, dict) else None
            if not cls._creator_profile_payload_ok(
                payload,
                packet.get("http") if isinstance(packet, dict) else None,
            ):
                continue
            for row in cls._creator_profile_rows(payload):
                item = cls._creator_profile_item(row)
                post_id = str(
                    item.get("id")
                    or item.get("aweme_id")
                    or item.get("item_id")
                    or ""
                ).strip()
                identity = cls._creator_identity_fields(item.get("author"))
                handle_key = identity["handle"].casefold()
                if not post_id or (handle_key and handle_key != expected_key):
                    continue
                if cls._creator_identity_conflicts(bound, identity):
                    continue
                if handle_key == expected_key:
                    handle_verified = True
                    cls._bind_creator_identity(bound, identity)
                elif not (
                    handle_verified
                    and any(
                        bound.get(key)
                        and identity.get(key) == bound.get(key)
                        for key in ("id", "sec_uid")
                    )
                ):
                    continue
                observed_ids.add(post_id)
        return observed_ids

    def _extract_creator_profile_candidate(
        self,
        item: Dict[str, Any],
        handle: str,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one already owner-verified profile post."""
        candidate = self._extract_video_from_search_item(item)
        if not candidate:
            return None
        post_id = str(candidate.get("id") or "").strip()
        if not post_id:
            return None

        photo_keys = (
            "imagePost",
            "imagePostInfo",
            "image_post_info",
            "image_post",
            "images",
        )
        type_hint = str(
            item.get("contentType")
            or item.get("content_type")
            or item.get("type")
            or ""
        ).casefold()
        is_photo = (
            "photo" in type_hint
            or "image" in type_hint
            or any(item.get(key) not in (None, "", [], {}) for key in photo_keys)
        )
        content_type = "photo" if is_photo else "video"
        identity = self._creator_identity_fields(item.get("author"))
        candidate.update(
            {
                "url": (
                    f"https://www.tiktok.com/@{handle}/{content_type}/{post_id}"
                ),
                "username": handle,
                "creator_id": identity["id"],
                "creator_user_id": identity["id"],
                "creator_sec_uid": identity["sec_uid"],
                "content_type": content_type,
                "is_pinned": any(
                    self._creator_profile_flag(item.get(key))
                    for key in (
                        "isPinnedItem",
                        "isPinned",
                        "is_pinned",
                        "pinned",
                    )
                ),
                "discovery_method": "tiktok_creator_profile_api_live_capture",
                "discovery_source": "tiktok_creator_profile",
                "metadata_method": "tiktok_creator_profile_api_response",
                "matched_queries": [f"creator:@{handle}"],
                "matched_keywords": [f"creator:@{handle}"],
            }
        )
        creator_profile = candidate.get("creator_profile")
        if not isinstance(creator_profile, dict):
            creator_profile = {}
            candidate["creator_profile"] = creator_profile
        creator_profile.update(
            {
                "user_id": identity["id"],
                "sec_uid": identity["sec_uid"],
                "username": handle,
            }
        )
        return candidate

    @staticmethod
    def _merge_creator_profile_candidate(
        existing: Dict[str, Any],
        incoming: Dict[str, Any],
    ) -> None:
        """Merge a pinned duplicate and retain the strongest media evidence."""
        existing["is_pinned"] = bool(
            existing.get("is_pinned") or incoming.get("is_pinned")
        )
        # Pinned cards can be abbreviated.  A later chronological row may be
        # the first one that includes imagePost and proves that the canonical
        # route is /photo/, so media identity is explicitly upgradeable.
        if (
            str(incoming.get("content_type") or "").casefold() == "photo"
            and str(existing.get("content_type") or "").casefold() != "photo"
        ):
            existing["content_type"] = "photo"
            existing["url"] = incoming.get("url")
        existing["visual_slide_count"] = max(
            int(existing.get("visual_slide_count") or 0),
            int(incoming.get("visual_slide_count") or 0),
        )
        existing["photo_slide_count"] = max(
            int(existing.get("photo_slide_count") or 0),
            int(incoming.get("photo_slide_count") or 0),
        )
        visual_rank = {"not_provided": 0, "unavailable": 1, "available": 2}
        for prefix in ("visual_evidence", "photo_visual_evidence"):
            status_key = f"{prefix}_status"
            terminal_key = f"{prefix}_terminal"
            current_status = str(existing.get(status_key) or "").casefold()
            incoming_status = str(incoming.get(status_key) or "").casefold()
            if visual_rank.get(incoming_status, -1) > visual_rank.get(
                current_status,
                -1,
            ):
                existing[status_key] = incoming.get(status_key)
            existing[terminal_key] = bool(
                existing.get(terminal_key) or incoming.get(terminal_key)
            )
        for key, value in incoming.items():
            current = existing.get(key)
            if current in (None, "", [], {}) and value not in (None, "", [], {}):
                existing[key] = value
            elif isinstance(current, dict) and isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if current.get(nested_key) in (None, "") and nested_value not in (
                        None,
                        "",
                    ):
                        current[nested_key] = nested_value

    async def _capture_creator_profile_api_pages(
        self,
        page,
        *,
        handle: str,
        profile_url: str,
        target_count: Optional[int],
        collect_all: bool,
        max_pages: Optional[int],
    ) -> Dict[str, Any]:
        """Capture TikTok-signed profile inventory responses from the open page.

        No API URL is replayed or manufactured here.  The authenticated TikTok
        page issues every request itself; scrolling only asks that page to load
        its next inventory cursor.
        """
        post_pages: List[Dict[str, Any]] = []
        identity_pages: List[Dict[str, Any]] = []
        seen_response_urls: Set[str] = set()
        tasks: Set[asyncio.Task] = set()
        post_event = asyncio.Event()
        navigation_error = ""
        response_sequence = 0

        async def capture(response, sequence: int) -> None:
            url = str(getattr(response, "url", "") or "")
            try:
                path = urllib.parse.urlsplit(url).path
            except Exception:
                path = ""
            is_post = any(marker in path for marker in TIKTOK_CREATOR_POST_API_PATHS)
            is_identity = any(
                marker in path for marker in TIKTOK_CREATOR_IDENTITY_API_PATHS
            )
            if not is_post and not is_identity:
                return
            if url in seen_response_urls:
                return
            seen_response_urls.add(url)
            try:
                payload = await response.json()
            except Exception:
                try:
                    body = await response.text()
                    payload = json.loads(body) if body else None
                except Exception:
                    payload = None
            packet = {
                "http": getattr(response, "status", None),
                "data": payload,
                "_sequence": sequence,
            }
            request_metadata = self._creator_profile_request_metadata(url)
            packet.update(request_metadata)
            packet["request_url"] = request_metadata["url"]
            packet["request_query"] = dict(request_metadata["query"])
            if is_post:
                post_pages.append(packet)
                post_event.set()
            else:
                identity_pages.append(packet)

        def schedule(response) -> None:
            nonlocal response_sequence
            url = str(getattr(response, "url", "") or "")
            if not any(
                marker in url
                for marker in (
                    *TIKTOK_CREATOR_POST_API_PATHS,
                    *TIKTOK_CREATOR_IDENTITY_API_PATHS,
                )
            ):
                return
            response_sequence += 1
            task = asyncio.create_task(capture(response, response_sequence))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        try:
            page.on("response", schedule)
        except Exception as exc:
            raise TikTokCreatorProfileCollectionError(
                f"Could not attach TikTok creator-profile response capture: {exc}"
            ) from exc

        stop_reason = "not_started"
        stall_rounds = 0
        try:
            try:
                await page.goto(
                    profile_url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
            except Exception as exc:
                navigation_error = f"{type(exc).__name__}: {exc}"

            try:
                await asyncio.wait_for(post_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

            while True:
                cursor_chain = self._creator_profile_cursor_chain(
                    post_pages,
                    identity_pages,
                    handle,
                )
                coherent_pages = cursor_chain["pages"]
                exact_ids = self._captured_exact_owner_ids(
                    coherent_pages,
                    identity_pages,
                    handle,
                )
                valid_pages = coherent_pages
                terminal = bool(cursor_chain["terminal_verified"])
                if target_count and len(exact_ids) >= target_count:
                    stop_reason = "requested_limit_observed"
                    break
                if terminal:
                    stop_reason = "source_exhausted"
                    break
                if collect_all and cursor_chain["stop_reason"] in {
                    "creator_identity_conflict",
                    "cursor_cycle",
                    "cursor_gap",
                    "missing_has_more",
                    "missing_initial_cursor_zero",
                    "missing_next_cursor",
                }:
                    stop_reason = cursor_chain["stop_reason"]
                    break
                if max_pages and len(valid_pages) >= max_pages:
                    stop_reason = "page_cap_reached"
                    break
                if stall_rounds >= 4:
                    stop_reason = "pagination_stalled"
                    break

                before = len(post_pages)
                post_event.clear()
                try:
                    await page.mouse.wheel(0, 2600)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(post_event.wait(), timeout=2.5)
                except asyncio.TimeoutError:
                    pass
                if len(post_pages) == before:
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

        cursor_chain = self._creator_profile_cursor_chain(
            post_pages,
            identity_pages,
            handle,
        )
        valid_pages = cursor_chain["pages"]
        return {
            "post_pages": sorted(
                post_pages,
                key=lambda row: row.get("_sequence", 0),
            ),
            "identity_pages": sorted(
                identity_pages,
                key=lambda row: row.get("_sequence", 0),
            ),
            "capture_diagnostics": {
                "post_responses": len(post_pages),
                "identity_responses": len(identity_pages),
                "valid_post_responses": len(valid_pages),
                "latest_has_more": cursor_chain["has_more"],
                "terminal_response_observed": cursor_chain[
                    "terminal_verified"
                ],
                "cursor_chain_stop_reason": cursor_chain["stop_reason"],
                "cursor_chain": list(cursor_chain["cursor_chain"]),
                "target_bound_pages": cursor_chain["target_bound_pages"],
                "unrelated_identity_pages": cursor_chain[
                    "unrelated_identity_pages"
                ],
                "missing_request_binding_pages": cursor_chain[
                    "missing_request_binding_pages"
                ],
                "stop_reason": stop_reason,
                "pagination_stall_rounds": stall_rounds,
                "navigation_error": navigation_error,
            },
        }

    async def discover_creator_profile_posts(
        self,
        page,
        creator: Any,
        *,
        limit: Any = None,
        collect_all: bool = False,
        max_pages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Discover an exact creator's video/photo inventory.

        ``limit="ALL"`` is accepted as a convenience alias for
        ``collect_all=True``.  An ALL result is complete only when diagnostics
        report ``terminal_verified=True`` and ``inventory_complete=True``.
        Finite discovery returns at most the requested number in profile order.
        """
        handle, profile_url = normalize_tiktok_creator_target(creator)
        if isinstance(limit, str) and limit.strip().casefold() == "all":
            if collect_all:
                limit = None
            else:
                collect_all = True
                limit = None
        if collect_all:
            if limit not in (None, ""):
                raise ValueError("ALL creator discovery cannot also use a numeric limit")
            requested_limit: Optional[int] = None
        else:
            if isinstance(limit, bool):
                raise ValueError("Creator post limit must be a positive integer")
            try:
                requested_limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Creator discovery requires a positive limit or ALL"
                ) from exc
            if requested_limit <= 0:
                raise ValueError("Creator post limit must be a positive integer")
        if max_pages is not None:
            try:
                max_pages = int(max_pages)
            except (TypeError, ValueError) as exc:
                raise ValueError("max_pages must be a positive integer") from exc
            if max_pages <= 0:
                raise ValueError("max_pages must be a positive integer")

        self.last_creator_profile_diagnostics = {
            "target_handle": handle,
            "profile_url": profile_url,
            "mode": "all" if collect_all else "limit",
            "requested_limit": requested_limit,
            "max_pages": max_pages,
            "candidate_count": 0,
            "terminal_verified": False,
            "source_exhausted": False,
            "inventory_complete": False,
            "limit_reached": False,
            "stop_reason": "not_started",
        }

        try:
            capture = await self._capture_creator_profile_api_pages(
                page,
                handle=handle,
                profile_url=profile_url,
                target_count=requested_limit,
                collect_all=collect_all,
                max_pages=max_pages,
            )
        except Exception as exc:
            self.last_creator_profile_diagnostics.update(
                {
                    "stop_reason": "capture_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if isinstance(exc, TikTokCreatorProfileCollectionError):
                raise
            raise TikTokCreatorProfileCollectionError(str(exc)) from exc

        post_pages = capture.get("post_pages") or []
        identity_pages = capture.get("identity_pages") or []
        capture_diagnostics = capture.get("capture_diagnostics") or {}
        expected_key = handle.casefold()
        cursor_chain = self._creator_profile_cursor_chain(
            post_pages,
            identity_pages,
            handle,
        )
        coherent_post_pages = cursor_chain["pages"]
        bound_identity = dict(cursor_chain["bound_identity"])
        handle_verified = bool(cursor_chain["handle_verified"])
        identity_conflicts = int(cursor_chain["identity_conflicts"])
        mismatched_profile_handles = set(cursor_chain["mismatched_handles"])

        candidates_by_id: Dict[str, Dict[str, Any]] = {}
        valid_pages = 0
        invalid_pages = 0
        observed_rows = 0
        duplicate_rows = 0
        rejected_handle_mismatch = 0
        rejected_identity_conflict = identity_conflicts
        rejected_owner_unverified = 0
        has_more_values: List[bool] = []

        raw_valid_pages = sum(
            1
            for packet in post_pages
            if isinstance(packet, dict)
            and self._creator_profile_payload_ok(
                packet.get("data"),
                packet.get("http"),
            )
        )
        invalid_pages = len(post_pages) - raw_valid_pages
        for packet in coherent_post_pages:
            payload = packet.get("data") if isinstance(packet, dict) else None
            if not self._creator_profile_payload_ok(
                payload,
                packet.get("http") if isinstance(packet, dict) else None,
            ):
                continue
            valid_pages += 1
            has_more = self._creator_profile_has_more(payload)
            if has_more is not None:
                has_more_values.append(has_more)
            for row in self._creator_profile_rows(payload):
                item = self._creator_profile_item(row)
                post_id = str(
                    item.get("id")
                    or item.get("aweme_id")
                    or item.get("item_id")
                    or ""
                ).strip()
                if not post_id:
                    continue
                observed_rows += 1
                identity = self._creator_identity_fields(item.get("author"))
                observed_handle = identity["handle"]
                observed_key = observed_handle.casefold()
                if observed_key and observed_key != expected_key:
                    rejected_handle_mismatch += 1
                    continue
                if self._creator_identity_conflicts(bound_identity, identity):
                    rejected_identity_conflict += 1
                    continue
                if observed_key == expected_key:
                    handle_verified = True
                    self._bind_creator_identity(bound_identity, identity)
                else:
                    stable_match = any(
                        bound_identity.get(key)
                        and identity.get(key) == bound_identity.get(key)
                        for key in ("id", "sec_uid")
                    )
                    if not handle_verified or not stable_match:
                        rejected_owner_unverified += 1
                        continue
                    self._bind_creator_identity(bound_identity, identity)

                candidate = self._extract_creator_profile_candidate(item, handle)
                if not candidate:
                    continue
                existing = candidates_by_id.get(post_id)
                if existing is None:
                    candidates_by_id[post_id] = candidate
                else:
                    duplicate_rows += 1
                    self._merge_creator_profile_candidate(existing, candidate)

        terminal_verified = bool(cursor_chain["terminal_verified"])
        source_exhausted = terminal_verified
        candidates = list(candidates_by_id.values())
        if requested_limit is not None:
            candidates = candidates[:requested_limit]
        for position, candidate in enumerate(candidates, start=1):
            candidate["profile_inventory_position"] = position

        limit_reached = bool(
            requested_limit is not None and len(candidates) >= requested_limit
        )
        if collect_all and terminal_verified:
            stop_reason = "source_exhausted"
        elif limit_reached:
            stop_reason = "requested_limit_reached"
        elif terminal_verified:
            stop_reason = "source_exhausted_before_limit"
        else:
            chain_stop_reason = str(cursor_chain.get("stop_reason") or "")
            stop_reason = (
                chain_stop_reason
                if chain_stop_reason not in {"", "frontier_not_terminal"}
                else str(
                    capture_diagnostics.get("stop_reason")
                    or chain_stop_reason
                    or "inventory_incomplete"
                )
            )
        inventory_complete = bool(
            collect_all
            and terminal_verified
            and handle_verified
            and valid_pages
            and cursor_chain["binding_verified"]
        )

        public_chain_diagnostics = {
            key: value
            for key, value in cursor_chain.items()
            if key != "pages"
        }

        self.last_creator_profile_diagnostics.update(
            {
                "pages_received": len(post_pages),
                "valid_pages": valid_pages,
                "raw_valid_pages": raw_valid_pages,
                "invalid_pages": invalid_pages,
                "identity_pages_received": len(identity_pages),
                "observed_rows": observed_rows,
                "unique_owner_posts_observed": len(candidates_by_id),
                "duplicate_rows_deduped": duplicate_rows,
                "rejected_handle_mismatch": rejected_handle_mismatch,
                "rejected_identity_conflict": rejected_identity_conflict,
                "rejected_owner_unverified": rejected_owner_unverified,
                "identity_conflicts": identity_conflicts,
                "mismatched_profile_handles_observed": sorted(
                    mismatched_profile_handles,
                    key=str.casefold,
                ),
                "owner_handle_verified": handle_verified,
                "bound_creator_identity": dict(bound_identity),
                "has_more": cursor_chain["has_more"],
                "terminal_verified": terminal_verified,
                "source_exhausted": source_exhausted,
                "inventory_complete": inventory_complete,
                "limit_reached": limit_reached,
                "candidate_count": len(candidates),
                "stop_reason": stop_reason,
                "cursor_chain": public_chain_diagnostics,
                "capture": dict(capture_diagnostics),
            }
        )

        if not valid_pages:
            if not raw_valid_pages:
                self.last_creator_profile_diagnostics["stop_reason"] = (
                    "no_valid_profile_post_response"
                )
                detail = "TikTok did not return a valid profile post response"
            else:
                if identity_conflicts:
                    self.last_creator_profile_diagnostics["stop_reason"] = (
                        "all_profile_rows_rejected_by_owner_fence"
                    )
                detail = (
                    "TikTok profile responses could not be verified as one "
                    f"target-bound cursor chain ({cursor_chain['stop_reason']})"
                )
            raise TikTokCreatorProfileCollectionError(
                f"{detail}; this is not a verified empty profile"
            )
        if not handle_verified:
            self.last_creator_profile_diagnostics["stop_reason"] = (
                "exact_creator_identity_unverified"
            )
            raise TikTokCreatorProfileCollectionError(
                f"Could not bind the profile inventory to exact creator @{handle}"
            )
        if observed_rows and not candidates_by_id:
            self.last_creator_profile_diagnostics["stop_reason"] = (
                "all_profile_rows_rejected_by_owner_fence"
            )
            raise TikTokCreatorProfileCollectionError(
                f"TikTok profile rows could not be verified as belonging to @{handle}"
            )
        return candidates

    async def get_transcript_for_video(self, page, video: Dict[str, Any]) -> Dict[str, Any]:
        """Collect one TikTok web-player subtitle track without blocking comments."""
        enabled = os.environ.get("TIKTOK_SCRAPE_TRANSCRIPTS", "1").strip().casefold()
        enabled = enabled not in {"0", "false", "no", "off"}
        preferred = os.environ.get("TIKTOK_TRANSCRIPT_LANGS", "id,en")
        try:
            timeout_ms = max(
                1000,
                int(float(os.environ.get("TIKTOK_TRANSCRIPT_TIMEOUT_SECONDS", "15")) * 1000),
            )
        except ValueError:
            timeout_ms = 15000
        try:
            retries = max(0, int(os.environ.get("TIKTOK_TRANSCRIPT_RETRIES", "1")))
        except ValueError:
            retries = 1
        try:
            max_bytes = max(1024, int(os.environ.get("TIKTOK_TRANSCRIPT_MAX_BYTES", "2000000")))
        except ValueError:
            max_bytes = 2000000

        try:
            return await collect_tiktok_transcript(
                page,
                video,
                preferred_languages=preferred,
                enabled=enabled,
                timeout_ms=timeout_ms,
                retries=retries,
                max_bytes=max_bytes,
            )
        except Exception as exc:
            logger.warning("TikTok subtitle collection failed for %s: %s", video.get("id"), exc)
            result = empty_tiktok_transcript_result("collector_error")
            result["transcript_error"] = str(exc) or type(exc).__name__
            return result

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

    async def refresh_video_candidates_from_html(
        self,
        page,
        videos: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Refresh known posts from their canonical TikTok HTML.

        Unlike search discovery, this method is bound to the supplied post ID
        and URL. The shared TikTok rehydration parser also extracts the
        platform subtitle manifest, so a targeted refresh retains the same
        transcript inputs as a newly discovered search result.
        """
        candidates = [
            video
            for video in videos
            if str(video.get("id") or "").strip()
            and str(video.get("url") or "").strip()
        ]
        for video in videos:
            video["metadata_refresh_ok"] = False
            for key in (
                "caption",
                "description",
                "title",
                "view_count",
                "play_count",
                "like_count",
                "digg_count",
                "comment_count",
                "share_count",
                "save_count",
                "collect_count",
                "follower_count",
                "metric_availability",
                "_tiktok_subtitle_manifest",
                "subtitle_track_count",
            ):
                video.pop(key, None)
        stats = {
            "eligible": len(videos),
            "attempted": 0,
            "hydrated": 0,
            "failed": 0,
        }
        if not candidates:
            return stats

        try:
            timeout_ms = max(
                1000,
                int(
                    float(
                        os.environ.get(
                            "TIKTOK_METADATA_TIMEOUT_SECONDS",
                            "8",
                        )
                    )
                    * 1000
                ),
            )
        except ValueError:
            timeout_ms = 8000
        try:
            concurrency = max(
                1,
                int(os.environ.get("TIKTOK_METADATA_CONCURRENCY", "4")),
            )
        except ValueError:
            concurrency = 4
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch(video: Dict[str, Any]) -> bool:
            expected_id = str(video.get("id") or "").strip()
            target_url = str(video.get("url") or "").strip()
            original_content_type = str(
                video.get("content_type") or ""
            ).casefold()
            original_slide_count = int(video.get("visual_slide_count") or 0)
            async with semaphore:
                stats["attempted"] += 1
                try:
                    response = await page.request.get(
                        target_url,
                        timeout=timeout_ms,
                    )
                    if not response.ok:
                        stats["failed"] += 1
                        return False
                    item = extract_tiktok_item_from_html(
                        await response.text(),
                        expected_id,
                    )
                    if not isinstance(item, dict):
                        stats["failed"] += 1
                        return False
                    refreshed = self._extract_video_from_search_item(item)
                    if (
                        not isinstance(refreshed, dict)
                        or str(refreshed.get("id") or "") != expected_id
                    ):
                        stats["failed"] += 1
                        return False

                    source_is_photo = (
                        original_content_type == "photo"
                        or "/photo/" in urllib.parse.urlsplit(target_url).path
                    )
                    if source_is_photo:
                        refreshed["content_type"] = "photo"
                        username = str(
                            refreshed.get("username")
                            or video.get("username")
                            or ""
                        ).lstrip("@").strip()
                        if not username:
                            handle_match = re.search(r"/@([^/?#]+)", target_url)
                            username = handle_match.group(1) if handle_match else ""
                        if username:
                            refreshed["url"] = (
                                f"https://www.tiktok.com/@{username}/photo/"
                                f"{expected_id}"
                            )
                        refreshed["visual_slide_count"] = max(
                            int(refreshed.get("visual_slide_count") or 0),
                            original_slide_count,
                        )
                        refreshed["photo_slide_count"] = max(
                            int(refreshed.get("photo_slide_count") or 0),
                            original_slide_count,
                        )
                        refreshed_visual_status = str(
                            refreshed.get("visual_evidence_status") or ""
                        ).casefold()
                        refreshed["visual_evidence_status"] = (
                            refreshed_visual_status
                            if refreshed_visual_status in {
                                "available",
                                "unavailable",
                            }
                            else "unavailable"
                        )
                        refreshed["visual_evidence_terminal"] = True
                        refreshed["photo_visual_evidence_status"] = (
                            refreshed["visual_evidence_status"]
                        )
                        refreshed["photo_visual_evidence_terminal"] = True

                    preserved = {
                        key: video.get(key)
                        for key in (
                            "discovery_method",
                            "discovery_source",
                            "matched_queries",
                            "matched_keywords",
                            "topic_relevance",
                            "topic_relevance_required",
                        )
                        if key in video
                    }
                    video.update(refreshed)
                    video.update(preserved)
                    video["metadata_method"] = "tiktok_web_rehydration"
                    video["metadata_hydration_method"] = (
                        "tiktok_web_rehydration"
                    )
                    video["metadata_refresh_ok"] = True
                    stats["hydrated"] += 1
                    return True
                except Exception as exc:
                    stats["failed"] += 1
                    logger.debug(
                        "TikTok direct HTML metadata refresh failed for %s: %s",
                        target_url,
                        exc,
                    )
                    return False

        await asyncio.gather(*(fetch(video) for video in candidates))
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
        target_count: int = 0,
    ) -> List[Dict[str, str]]:
        """Discover TikTok videos by calling the browser-backed search API."""
        normalized = sanitize_search_keyword(keyword)
        candidate_target = max(0, int(target_count or 0))
        self.last_search_diagnostics = {
            "keyword": normalized,
            "max_pages": int(max_offsets),
            "candidate_target": candidate_target,
            "queries_attempted": 0,
            "query_budget_limit": 0,
            "pages_received": 0,
            "has_more": None,
            "stop_reason": "not_started",
            "candidate_count": 0,
        }
        variants = (
            self._build_search_variants(
                keyword,
                target_count=candidate_target,
            )
            if include_related_queries
            else [normalized]
        )
        variants = [variant for variant in variants if variant]
        self.last_search_diagnostics["query_budget_limit"] = len(variants)
        self.last_search_diagnostics["query_variants_planned"] = len(variants)
        videos_by_id: Dict[str, Dict[str, str]] = {}

        def remember_video(
            video: Dict[str, Any],
            *,
            variant: str,
            discovery_method: str,
            metadata_method: str,
        ) -> None:
            video.setdefault("discovery_method", discovery_method)
            video.setdefault("discovery_source", "tiktok_search_api")
            video.setdefault("metadata_method", metadata_method)
            video["matched_queries"] = [variant]
            video["matched_keywords"] = [variant]
            video_id = str(video.get("id") or "")
            existing = videos_by_id.get(video_id)
            if existing is None:
                videos_by_id[video_id] = video
                return

            for key, value in video.items():
                if key in {"matched_queries", "matched_keywords"}:
                    continue
                current = existing.get(key)
                if current in (None, "", [], {}) and value not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    existing[key] = value
                elif isinstance(current, dict) and isinstance(value, dict):
                    for nested_key, nested_value in value.items():
                        if current.get(nested_key) in (None, "") and nested_value not in (
                            None,
                            "",
                        ):
                            current[nested_key] = nested_value

            matched_queries = [
                *(
                    existing.get("matched_queries")
                    if isinstance(existing.get("matched_queries"), list)
                    else []
                ),
                variant,
            ]
            existing["matched_queries"] = list(
                dict.fromkeys(
                    query
                    for query in matched_queries
                    if str(query or "").strip()
                )
            )
            existing["matched_keywords"] = list(
                existing["matched_queries"]
            )

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
            self.last_search_diagnostics["queries_attempted"] += 1
            captured_pages = await self._capture_search_api_pages(page, variant, max_offsets)
            if not captured_pages:
                continue
            capture_seen = True
            for page_number, result in enumerate(captured_pages, start=1):
                data = result.get("data") if isinstance(result, dict) else None
                status_code = data.get("status_code", data.get("statusCode", 0)) if isinstance(data, dict) else None
                rows = self._search_rows(data) if isinstance(data, dict) else []
                extracted_videos = [
                    video
                    for row in rows
                    if (video := self._extract_video_from_search_item(row))
                ]
                result_bearing_error = (
                    status_code != 0
                    and isinstance(result, dict)
                    and result.get("http") == 200
                    and bool(extracted_videos)
                )
                if not isinstance(data, dict) or (status_code != 0 and not result_bearing_error):
                    capture_errors.append(result)
                    continue
                valid_capture_seen = True
                self.last_search_diagnostics["pages_received"] += 1
                self.last_search_diagnostics["has_more"] = bool(
                    data.get("has_more", data.get("hasMore", False))
                )
                for video in extracted_videos:
                    remember_video(
                        video,
                        variant=variant,
                        discovery_method=(
                            "tiktok_search_api_live_capture"
                        ),
                        metadata_method="tiktok_search_api_response",
                    )
                logger.info(
                    "Captured Search API '%s' page %s with %s rows; total unique=%s",
                    variant,
                    page_number,
                    len(rows),
                    len(videos_by_id),
                )
                if candidate_target and len(videos_by_id) >= candidate_target:
                    break
            if candidate_target and len(videos_by_id) >= candidate_target:
                break

        if valid_capture_seen:
            videos = list(videos_by_id.values())
            self.last_search_diagnostics.update(
                {
                    "method": "live_capture",
                    "candidate_count": len(videos),
                    "stop_reason": (
                        "candidate_target_reached"
                        if candidate_target and len(videos) >= candidate_target
                        else "query_budget_exhausted"
                        if (
                            candidate_target
                            and len(videos) < candidate_target
                            and len(variants)
                            >= self.last_search_diagnostics[
                                "query_budget_limit"
                            ]
                        )
                        else "query_frontier_exhausted"
                        if candidate_target and len(videos) < candidate_target
                        else "page_cap_reached"
                        if self.last_search_diagnostics["has_more"]
                        and self.last_search_diagnostics["pages_received"]
                        >= int(max_offsets)
                        else "source_exhausted"
                        if self.last_search_diagnostics["has_more"] is False
                        else "capture_complete"
                    ),
                }
            )
            logger.info("Discovered %s unique TikTok videos from live Search API responses", len(videos))
            return videos

        if capture_seen:
            self.last_search_diagnostics["stop_reason"] = "invalid_search_session"
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
            self.last_search_diagnostics["stop_reason"] = "no_search_request_captured"
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
            self.last_search_diagnostics["queries_attempted"] += 1
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
                rows = self._search_rows(data) if isinstance(data, dict) else []
                extracted_videos = [
                    video
                    for row in rows
                    if (video := self._extract_video_from_search_item(row))
                ]
                result_bearing_error = (
                    status_code != 0
                    and isinstance(result, dict)
                    and result.get("http") == 200
                    and bool(extracted_videos)
                )
                if (
                    not isinstance(data, dict)
                    or (status_code != 0 and not result_bearing_error)
                ) and page_number == 1:
                    logger.info("Retrying the first TikTok search API response once")
                    await page.wait_for_timeout(1500)
                    try:
                        result = await page.evaluate(fetch_js, {"url": url})
                    except Exception as e:
                        logger.warning(f"Search API retry failed for '{variant}': {e}")
                    data = result.get("data") if isinstance(result, dict) else None
                    status_code = data.get("status_code", data.get("statusCode", 0)) if isinstance(data, dict) else None
                    rows = self._search_rows(data) if isinstance(data, dict) else []
                    extracted_videos = [
                        video
                        for row in rows
                        if (video := self._extract_video_from_search_item(row))
                    ]
                    result_bearing_error = (
                        status_code != 0
                        and isinstance(result, dict)
                        and result.get("http") == 200
                        and bool(extracted_videos)
                    )
                if not isinstance(data, dict) or (status_code != 0 and not result_bearing_error):
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

                self.last_search_diagnostics["pages_received"] += 1
                if not rows:
                    self.last_search_diagnostics["has_more"] = False
                    self.last_search_diagnostics["stop_reason"] = "source_exhausted"
                    logger.info(f"Search API returned no rows for '{variant}' page {page_number} cursor {cursor}")
                    break

                if not search_id:
                    search_id = self._extract_search_id(data)
                    if search_id:
                        logger.info(f"Captured TikTok search_id for '{variant}' pagination")

                for video in extracted_videos:
                    remember_video(
                        video,
                        variant=variant,
                        discovery_method="tiktok_search_api",
                        metadata_method="tiktok_search_api",
                    )

                if candidate_target and len(videos_by_id) >= candidate_target:
                    self.last_search_diagnostics["stop_reason"] = (
                        "candidate_target_reached"
                    )
                    break

                has_more = bool(data.get("has_more", data.get("hasMore", False)))
                self.last_search_diagnostics["has_more"] = has_more
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
                    self.last_search_diagnostics["stop_reason"] = "source_exhausted"
                    break

                next_cursor = data.get("cursor")
                if next_cursor is None:
                    next_cursor = cursor + len(rows)
                try:
                    next_cursor = int(next_cursor)
                except (TypeError, ValueError):
                    next_cursor = cursor + len(rows)

                if next_cursor <= cursor:
                    self.last_search_diagnostics["stop_reason"] = "cursor_did_not_advance"
                    logger.info(f"Search API cursor did not advance for '{variant}' page {page_number}; stopping")
                    break

                cursor = next_cursor

                await page.wait_for_timeout(200)
            if candidate_target and len(videos_by_id) >= candidate_target:
                break

        videos = list(videos_by_id.values())
        if candidate_target and len(videos) >= candidate_target:
            self.last_search_diagnostics["stop_reason"] = (
                "candidate_target_reached"
            )
        elif (
            candidate_target
            and len(videos) < candidate_target
            and len(variants)
            >= self.last_search_diagnostics["query_budget_limit"]
        ):
            self.last_search_diagnostics["stop_reason"] = (
                "query_budget_exhausted"
            )
        elif candidate_target and len(videos) < candidate_target:
            self.last_search_diagnostics["stop_reason"] = (
                "query_frontier_exhausted"
            )
        elif (
            self.last_search_diagnostics["has_more"]
            and self.last_search_diagnostics["pages_received"] >= int(max_offsets)
        ):
            self.last_search_diagnostics["stop_reason"] = "page_cap_reached"
        elif self.last_search_diagnostics["stop_reason"] == "not_started":
            self.last_search_diagnostics["stop_reason"] = "search_complete"
        self.last_search_diagnostics.update(
            {
                "method": "signed_replay",
                "candidate_count": len(videos),
            }
        )
        logger.info(f"Discovered {len(videos)} unique TikTok videos from search API")
        return videos

    async def get_comments_for_multiple_videos(
        self,
        page,
        videos: List[Dict[str, Any]],
        max_comments: int = 0,
        concurrency: int = 3,
        use_checkpoints: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
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
                transcript_result = await self.get_transcript_for_video(page, video)
                try:
                    logger.info(f"Direct API fetching video {vid}")
                    result = await self.api.get_all_comments(
                        vid,
                        max_comments=max_comments,
                        return_status=True,
                        use_checkpoint=use_checkpoints,
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
                        **transcript_result,
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
                        **transcript_result,
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
