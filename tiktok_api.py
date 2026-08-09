import requests
import time
import json
import random
import asyncio
import uuid
import os
import re
import logging
from typing import Dict, List, Optional, Tuple, Any, Union, Set
from datetime import datetime
from requests.adapters import HTTPAdapter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('tiktok_api')

class TikTokAPI:
    """TikTok API client to directly access TikTok endpoints without browser automation."""

    COMMENT_PAGE_SIZE = 50
    COMMENT_PAGE_CONCURRENCY = 4
    REPLY_PAGE_CONCURRENCY = 6

    def __init__(self,
                 device_id: Optional[str] = None,
                 user_agent: Optional[str] = None,
                 cookies_file: Optional[str] = None):
        """Initialize the TikTok API client.

        Args:
            device_id: Custom device ID. Generated if not provided.
            user_agent: Browser user agent to use. Default is Edge on Windows.
            cookies_file: Path to JSON file containing cookies to use for authentication.
        """
        # Generate or use provided device_id
        self.device_id = device_id or self._generate_device_id()

        # Default user agent (Edge on Windows)
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"

        # Load cookies if provided
        self.cookies = {}
        if cookies_file and os.path.exists(cookies_file):
            try:
                with open(cookies_file, 'r') as f:
                    self.cookies = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load cookies from {cookies_file}: {e}")

        # Session for making requests
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })

        # Apply cookies to session
        for key, value in self.cookies.items():
            self.session.cookies.set(key, value)

        # Store other params needed for requests
        self.region = "ID"  # Default region code
        self.app_language = "en"  # Default language
        self.browser_name = "Mozilla"
        self.browser_version = "5.0"
        self.browser_platform = "Win32"
        self.browser_language = "en-US"
        self.tz_name = "Asia/Jakarta"
        self.current_time = int(time.time())
        self.odin_id = self._generate_odin_id()

        # Initialize security tokens
        self.ms_token: Optional[str] = None
        self.update_tokens()

    def _generate_device_id(self) -> str:
        """Generate a random device ID."""
        return str(random.randint(1000000000000000000, 9999999999999999999))

    def _generate_odin_id(self) -> str:
        """Generate a random odin ID."""
        return str(random.randint(1000000000000000000, 9999999999999999999))

    def update_tokens(self) -> None:
        """Update security tokens required for API calls.

        Note: This implementation is a placeholder. In a real implementation,
        you would need to reverse-engineer how TikTok generates these tokens.
        """
        # WARNING: This is a placeholder! Real implementation requires reverse engineering
        # TikTok's JavaScript to generate valid tokens
        self.ms_token = f"{self._generate_random_hex(32)}"
        logger.info("Updated security tokens")

    def _generate_random_hex(self, length: int) -> str:
        """Generate a random hex string of specified length."""
        return ''.join(random.choice('0123456789abcdef') for _ in range(length))

    def _generate_x_bogus(self, url: str, payload: Optional[Dict[str, Any]] = None) -> str:
        """Generate X-Bogus header required for TikTok API calls.

        WARNING: This is a placeholder. Actual implementation requires reverse engineering
        TikTok's JavaScript to understand how X-Bogus is calculated.

        Args:
            url: The URL being requested
            payload: Optional payload for POST requests

        Returns:
            A fake X-Bogus signature (will not work in production)
        """
        # WARNING: This is a placeholder! Real implementation would reverse engineer
        # TikTok's signature algorithm
        return f"DFSz{self._generate_random_hex(16)}"

    def _generate_x_gnarly(self, url: str, payload: Optional[Dict[str, Any]] = None) -> str:
        """Generate X-Gnarly header required for some TikTok API calls.

        WARNING: This is a placeholder. Actual implementation requires reverse engineering
        TikTok's JavaScript.

        Args:
            url: The URL being requested
            payload: Optional payload for POST requests

        Returns:
            A fake X-Gnarly signature (will not work in production)
        """
        # WARNING: This is a placeholder! Real implementation would reverse engineer
        # TikTok's signature algorithm
        return f"M{self._generate_random_hex(128)}"

    def _get_base_params(self) -> Dict[str, str]:
        """Get common parameters used in TikTok API requests."""
        return {
            "aid": "1988",
            "app_language": self.app_language,
            "app_name": "tiktok_web",
            "browser_language": self.browser_language,
            "browser_name": self.browser_name,
            "browser_online": "true",
            "browser_platform": self.browser_platform,
            "browser_version": self.browser_version,
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "current_region": self.region,
            "data_collection_enabled": "true",
            "device_id": self.device_id,
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "video",
            "history_len": "2",
            "is_fullscreen": "false",
            "is_non_personalized": "false",
            "is_page_visible": "true",
            "odinId": self.odin_id,
            "os": "windows",
            "priority_region": "",
            "referer": "",
            "region": self.region,
            "screen_height": "720",
            "screen_width": "1280",
            "tz_name": self.tz_name,
            "user_is_login": "false" if not self.cookies else "true",
            "webcast_language": "en",
        }

    def _request_json(self, url: str, params: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
        """Run a blocking TikTok API request and parse the JSON response."""
        response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()

        if not response.text.strip():
            return {
                "comments": [],
                "has_more": False,
                "cursor": params.get("cursor", "0"),
                "total": 0,
                "status_code": -1,
                "status_msg": "empty response",
                "error": True,
            }

        return response.json()

    async def _get_json(self, url: str, params: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
        """Execute a requests call without blocking the asyncio event loop."""
        return await asyncio.to_thread(self._request_json, url, params, timeout)

    async def get_comments(self,
                           video_id: str,
                           count: int = COMMENT_PAGE_SIZE,
                           cursor: str = "0") -> Dict[str, Any]:
        """Get comments for a specific TikTok video.

        Args:
            video_id: The TikTok video ID (aweme_id)
            count: Number of comments to fetch per request
            cursor: Pagination cursor for fetching more comments

        Returns:
            Dictionary containing comment data
        """
        url = "https://www.tiktok.com/api/comment/list/"
        safe_count = max(1, min(int(count), self.COMMENT_PAGE_SIZE))
        params = {
            "aweme_id": video_id,
            "count": str(safe_count),
            "cursor": cursor,
            "aid": "1988",
        }

        logger.info(f"Fetching comments for video {video_id} with cursor {cursor}")
        try:
            data = await self._get_json(url, params)

            status_code = data.get("status_code", data.get("statusCode", 0))
            if status_code != 0:
                logger.warning(f"Error in API response: {data.get('status_msg', data.get('statusMsg', 'Unknown error'))}")
                return {"comments": [], "has_more": False, "cursor": cursor, "total": 0, "error": True}

            data["_requested_cursor"] = cursor
            return data
        except Exception as e:
            logger.error(f"Error fetching comments: {e}")
            return {"comments": [], "has_more": False, "cursor": cursor, "total": 0, "error": True}

    async def get_comment_replies(self,
                                  video_id: str,
                                  comment_id: str,
                                  count: int = COMMENT_PAGE_SIZE,
                                  cursor: str = "0") -> Dict[str, Any]:
        """Get replies for one top-level TikTok comment."""
        url = "https://www.tiktok.com/api/comment/list/reply/"
        safe_count = max(1, min(int(count), self.COMMENT_PAGE_SIZE))
        params = {
            "item_id": video_id,
            "comment_id": comment_id,
            "count": str(safe_count),
            "cursor": cursor,
            "aid": "1988",
        }

        try:
            data = await self._get_json(url, params)
            status_code = data.get("status_code", data.get("statusCode", 0))
            if status_code != 0:
                logger.warning(f"Error in reply API response: {data.get('status_msg', data.get('statusMsg', 'Unknown error'))}")
                return {"comments": [], "has_more": False, "cursor": cursor, "total": 0, "error": True}

            data["_requested_cursor"] = cursor
            return data
        except Exception as e:
            logger.error(f"Error fetching replies for comment {comment_id}: {e}")
            return {"comments": [], "has_more": False, "cursor": cursor, "total": 0, "error": True}

    async def _get_comments_with_retry(self, video_id: str, cursor: int, count: int) -> Dict[str, Any]:
        """Fetch one comment page with short retry/backoff."""
        response: Dict[str, Any] = {}
        for attempt in range(4):
            response = await self.get_comments(video_id, count=count, cursor=str(cursor))
            if not response.get("error"):
                return response

            if attempt < 3:
                await asyncio.sleep((0.4 * (attempt + 1)) + random.uniform(0.05, 0.35))

        return response

    async def get_all_replies(self,
                              video_id: str,
                              comment_id: str,
                              max_replies: int = 500) -> List[Dict[str, Any]]:
        """Fetch all accessible replies for a top-level comment."""
        replies: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        cursor = "0"
        has_more = True

        while has_more and len(replies) < max_replies:
            response = await self.get_comment_replies(
                video_id,
                comment_id,
                count=self.COMMENT_PAGE_SIZE,
                cursor=cursor,
            )
            if response.get("error"):
                break

            page_replies = response.get("comments", [])
            if not page_replies:
                break

            for reply in page_replies:
                if not isinstance(reply, dict):
                    continue
                reply_id = str(reply.get("cid") or reply.get("id") or "")
                if reply_id and reply_id in seen:
                    continue
                if reply_id:
                    seen.add(reply_id)
                replies.append(reply)
                if len(replies) >= max_replies:
                    break

            cursor = str(response.get("cursor", "0"))
            has_more = bool(response.get("has_more", response.get("hasMore", False)))
            if len(page_replies) < self.COMMENT_PAGE_SIZE:
                break

        return replies

    async def _attach_replies(self,
                              video_id: str,
                              comments: List[Dict[str, Any]],
                              max_replies_per_comment: int = 500) -> None:
        """Attach nested replies to comments that advertise reply counts."""
        semaphore = asyncio.Semaphore(self.REPLY_PAGE_CONCURRENCY)

        async def attach(comment: Dict[str, Any]) -> None:
            try:
                reply_total = int(comment.get("reply_comment_total") or 0)
            except (TypeError, ValueError):
                reply_total = 0

            comment_id = str(comment.get("cid") or comment.get("id") or "")
            if reply_total <= 0 or not comment_id:
                return

            async with semaphore:
                max_replies = min(reply_total, max_replies_per_comment)
                replies = await self.get_all_replies(video_id, comment_id, max_replies=max_replies)
                if replies:
                    comment["reply_comment"] = replies

        await asyncio.gather(*(attach(comment) for comment in comments))

    async def get_all_comments(self,
                               video_id: str,
                               max_comments: int = 0,
                               include_replies: bool = True,
                               return_status: bool = False,
                               use_checkpoint: bool = True) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """Get all comments for a video through pagination.

        Args:
            video_id: The TikTok video ID
            max_comments: Maximum number of comments to fetch. Zero means unlimited.
            use_checkpoint: Resume/write legacy local pagination checkpoints.
                ENGAGE must pass False so evidence is collected live for the
                current run.

        Returns:
            List of comment objects
        """
        all_comments: List[Dict[str, Any]] = []
        seen_comment_ids: Set[str] = set()
        next_cursor = 0
        has_more = True
        batch_size = self.COMMENT_PAGE_SIZE
        comment_limit = max_comments if max_comments and max_comments > 0 else None
        fetch_error = ""
        exhausted = False
        limit_reached = False
        resume_cursor = next_cursor

        # State Management: Load Checkpoint
        checkpoint_file = ""
        if use_checkpoint:
            checkpoint_dir = os.path.join(os.getcwd(), "comments_data")
            os.makedirs(checkpoint_dir, exist_ok=True)
            checkpoint_file = os.path.join(
                checkpoint_dir,
                f"checkpoint_{video_id}.json",
            )

        if checkpoint_file and os.path.exists(checkpoint_file):
            try:
                with open(checkpoint_file, 'r', encoding='utf-8') as f:
                    checkpoint_data = json.load(f)
                    if (
                        checkpoint_data.get('method') != 'minimal_direct_api'
                        or checkpoint_data.get('resume_safe') is not True
                    ):
                        raise ValueError("checkpoint was created by an older API method")
                    all_comments = checkpoint_data.get('comments', [])
                    next_cursor = int(checkpoint_data.get('cursor', "0"))
                    resume_cursor = next_cursor
                    seen_comment_ids = {
                        str(comment.get("cid") or comment.get("id"))
                        for comment in all_comments
                        if isinstance(comment, dict) and (comment.get("cid") or comment.get("id"))
                    }
                    total_fetched = len(all_comments)
                    has_more = checkpoint_data.get('has_more', True)
                logger.info(f"Resumed from checkpoint for video {video_id}. Fetched {total_fetched} comments already. Cursor: {next_cursor}")
            except Exception as e:
                logger.warning(f"Failed to load checkpoint file {checkpoint_file}: {e}")

        if not has_more:
            exhausted = True
        if comment_limit is not None and len(all_comments) >= comment_limit:
            limit_reached = True

        while has_more and (comment_limit is None or len(all_comments) < comment_limit):
            hit_comment_limit = False
            cursors = [
                next_cursor + (batch_size * offset)
                for offset in range(self.COMMENT_PAGE_CONCURRENCY)
            ]
            pages = await asyncio.gather(*(
                self._get_comments_with_retry(video_id, cursor, batch_size)
                for cursor in cursors
            ))

            stop_after_wave = False
            last_processed_cursor = next_cursor

            for requested_cursor, response in zip(cursors, pages):
                if response.get("error"):
                    logger.error(f"Stopping API pagination for video {video_id} after failed page cursor {requested_cursor}")
                    fetch_error = str(
                        response.get("status_msg")
                        or response.get("statusMsg")
                        or f"comment_page_failed_at_cursor_{requested_cursor}"
                    )
                    stop_after_wave = True
                    break

                comments = response.get("comments", [])
                response_has_more = bool(response.get("has_more", response.get("hasMore", False)))
                if not comments:
                    has_more = response_has_more
                    if has_more:
                        fetch_error = f"empty_comment_page_with_more_at_cursor_{requested_cursor}"
                    else:
                        exhausted = True
                    stop_after_wave = True
                    break

                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    comment_id = str(comment.get("cid") or comment.get("id") or "")
                    if comment_id and comment_id in seen_comment_ids:
                        continue
                    if comment_id:
                        seen_comment_ids.add(comment_id)
                    all_comments.append(comment)
                    if comment_limit is not None and len(all_comments) >= comment_limit:
                        hit_comment_limit = True
                        limit_reached = True
                        break

                last_processed_cursor = int(response.get("cursor", requested_cursor + batch_size))
                resume_cursor = last_processed_cursor
                has_more = response_has_more
                if not has_more:
                    exhausted = True

                if not has_more or hit_comment_limit:
                    stop_after_wave = True
                    break

            # State Management: Save only page-boundary-safe checkpoints.
            if use_checkpoint and checkpoint_file and not hit_comment_limit:
                try:
                    with open(checkpoint_file, 'w', encoding='utf-8') as f:
                        json.dump({
                            'method': 'minimal_direct_api',
                            'resume_safe': True,
                            'video_id': video_id,
                            'cursor': last_processed_cursor,
                            'has_more': has_more,
                            'comments': all_comments
                        }, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.warning(f"Failed to save checkpoint for video {video_id}: {e}")

            if len(all_comments) % 100 == 0:
                logger.info(f"Fetched {len(all_comments)} comments for video {video_id}")

            if stop_after_wave:
                break

            next_cursor = max(last_processed_cursor, cursors[-1] + batch_size)
            resume_cursor = next_cursor
            await asyncio.sleep(random.uniform(0.15, 0.45))

        if include_replies and all_comments:
            await self._attach_replies(video_id, all_comments)

        logger.info(f"Completed fetching {len(all_comments)} comments for video {video_id}")

        # Cleanup checkpoint on successful completion
        if (
            checkpoint_file
            and os.path.exists(checkpoint_file)
            and (exhausted or limit_reached)
        ):
            try:
                os.remove(checkpoint_file)
            except Exception:
                pass

        result = {
            "comments": all_comments,
            "ok": not fetch_error,
            "complete": exhausted and not fetch_error,
            "exhausted": exhausted,
            "limit_reached": limit_reached,
            "has_more": has_more,
            "error": fetch_error,
            "next_cursor": resume_cursor,
        }
        return result if return_status else all_comments

    async def get_explore_items(self, count: int = 20, category_type: str = "120") -> Dict[str, Any]:
        """Fetch TikTok Explore items using the minimal discovered endpoint."""
        url = "https://www.tiktok.com/api/explore/item_list/"
        params = {
            "aid": "1988",
            "count": str(max(1, min(int(count), 50))),
            "categoryType": str(category_type),
        }

        try:
            data = await self._get_json(url, params)
            status_code = data.get("status_code", data.get("statusCode", 0))
            if status_code != 0:
                logger.warning(f"Explore API returned error: {data.get('status_msg', data.get('statusMsg', 'Unknown error'))}")
                return {"itemList": [], "hasMore": False, "cursor": "0", "error": True}
            return data
        except Exception as e:
            logger.error(f"Error fetching Explore items: {e}")
            return {"itemList": [], "hasMore": False, "cursor": "0", "error": True}

    async def get_profile_videos(self, username: str, max_videos: int = 100) -> List[Dict[str, Any]]:
        """Get videos from a user's profile.

        WARNING: This implementation is a placeholder. Real implementation requires
        reverse engineering TikTok's secuser API.

        Args:
            username: TikTok username without '@'
            max_videos: Maximum number of videos to fetch

        Returns:
            List of video objects
        """
        # This function would need to use TikTok's secuser API endpoints
        # WARNING: Placeholder - requires reverse engineering
        logger.warning("get_profile_videos is a placeholder and requires reverse engineering TikTok's secuser API")
        return []

    async def get_video_count(self, username: str) -> int:
        """Get the total number of videos for a user.

        WARNING: This implementation is a placeholder. Real implementation requires
        reverse engineering TikTok's user info API.

        Args:
            username: TikTok username without '@'

        Returns:
            Total video count or 0 if unable to retrieve
        """
        # This function would need to use TikTok's user info API endpoints
        # WARNING: Placeholder - requires reverse engineering
        logger.warning("get_video_count is a placeholder and requires reverse engineering TikTok's user info API")
        return 0

    def extract_video_id(self, url: str) -> Optional[str]:
        """Extract video ID from a TikTok URL.

        Args:
            url: TikTok video URL

        Returns:
            Video ID or None if not found
        """
        patterns = [
            r'video/(\d+)',
            r'/v/(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        return None

    def _dict_to_query_params(self, params: Dict[str, str]) -> str:
        """Convert a dictionary to URL query parameters.

        Args:
            params: Dictionary of parameters

        Returns:
            URL-encoded query string
        """
        return "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])


# Example cookie extractor (to get cookies from an existing Edge profile)
def extract_edge_cookies(profile_path: str) -> Dict[str, str]:
    """Extract TikTok cookies from Edge browser profile.

    WARNING: This is a placeholder. Real implementation would need to use
    a library that can read browser cookies or extract them from the Chrome/Edge
    SQLite database.

    Args:
        profile_path: Path to Edge profile

    Returns:
        Dictionary of cookies
    """
    logger.warning("Cookie extraction requires additional libraries to read browser cookies.")
    logger.warning("This is a placeholder that returns empty cookies.")
    return {}


# Example usage function
async def example_get_comments(video_url: str, cookies_file: Optional[str] = None):
    """Example function to demonstrate getting comments for a video."""
    api = TikTokAPI(cookies_file=cookies_file)
    video_id = api.extract_video_id(video_url)

    if not video_id:
        logger.error(f"Could not extract video ID from URL: {video_url}")
        return []

    comments = await api.get_all_comments(video_id)
    return comments


# Advanced usage example
async def get_all_profile_comments(username: str, max_videos: int = 100, cookies_file: Optional[str] = None):
    """Get comments from all videos of a user.

    WARNING: This is currently a placeholder as get_profile_videos is not implemented.

    Args:
        username: TikTok username without '@'
        max_videos: Maximum number of videos to process
        cookies_file: Optional path to cookies file

    Returns:
        Dictionary mapping video IDs to their comments
    """
    api = TikTokAPI(cookies_file=cookies_file)

    # Get total video count (placeholder)
    total_videos = await api.get_video_count(username)
    logger.info(f"User {username} has {total_videos} videos (estimated)")

    # Get videos (placeholder)
    videos = await api.get_profile_videos(username, max_videos)

    if not videos:
        logger.warning(f"No videos found for user {username}")
        return {}

    # Get comments for each video
    all_comments = {}
    for i, video in enumerate(videos):
        video_id = video.get("id")
        logger.info(f"Processing video {i+1}/{len(videos)}: {video_id}")

        if not video_id:
            continue

        comments = await api.get_all_comments(video_id)
        all_comments[video_id] = comments

        # Avoid rate limiting
        await asyncio.sleep(2)

    return all_comments


if __name__ == "__main__":
    # Example of how to use the API
    import sys

    if len(sys.argv) > 1:
        video_url = sys.argv[1]
    else:
        video_url = input("Enter TikTok video URL: ")

    comments = asyncio.run(example_get_comments(video_url))
    print(f"Found {len(comments)} comments")

    if comments:
        # Print first 5 comments as example
        for i, comment in enumerate(comments[:5]):
            print(f"{i+1}. {comment.get('user', {}).get('unique_id')}: {comment.get('text')}")
