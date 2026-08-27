"""Main entry point for the TikTok scraper."""
import asyncio
import os
import sys
import time
import json
import math
import re
from playwright.async_api import async_playwright
from pathlib import Path
from typing import Optional

# --- START: Add import for functools ---
from functools import partial
# --- END: Add import for functools ---

from .utils.file_utils import create_session_folders, load_element_info, save_element_info, load_recorded_elements
from .utils.element_recorder import record_button_click, record_elements
from .utils.navigation import click_next_video, navigate_to_url
from .utils.comment_count import extract_total_comment_count
from .utils.debug_utils import analyze_tiktok_page, save_page_structure
from .scrapers.comment_scraper import open_comment_section, scrape_comments, save_diagnostic_info, save_error_logs
# --- START: Add api_integration import ---
from .api_integration import TikTokAPIIntegration, TikTokSearchSessionError, process_api_comments
# --- END: Add api_integration import ---

# --- START: Add network response handler ---
# Define known TikTok API patterns to watch for
TIKTOK_API_PATTERNS = [
    "/api/comment/list/reply/", # Reply comments API
    "/api/comment/list/",  # Main comment list API
    "/api/comment/detail/", # Comment details API
    "/api/comment/reply/list/", # Reply comments API
    "/api/item/detail/",    # Video details API
    "/api/user/detail/",    # User details API
]


def _env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


TIKTOK_TRANSCRIPT_FIELDS = (
    "transcript",
    "transcript_available",
    "transcript_language",
    "transcript_language_name",
    "transcript_is_auto_generated",
    "transcript_segment_count",
    "transcript_segments",
    "transcript_error",
    "transcript_status",
    "transcript_source",
    "transcript_method",
    "transcript_attempt_count",
    "subtitle_tracks",
    "subtitle_selected_track",
    "subtitle_no_caption_reason",
    "subtitle_manifest_source",
)


def _tiktok_transcript_metadata(record):
    return {
        field: record.get(field)
        for field in TIKTOK_TRANSCRIPT_FIELDS
        if field in record
    }


# Global dict to store intercepted comments keyed by video ID (aweme_id)
global_intercepted_comments = {}

async def handle_comment_response(response, processed_urls):
    """Handles network responses, looking for comment API calls."""
    # Skip non-successful responses
    if not response.ok:
        return

    # Check if the URL matches any of our known API patterns
    matches_pattern = any(pattern in response.url for pattern in TIKTOK_API_PATTERNS)
    if not matches_pattern or response.request.method != "GET":
        return

    # Avoid processing the same response multiple times if listener triggers rapidly
    if response.url in processed_urls:
        return
    processed_urls.add(response.url)

    # Log which API endpoint we've captured
    api_type = "Unknown"
    for pattern in TIKTOK_API_PATTERNS:
        if pattern in response.url:
            api_type = pattern.strip("/").split("/")[-1]
            break

    print(f"[API] Intercepted TikTok {api_type} data from: {response.url[:100]}...")

    try:
        content_type = response.headers.get('content-type', '')
        if 'application/json' not in content_type and 'text/json' not in content_type:
            print(f"[API] Response is not JSON (type: {content_type}), skipping.")
            return

        # Parse the response data
        try:
            data = await response.json()
        except Exception as json_err:
            print(f"[API] Failed to parse JSON response: {json_err}")
            # Try to get text content for debugging
            try:
                text = await response.text()
                print(f"[API] Raw response (first 200 chars): {text[:200]}")
            except:
                pass
            return

        # Extract status code from TikTok's response
        status_code = data.get("status_code", 0)
        status_msg = data.get("status_msg", "")

        # Log status for all responses
        print(f"[API] TikTok API response status: {status_code} {status_msg}")

        if status_code != 0:
            print(f"[API] TikTok API returned error code {status_code}: {status_msg}")
            return

        # Process based on the API type
        if "/api/comment/list/reply/" in response.url or "/api/comment/reply/list/" in response.url:
            # Extract replies to comments
            reply_data = data.get("comments", [])
            if reply_data and isinstance(reply_data, list):
                print(f"[API] Found {len(reply_data)} reply comments in response")
                # Direct API scraping attaches these to the parent comment; this listener is diagnostic.

        elif "/api/comment/list/" in response.url:
            # Extract comments from the main comments list API
            comments_data = data.get("comments", [])
            if not comments_data:
                # Check alternative paths in the response structure
                if "data" in data and "comments" in data["data"]:
                    comments_data = data["data"]["comments"]
                elif "itemList" in data and isinstance(data["itemList"], list):
                    # Some API responses nest comments under items
                    for item in data["itemList"]:
                        if "comments" in item and isinstance(item["comments"], list):
                            comments_data.extend(item["comments"])

            cursor = data.get("cursor", data.get("data", {}).get("cursor", 0))
            has_more = data.get("has_more", data.get("data", {}).get("has_more", 0))

            # Print full data if debug needed and no comments found
            if not comments_data:
                print(f"[API DEBUG] Response structure: {str(data.keys())[:200]}")

            if comments_data and isinstance(comments_data, list):
                extracted_count = 0
                new_comments = []

                for comment in comments_data:
                    # Extract the comment info - adapt based on actual API structure
                    if not isinstance(comment, dict):
                        continue
                    raw_user = comment.get('user') if isinstance(comment.get('user'), dict) else {}
                    comment_obj = {
                        'cid': comment.get('cid', comment.get('id', 'N/A')),
                        'text': comment.get('text', ''),
                        'user': {
                            'id': raw_user.get('uid') or raw_user.get('id') or '',
                            'sec_uid': raw_user.get('sec_uid') or raw_user.get('secUid') or '',
                            'unique_id': raw_user.get('unique_id') or raw_user.get('uniqueId') or 'N/A',
                            'nickname': raw_user.get('nickname', 'N/A'),
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
                            'following_count': raw_user.get('following_count'),
                        },
                        'create_time': comment.get('create_time', 0),
                        'digg_count': comment.get('digg_count', 0),
                        'reply_comment_total': comment.get('reply_comment_total', 0),
                        'author_pin': comment.get('author_pin', False),
                        'from_api': True,  # Mark that this came from API for debugging
                    }

                    # Check if this comment is already in our list (avoid duplicates)
                    is_duplicate = False

                    # Extract aweme_id from the URL to store comments correctly
                    import urllib.parse
                    parsed_url = urllib.parse.urlparse(response.url)
                    query_params = urllib.parse.parse_qs(parsed_url.query)
                    aweme_id = query_params.get('aweme_id', [None])[0]

                    if aweme_id:
                        if aweme_id not in global_intercepted_comments:
                            global_intercepted_comments[aweme_id] = []

                        # Check duplicates within the global dict for this video
                        for existing in global_intercepted_comments[aweme_id]:
                            if existing.get('cid') == comment_obj.get('cid'):
                                is_duplicate = True
                                break

                        if not is_duplicate:
                            global_intercepted_comments[aweme_id].append(comment_obj)
                            new_comments.append(comment_obj)
                            extracted_count += 1

                if extracted_count > 0:
                    print(f"[API] Parsed {extracted_count} new comments from this response.")
                else:
                    print(f"[API] Found {len(comments_data)} comments in response, but all were duplicates.")

                if has_more:
                    print(f"[API] There are more comments available (cursor: {cursor})")
            else:
                print(f"[API] No comments found in response")

        elif "/api/item/detail/" in response.url:
            # Extract video details which might contain comment statistics
            item_info = data.get("itemInfo", {}).get("itemStruct", {})
            if item_info:
                stats = item_info.get("stats", {})
                comment_count = stats.get("commentCount", 0)
                if comment_count > 0:
                    print(f"[API] Video has {comment_count} comments according to item details API")

    except Exception as e:
        print(f"[API] Error processing response: {e}")
        # Try to get text content for debugging
        try:
            text = await response.text()
            print(f"[API] Raw response (first 200 chars): {text[:200]}")
        except:
            pass
# --- END: Add network response handler ---

def get_checkpoint_identifier(target_url):
    """Generate a valid filename identifier based on the target_url."""
    if not target_url:
        return "default"
    import urllib.parse
    import re
    if "search/video?q=" in target_url:
        query = urllib.parse.unquote(target_url.split("q=")[-1].split("&")[0])
        clean = re.sub(r'[^a-zA-Z0-9]', '_', query).strip('_')
        return clean if clean else "search"
    else:
        match = re.search(r'/@([^/?]+)', target_url)
        if match:
            return match.group(1)
        elif "explore" in target_url:
            return "explore"
    return "unknown"

def load_checkpoint(identifier):
    """Load processed_video_urls from checkpoint file."""
    import os, json
    checkpoint_dir = os.path.join("comments_data", "checkpoints")
    checkpoint_file = os.path.join(checkpoint_dir, f"{identifier}_checkpoint.json")
    if os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                urls = data.get("processed_video_urls", [])
                return set(urls)
        except Exception as e:
            print(f"[WARN] Failed to load checkpoint {checkpoint_file}: {e}")
    return set()

def save_checkpoint(identifier, processed_video_urls, target_url):
    """Save processed_video_urls to checkpoint file."""
    import os, json, datetime
    checkpoint_dir = os.path.join("comments_data", "checkpoints")
    os.makedirs(checkpoint_dir, exist_ok=True)
    checkpoint_file = os.path.join(checkpoint_dir, f"{identifier}_checkpoint.json")
    try:
        with open(checkpoint_file, 'w', encoding='utf-8') as f:
            json.dump({
                "target_url": target_url,
                "last_updated": datetime.datetime.now().isoformat(),
                "processed_video_urls": list(processed_video_urls)
            }, f, indent=2)
    except Exception as e:
        print(f"[WARN] Failed to save checkpoint {checkpoint_file}: {e}")

# Add this section after finding video elements and before processing them
async def wait_and_scroll_for_videos(page, max_attempts=5, target_count=0):
    """Waits and scrolls to try to load videos on a profile page.

    Args:
        page: The Playwright page object
        max_attempts: Maximum number of scroll attempts
        target_count: Target number of videos to find (0 for just finding any)

    Returns:
        bool: True if scrolling was performed
    """
    print(f"[INFO] Scrolling page to load videos (max {max_attempts} attempts)...")

    last_video_count = 0
    stagnant_attempts = 0

    for i in range(max_attempts):
        # Scroll down
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        print(f"[INFO] Scroll attempt {i+1}/{max_attempts}")

        # Wait for content to load
        await page.wait_for_timeout(3000)

        # Check if we've found any videos after scrolling
        num_videos = await page.evaluate("""() => {
            const videoLinks = document.querySelectorAll('a[href*="/video/"]');
            return videoLinks.length;
        }""")

        if num_videos > last_video_count:
            stagnant_attempts = 0
            last_video_count = num_videos
        else:
            stagnant_attempts += 1

        if target_count < 0:
            print(f"[INFO] Found {num_videos} video links after scroll {i+1}")
            if num_videos > 0 and stagnant_attempts >= 3:
                print("[INFO] Video count stopped growing after repeated scrolls")
                return True
            continue

        if num_videos > 0 and target_count == 0:
            print("[INFO] Videos found after scrolling")
            return True

        if num_videos >= target_count and target_count > 0:
            print(f"[INFO] Target {target_count} videos found after scrolling")
            return True

    # One final check with longer wait time
    await page.wait_for_timeout(5000)
    return True

async def find_profile_videos(page, logs_folder, max_videos=0):
    """Finds all video elements on a profile page.

    Args:
        page: The Playwright page object
        logs_folder: Folder to save debug information
        max_videos: Number of videos we want to find (0 for just one page)

    Returns:
        list: Locator objects of video elements found on the profile
    """
    print("[INFO] Scanning for videos on profile page...")

    try:
        # TikTok keeps background connections open, so networkidle commonly
        # times out even after search cards are ready.
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("[WARN] TikTok did not reach networkidle; continuing with rendered content.")

        # Take a screenshot and save page structure for analysis
        await save_page_structure(page, logs_folder)

        # Try scrolling to reveal videos
        if max_videos > 0 and math.isfinite(max_videos):
            await wait_and_scroll_for_videos(page, max_attempts=max(5, int(max_videos / 5)), target_count=max_videos)
        else:
            await wait_and_scroll_for_videos(page, max_attempts=20, target_count=-1)

        if "/search/" in page.url:
            try:
                body_text = " ".join((await page.locator("body").inner_text()).split()).casefold()
            except Exception:
                body_text = ""
            if "no results found for" in body_text:
                print("[WARN] TikTok rendered an explicit no-results state for this search.")
                return []


        # Identify video elements for direct clicking rather than URL navigation
        print("[INFO] Looking for clickable video elements...")

        # Try the exact selector from the example (including explore-item for trending page and search_video-item for search page)
        video_elements = await page.locator('div.css-1uqux2o-DivItemContainerV2, div.css-8dx572-DivContainer-StyledDivContainerV2, div[data-e2e="user-post-item"], div[data-e2e="explore-item"], div[data-e2e="search_video-item"]').all()

        if video_elements:
            print(f"[INFO] Found {len(video_elements)} video elements using primary selectors")
            return video_elements

        # If not found, try alternative selectors
        print("[INFO] Primary selectors didn't find videos. Trying alternatives...")
        alt_video_elements = await page.locator('div[class*="DivItemContainer"], div[class*="StyledDivContainer"]').all()

        if alt_video_elements:
            print(f"[INFO] Found {len(alt_video_elements)} video elements using alternative selectors")

            # Filter out elements that are likely not videos (too small, contain "playlist", etc.)
            filtered_elements = []
            for el in alt_video_elements:
                # Check element dimensions to avoid small elements
                box = await el.bounding_box()
                if not box or box["width"] < 100 or box["height"] < 100:
                    continue

                # Check content to filter out playlists and lives
                el_text = await el.text_content()
                if el_text and "playlist" in el_text.lower():
                    continue

                # Check if it contains a video link
                has_video_link = await el.locator('a[href*="/video/"]').count() > 0
                if has_video_link:
                    filtered_elements.append(el)

            if filtered_elements:
                print(f"[INFO] Filtered to {len(filtered_elements)} valid video elements")
                return filtered_elements

        # Final fallback
        print("[INFO] Trying fallback direct parent element selectors...")
        link_elements = await page.locator('a[href*="/video/"]:visible').all()

        if link_elements:
            print(f"[INFO] Found {len(link_elements)} direct video links")
            return link_elements

        print("[WARN] No video elements found. Check the page structure.")
        return []

    except Exception as e:
        print(f"[ERROR] Error finding profile videos: {e}")
        return []

async def process_videos_by_clicking(
    page,
    video_elements,
    comment_button_selector,
    folders,
    recorded_elements,
    max_videos,
    discovery_videos=0,
    use_api=True,
    target_url=None,
    max_comments=0,
    output_prefix="",
    candidate_videos=None,
):
    """Process videos by clicking on them directly instead of navigating to URLs.

    Args:
        page: The Playwright page object
        video_elements: List of video element locators
        comment_button_selector: CSS selector for the comment button
        folders: Dictionary containing folder paths
        recorded_elements: Recorded element information
        max_videos: Maximum number of videos to scrape, overrides the detected video count
            (set to infinity for unlimited scraping)
        discovery_videos: Candidate metadata limit before gates; max_videos remains
            the final comment-extraction cap
        use_api: Whether to attempt using the direct API for comment scraping first
        target_url: URL being scraped, to determine checkpoint identifier
        candidate_videos: Optional normalized candidate batch for direct API extraction

    Returns:
        None
    """
    from .raw_contract import transport_mode

    requested_transport = transport_mode()
    api_only = requested_transport == "api-only"
    # Initialize the API integration if enabled
    api_integration = None
    if use_api:
        api_integration = TikTokAPIIntegration(enable_api=True)
        api_initialized = await api_integration.initialize_api(page)
        if api_initialized:
            print("[INFO] TikTok API integration initialized successfully")
        else:
            print("[WARN] Could not initialize TikTok API. Will use browser-based scraping only.")
            api_integration = None
            if api_only:
                raise RuntimeError("TikTok API-only mode could not initialize the authenticated API session")

    video_count = 0
    identifier = get_checkpoint_identifier(target_url or page.url)
    processed_video_urls = load_checkpoint(identifier)

    if processed_video_urls:
        print(f"[INFO] Resuming session. Loaded {len(processed_video_urls)} processed URLs from checkpoint.")

    # Selector for the video navigation buttons
    button_selector = "button.TUXButton.TUXButton--capsule.TUXButton--medium.TUXButton--secondary.action-item.css-1egy55o:not([disabled])"

    # We'll only need to click the first video element, then use the next button navigation
    # This helps avoid problems with overlays blocking video element clicks
    initial_videos_to_click = min(1, len(video_elements))

    # For token refreshing when using API
    api_failures = 0
    api_consecutive_failures = 0  # Track consecutive API failures
    videos_since_refresh = 0
    max_api_failures = 3  # After this many consecutive failures, refresh tokens

    # For API recovery after fallback
    api_backoff_minutes = 5  # Time to wait before trying API again after fallbacks
    last_api_failure_time = 0  # Last time API failed
    using_fallback = False  # Track if we're currently using fallback

    # For rate limiting
    min_time_between_requests = 0.75  # Minimum seconds between API requests
    last_api_request_time = 0

    # For tracking progress during unlimited scraping
    total_comments_scraped = 0
    successful_videos = 0
    start_time = time.time()

    is_search_target = bool(target_url and "search/video?q=" in target_url)
    is_candidate_target = bool(candidate_videos)
    candidate_limit = discovery_videos if discovery_videos and discovery_videos > 0 else max_videos

    # Phase 0: Concurrent API fetching
    if api_integration and use_api and (video_elements or is_search_target or is_candidate_target):
        print(f"\n[INFO] Extracting URLs for concurrent API fetching...")
        videos_to_fetch = []

        for raw_candidate in candidate_videos or []:
            if not isinstance(raw_candidate, dict):
                continue
            candidate = dict(raw_candidate)
            candidate_id = candidate.get("id") or candidate.get("video_id")
            if candidate_id:
                candidate["id"] = str(candidate_id)
            if not candidate.get("caption"):
                candidate["caption"] = candidate.get("title") or ""
            if not candidate.get("username"):
                candidate["username"] = candidate.get("content_creator") or candidate.get("creator") or ""
            videos_to_fetch.append(candidate)

        if target_url and "search/video?q=" in target_url:
            try:
                import urllib.parse
                keyword = urllib.parse.unquote(target_url.split("q=", 1)[1].split("&", 1)[0])
                search_pages = 25
                if math.isfinite(candidate_limit):
                    search_pages = max(1, math.ceil(int(candidate_limit) / 12))
                discovered_videos = await api_integration.discover_search_videos(
                    page,
                    keyword,
                    max_offsets=search_pages,
                )
                if discovered_videos:
                    for candidate in discovered_videos:
                        matched = candidate.get("matched_keywords")
                        if not isinstance(matched, list):
                            matched = []
                        if keyword.casefold() not in {str(value).casefold() for value in matched}:
                            matched.append(keyword)
                        candidate["matched_keywords"] = matched
                    print(f"[INFO] Search API discovered {len(discovered_videos)} unique videos from exact-query pagination.")
                    videos_to_fetch.extend(discovered_videos)
            except TikTokSearchSessionError as exc:
                if api_only or not video_elements:
                    raise
                print(f"[WARN] Search API discovery unavailable; using rendered video links: {exc}")
            except Exception as e:
                print(f"[WARN] Search API discovery failed; using DOM links only: {e}")

        search_keyword = ""
        if target_url and "search/video?q=" in target_url:
            try:
                import urllib.parse
                search_keyword = urllib.parse.unquote(target_url.split("q=", 1)[1].split("&", 1)[0])
            except Exception:
                search_keyword = ""

        for el in ([] if api_only else video_elements):
            try:
                dom_candidate = await el.evaluate(
                    r"""
                    el => {
                      const anchor = el.tagName.toLowerCase() === 'a'
                        ? el
                        : el.querySelector('a[href*="/video/"]');
                      const href = anchor ? anchor.href : '';
                      const card = el.closest('div[id^="grid-item-container-"]')
                        || el.closest('div[class*="DivItemContainer"]')
                        || el;
                      const text = (card.innerText || '').trim();
                      const lines = text.split(/\n+/).map(value => value.trim()).filter(Boolean);
                      const published = lines.find(value =>
                        /^\d+\s*(?:s|m|h|d|w|y)\s*(?:ago)?$/i.test(value)
                        || /^\d+\s*(?:detik|menit|jam|hari|minggu|bulan|tahun)\s*(?:lalu)?$/i.test(value)
                        || /^\d{1,2}-\d{1,2}$/.test(value)
                      ) || '';
                      const metric = lines.find(value =>
                        /^(?:top liked\s*)?\d+(?:[.,]\d+)?[kmb]?$/i.test(value.replace(/\s+/g, ' '))
                      ) || '';
                      return { href, text, published, metric };
                    }
                    """
                )
                href = dom_candidate.get("href") if isinstance(dom_candidate, dict) else ""
                if href and "/video/" in href:
                    username_match = re.search(r"tiktok\.com/@([^/?#]+)/video/", href)
                    candidate = {
                        "url": href,
                        "caption": dom_candidate.get("text", ""),
                        "username": username_match.group(1) if username_match else "",
                        "published_at": dom_candidate.get("published", ""),
                        "content_type": "video",
                        "discovery_source": "browser_dom",
                        "discovery_method": "browser_dom",
                        "metadata_method": "browser_dom_card",
                    }
                    if search_keyword:
                        candidate["matched_keywords"] = [search_keyword]
                        candidate["like_count"] = dom_candidate.get("metric", "")
                    else:
                        candidate["view_count"] = dom_candidate.get("metric", "")
                    videos_to_fetch.append(candidate)
            except Exception as e:
                pass

        deduped_videos = []
        seen_video_ids = set()
        for video in videos_to_fetch:
            vid = video.get("id")
            if not vid and video.get("url"):
                vid = api_integration.extract_video_id_from_url(video["url"])
                if vid:
                    video["id"] = vid
            key = vid or video.get("url")
            if key and key not in seen_video_ids:
                deduped_videos.append(video)
                seen_video_ids.add(key)
        videos_to_fetch = deduped_videos
        if math.isfinite(candidate_limit) and len(videos_to_fetch) > int(candidate_limit):
            print(
                f"[INFO] Discovery cap tiktok: observed={len(videos_to_fetch)} "
                f"retained={int(candidate_limit)} limit={int(candidate_limit)}",
                flush=True,
            )
            videos_to_fetch = videos_to_fetch[:int(candidate_limit)]
        had_candidates_before_gates = bool(videos_to_fetch)

        try:
            from .date_filter import filter_candidates_by_date, windows_from_env, write_date_audit

            date_windows = windows_from_env()
            if date_windows:
                videos_to_fetch, date_audit = filter_candidates_by_date(videos_to_fetch, date_windows)
                audit_path = write_date_audit(folders["logs"], "tiktok", date_audit, date_windows)
                summary = {"within_date": 0, "outside_date": 0, "unknown_date": 0}
                for item in date_audit:
                    summary[item["decision"]] += 1
                print(
                    f"[INFO] Date-window tiktok: within={summary['within_date']} "
                    f"outside={summary['outside_date']} unknown={summary['unknown_date']}; "
                    f"scrape={len(videos_to_fetch)} audit={audit_path}",
                    flush=True,
                )
        except Exception as e:
            raise RuntimeError("TikTok date-window gate failed closed") from e

        try:
            from .relevance import filter_candidates, profile_from_env, score_candidate, write_relevance_audit

            relevance_profile, relevance_mode, review_action = profile_from_env()
            if relevance_profile and relevance_mode != "off":
                hydration_candidates = [
                    candidate
                    for candidate in videos_to_fetch
                    if score_candidate(candidate, relevance_profile, platform="tiktok").get(
                        "needs_metadata_hydration"
                    )
                ]
                if hydration_candidates and api_integration:
                    hydration = await api_integration.hydrate_video_candidates(page, hydration_candidates)
                    print(
                        f"[INFO] TikTok metadata preflight: eligible={hydration['eligible']} "
                        f"attempted={hydration['attempted']} hydrated={hydration['hydrated']} "
                        f"failed={hydration['failed']}",
                        flush=True,
                    )
                accepted, review, rejected, scored = filter_candidates(
                    videos_to_fetch,
                    relevance_profile,
                    platform="tiktok",
                    mode=relevance_mode,
                    review_action=review_action,
                )
                audit_path = write_relevance_audit(folders["logs"], "tiktok", scored, relevance_profile)
                print(
                    f"[INFO] Relevance tiktok: accepted={len(accepted)} "
                    f"review={len(review)} rejected={len(rejected)} mode={relevance_mode}; "
                    f"audit={audit_path}",
                    flush=True,
                )
                videos_to_fetch = accepted
        except Exception as e:
            raise RuntimeError("TikTok relevance gate failed closed") from e

        try:
            from .known_content import filter_new_candidates, known_content_from_env, write_known_content_audit

            known_keys, known_meta = known_content_from_env()
            if known_keys:
                fresh, skipped, audit = filter_new_candidates(videos_to_fetch, "tiktok", known_keys)
                audit_path = write_known_content_audit(folders["logs"], "tiktok", audit, known_meta)
                print(
                    f"[INFO] Known-content tiktok: scrape_new={len(fresh)} "
                    f"skip_known={len(skipped)} baseline={known_meta.get('last_successful_scrape_at', '')}; "
                    f"audit={audit_path}",
                    flush=True,
                )
                videos_to_fetch = fresh
        except Exception as e:
            raise RuntimeError("TikTok known-content gate failed closed") from e

        if (is_search_target or is_candidate_target) and had_candidates_before_gates and not videos_to_fetch:
            print("[INFO] Candidate discovery produced posts, but none remain after gates. Skipping browser fallback.")
            return

        # Limit to max_videos if specified
        if math.isfinite(max_videos):
            videos_to_fetch = videos_to_fetch[:int(max_videos)]

        if videos_to_fetch:
            print(f"[INFO] Found {len(videos_to_fetch)} URLs for concurrent fetching. Starting API tasks...")
            results = await api_integration.get_comments_for_multiple_videos(
                page,
                videos_to_fetch,
                max_comments=max_comments,
                concurrency=3,
            )
            attempted_videos = 0
            terminal_videos = 0
            failed_video_ids = []

            for i, (vid, data) in enumerate(results.items()):
                attempted_videos += 1
                api_comments = data.get("comments", [])
                comments = process_api_comments(api_comments) if api_comments else []
                caption = data.get("caption", "")
                username = data.get("username", "")
                candidate = next(
                    (
                        video
                        for video in videos_to_fetch
                        if str(video.get("id") or "") == str(vid)
                        or (vid and vid in str(video.get("url") or ""))
                    ),
                    {},
                )
                candidate_url = candidate.get("url", "")
                terminal = bool(data.get("ok")) and bool(
                    data.get("exhausted") or data.get("limit_reached")
                )
                extra_metadata = {
                    "url": candidate_url,
                    "platform": "tiktok",
                    "published_at": candidate.get("published_at") or candidate.get("create_time") or "",
                    "content_type": candidate.get("content_type") or "video",
                    "view_count": candidate.get("view_count"),
                    "like_count": candidate.get("like_count"),
                    "reported_comment_count": candidate.get("comment_count"),
                    "share_count": candidate.get("share_count"),
                    "save_count": candidate.get("save_count"),
                    "follower_count": candidate.get("follower_count"),
                    "metric_availability": dict(candidate.get("metric_availability") or {}),
                    "creator_id": candidate.get("creator_id") or "",
                    "creator_display_name": candidate.get("creator_display_name") or "",
                    "creator_verified": candidate.get("creator_verified"),
                    "creator_profile": dict(candidate.get("creator_profile") or {}),
                    "music_id": candidate.get("music_id") or "",
                    "music_title": candidate.get("music_title") or "",
                    "music_author": candidate.get("music_author") or "",
                    "duration_seconds": candidate.get("duration_seconds") or 0,
                    "thumbnail_url": candidate.get("thumbnail_url") or "",
                    "matched_keywords": candidate.get("matched_keywords") or [],
                    "complete": terminal,
                    "comments_exhausted": bool(data.get("exhausted")),
                    "comment_limit_reached": bool(data.get("limit_reached")),
                    "error": data.get("error", ""),
                    "source": data.get("source", "direct_api"),
                    "discovery_method": candidate.get("discovery_method") or candidate.get("discovery_source") or "tiktok_search_api",
                    "metadata_method": candidate.get("metadata_method") or "tiktok_candidate_metadata",
                    "comment_method": "tiktok_comment_api",
                    "comments_seen_in_response": len(api_comments),
                    **_tiktok_transcript_metadata(data),
                }
                output_folder = folders["comments"] if terminal else folders["logs"]
                record_prefix = output_prefix if terminal else f"incomplete_{output_prefix}"
                if comments:
                    level = "SUCCESS" if terminal else "WARN"
                    qualifier = "" if terminal else " partial"
                    print(f"[{level}] Concurrently fetched{qualifier} {len(comments)} comments for video ID {vid}")
                    await save_comments_to_file(
                        comments,
                        i + 1,
                        output_folder,
                        video_id=vid,
                        caption=caption,
                        username=username,
                        extra_metadata=extra_metadata,
                        filename_prefix=record_prefix,
                        platform="tiktok",
                    )
                    if terminal:
                        total_comments_scraped += len(comments)
                        successful_videos += 1
                else:
                    outcome = "complete zero-comment result" if terminal else "incomplete result"
                    print(f"[INFO] API returned {outcome} for video ID {vid}.")
                    await save_comments_to_file(
                        [],
                        i + 1,
                        output_folder,
                        video_id=vid,
                        caption=caption,
                        username=username,
                        extra_metadata=extra_metadata,
                        filename_prefix=record_prefix,
                        platform="tiktok",
                    )

                if terminal:
                    terminal_videos += 1
                    if candidate_url:
                        processed_video_urls.add(candidate_url)
                else:
                    failed_video_ids.append(str(vid))

            if terminal_videos:
                save_checkpoint(identifier, processed_video_urls, target_url or page.url)

            print(f"[INFO] Concurrent API phase finished. API attempted {attempted_videos} videos; saved comments for {successful_videos} videos.")

            if terminal_videos == len(videos_to_fetch):
                print("[INFO] All discovered videos completed via concurrent API. Skipping browser fallback.")
                return
            if api_only:
                raise RuntimeError(
                    "TikTok API-only mode left incomplete comment requests: "
                    + ", ".join(failed_video_ids[:10])
                )
            print(
                f"[WARN] Concurrent API left {len(failed_video_ids)} incomplete video(s); "
                "continuing to browser fallback.",
                flush=True,
            )
            if (is_search_target or is_candidate_target) and not video_elements:
                raise RuntimeError(
                    "TikTok API left incomplete videos and no DOM candidates are available for fallback: "
                    + ", ".join(failed_video_ids[:10])
                )
        elif (is_search_target or is_candidate_target) and not video_elements:
            if api_only:
                raise RuntimeError("TikTok API-only discovery returned no videos")
            print("[INFO] API and DOM discovery found no videos. Skipping browser fallback.")
            return

    if is_candidate_target and not api_integration:
        raise RuntimeError("TikTok candidate batches require an initialized direct API session")
    if api_only:
        raise RuntimeError(
            "TikTok API-only mode cannot use browser DOM video navigation; "
            "the direct discovery/comment phase did not complete the source"
        )

    # First phase: Click on the first video to enter video view mode
    for i in range(initial_videos_to_click):
        video_element = video_elements[i]
        video_count += 1
        print(f"\n[INFO] Processing video {video_count} of {len(video_elements) if math.isfinite(max_videos) else 'unlimited'}")

        profile_url = page.url
        try:
            # Take screenshot before clicking (for debugging if needed)
            screenshot_path = os.path.join(folders["logs"], "screenshots", f"before_click_video_{video_count}.png")
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await page.screenshot(path=screenshot_path, timeout=5000)

            # Remember the profile URL before clicking

            # Click the video element
            print(f"[INFO] Clicking on video element {i+1}")
            await video_element.click(timeout=5000)

            # Wait for the video to load
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)  # Additional wait for dynamic content

            # Check if we've successfully navigated to a video
            is_valid_page = await page.evaluate("""() => {
                return window.location.href.includes('/video/') || window.location.href.includes('/photo/');
            }""")

            if not is_valid_page:
                print("[WARN] Not on a content page after clicking. Taking diagnostic screenshot...")
                await save_page_structure(page, folders["logs"])
                print("[INFO] Initial navigation to content failed. Returning to profile and trying again...")

                # Return to the profile page if needed
                if page.url != profile_url:
                    await navigate_to_url(page, profile_url)
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                continue

            # Continue to second phase (video processing)
            break

        except Exception as e:
            print(f"[ERROR] Error clicking initial video: {e}")
            # Try to return to the profile page
            await navigate_to_url(page, profile_url)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            return  # Exit if we can't even click the first video

    # Second phase: Process videos sequentially using the "Next" button
    # If max_videos is infinity, we'll continue until there are no more videos
    max_videos_to_process = max_videos
    unlimited_mode = not math.isfinite(max_videos)

    if unlimited_mode:
        print("[INFO] Running in unlimited mode - will continue until no more videos are available")
        # Set video_limit to a large number for loop checks, but we'll break when navigation fails
        video_limit = 10000  # Just a very large number
    else:
        video_limit = max_videos

    # If we're still on the profile page, we failed to enter content viewing mode
    if not await page.evaluate("""() => {
        return window.location.href.includes('/video/') || window.location.href.includes('/photo/');
    }"""):
        print("[ERROR] Failed to enter content viewing mode. Exiting.")
        return

    if unlimited_mode:
        print(f"[INFO] Successfully entered content viewing mode. Processing unlimited videos.")
    else:
        print(f"[INFO] Successfully entered content viewing mode. Processing up to {video_limit} videos.")

    # Attach the global network listener ONCE before the loop
    processed_api_urls = set()
    global_listener = partial(handle_comment_response, processed_urls=processed_api_urls)
    page.on("response", global_listener)
    print("[API] Global network listener attached for all videos.")

    # Now process videos using the next button for navigation
    videos_processed = 0
    while videos_processed < video_limit and len(processed_video_urls) < max_videos_to_process:
        try:
            # Process current video (we're already on a video page)
            current_video_url = page.url

            # Skip if already processed
            if current_video_url in processed_video_urls:
                print(f"[INFO] Already processed video {current_video_url}. Moving to next...")

                # Try to navigate to next video
                next_success = await click_next_video(page, button_selector)
                if not next_success:
                    print("[INFO] No more videos available or navigation failed. Finishing.")
                    break

                continue

            # Show progress with more info for unlimited mode
            if unlimited_mode:
                elapsed_time = time.time() - start_time
                minutes = int(elapsed_time // 60)
                seconds = int(elapsed_time % 60)
                print(f"\n[INFO] Processing video {videos_processed+1} (URL: {current_video_url})")
                print(f"[PROGRESS] Videos: {videos_processed}/{successful_videos} successful, Comments: {total_comments_scraped}, Time: {minutes}m {seconds}s")
            else:
                print(f"\n[INFO] Processing video {videos_processed+1} (URL: {current_video_url})")

            # --- START: Extract Caption and Video ID ---
            video_caption = ""
            current_video_id = None
            username = ""  # Add username variable
            try:
                # Extract Video ID from URL
                if api_integration and api_integration.api: # Use initialized API if possible
                    current_video_id = api_integration.api.extract_video_id(current_video_url)
                elif api_integration: # Use fallback method from initialized integration object
                    current_video_id = api_integration.extract_video_id_from_url(current_video_url)
                else: # Create temporary instance if api_integration is None
                     temp_api_integration = TikTokAPIIntegration(enable_api=False)
                     current_video_id = temp_api_integration.extract_video_id_from_url(current_video_url)

                if current_video_id:
                    print(f"[INFO] Extracted Video ID: {current_video_id}")
                else:
                    print("[WARN] Could not extract Video ID from URL.")

                # Define selectors based on provided HTML
                caption_container_selector = "div[data-e2e='browse-video-desc']" # Use single quotes inside
                # Try simpler selector for the button within the text wrapper div
                more_button_selector = "div.css-bs495z-DivWrapper >> button.css-1kmeri5-ButtonExpand"
                # Add username selector
                username_selector = "span[data-e2e='browse-username'], a[data-e2e='browse-username']"

                # Try to click the 'more' button if visible
                try:
                    more_button = page.locator(more_button_selector).first
                    # Add a check for existence before visibility check
                    if await more_button.count() > 0 and await more_button.is_visible(timeout=1000):
                        print("[INFO] Clicking 'more' button for caption...")
                        await more_button.click(timeout=2000)
                        await page.wait_for_timeout(500) # Wait briefly for expansion
                except Exception:
                    # Fails silently if button not found, not visible, or click fails
                    pass

                # Extract caption text
                caption_container = page.locator(caption_container_selector).first
                if await caption_container.count() > 0:
                    video_caption = await caption_container.inner_text()
                    video_caption = video_caption.strip()
                    safe_caption = video_caption[:100].encode('ascii', 'ignore').decode('ascii')
                    print(f"[INFO] Extracted Caption (first 100 chars): {safe_caption}...")
                else:
                     print(f"[WARN] Caption container '{caption_container_selector}' not found.")

                # Extract username
                username_element = page.locator(username_selector).first
                if await username_element.count() > 0:
                    username = await username_element.inner_text()
                    username = username.strip()
                    if username.startswith('@'):
                        username = username[1:]  # Remove @ symbol if present
                    safe_username = username.encode('ascii', 'ignore').decode('ascii')
                    print(f"[INFO] Extracted Username: {safe_username}")
                else:
                    # Try alternative method - extract from URL
                    url_username_match = re.search(r'/@([^/?]+)', current_video_url)
                    if url_username_match:
                        username = url_username_match.group(1)
                        print(f"[INFO] Extracted Username from URL: {username}")
                    else:
                        print(f"[WARN] Username element '{username_selector}' not found and could not extract from URL.")

            except Exception as desc_err:
                safe_err = str(desc_err).encode('ascii', 'ignore').decode('ascii')
                print(f"[ERROR] Failed to extract video description/ID/username: {safe_err}")
            # --- END: Extract Caption and Video ID ---

            transcript_metadata = {}
            if current_video_id:
                transcript_collector = api_integration or TikTokAPIIntegration(enable_api=False)
                transcript_metadata = await transcript_collector.get_transcript_for_video(
                    page,
                    {
                        "id": current_video_id,
                        "url": current_video_url,
                        "caption": video_caption,
                        "username": username,
                    },
                )
                print(
                    f"[INFO] TikTok transcript status for {current_video_id}: "
                    f"{transcript_metadata.get('transcript_status', 'unknown')} "
                    f"({transcript_metadata.get('transcript_segment_count', 0)} segments)",
                    flush=True,
                )

            # --- START: Check if we should refresh API tokens ---
            if api_integration and videos_since_refresh >= 10:
                print("[INFO] Refreshing API tokens to maintain session validity...")
                refresh_success = await api_integration.refresh_tokens(page)
                if refresh_success:
                    print("[INFO] API tokens refreshed successfully")
                    api_failures = 0  # Reset failure counter after successful refresh
                    videos_since_refresh = 0
                else:
                    print("[WARN] Failed to refresh API tokens")
            # --- END: Check if we should refresh API tokens ---

            # --- START: Try API approach first if enabled ---
            comments = []
            total_comment_count = 0
            logs = []
            api_success = False

            # Check if we should try using the API based on our state
            should_try_api = api_integration and (
                not using_fallback or  # Not in fallback mode
                (using_fallback and time.time() - last_api_failure_time > api_backoff_minutes * 60)  # Backoff period passed
            )

            # Apply rate limiting to API requests
            current_time = time.time()
            if should_try_api and current_time - last_api_request_time < min_time_between_requests:
                time_to_wait = min_time_between_requests - (current_time - last_api_request_time)
                print(f"[INFO] Rate limiting API requests. Waiting {time_to_wait:.1f} seconds...")
                await asyncio.sleep(time_to_wait)

            if should_try_api:
                # Record API request time for rate limiting
                last_api_request_time = time.time()

                # Use the already extracted video_id if available
                if current_video_id:
                    print(f"[INFO] Using API to fetch comments for video ID: {current_video_id}")
                    # Attempt to get comments via API
                    api_comments = await api_integration.get_comments_for_video(
                        current_video_id,
                        current_video_url,
                        max_comments=max_comments,
                    )

                    if api_comments:
                        # --- Pass caption/ID to process_api_comments if needed (currently not needed) ---
                        comments = process_api_comments(api_comments)
                        total_comment_count = len(comments) # Or get from API if available
                        logs.append(f"Successfully fetched {len(comments)} comments via API")
                        api_success = True
                        print(f"[SUCCESS] Scraped {len(comments)} comments from video {videos_processed+1} using API")
                        api_failures = 0  # Reset failure counter
                        api_consecutive_failures = 0  # Reset consecutive failure counter
                        using_fallback = False  # No longer using fallback

                        # --- Pass caption/ID to save_comments_to_file ---
                        await save_comments_to_file(
                            comments,
                            videos_processed + 1,
                            folders["comments"],
                            video_id=current_video_id,
                            caption=video_caption,
                            username=username,
                            extra_metadata={
                                "url": current_video_url,
                                "platform": "tiktok",
                                "discovery_method": "browser_dom",
                                "metadata_method": "browser_dom",
                                "comment_method": "tiktok_comment_api",
                                "fallback_used": True,
                                **transcript_metadata,
                            },
                            filename_prefix=output_prefix,
                            platform="tiktok",
                        )

                        # Update our stats
                        total_comments_scraped += len(comments)
                        successful_videos += 1
                    else:
                        print("[INFO] API comment fetch returned no results, falling back to browser method")
                        api_failures += 1
                        api_consecutive_failures += 1
                        last_api_failure_time = time.time()
                        using_fallback = True
                else:
                    print("[INFO] Could not detect video ID, falling back to browser method")
                    api_failures += 1
                    api_consecutive_failures += 1
                    last_api_failure_time = time.time()
                    using_fallback = True

                # Check if we need to refresh tokens due to consecutive failures
                if api_consecutive_failures >= max_api_failures:
                    print(f"[INFO] Detected {api_consecutive_failures} consecutive API failures. Refreshing tokens...")
                    refresh_success = await api_integration.refresh_tokens(page)
                    if refresh_success:
                        print("[INFO] API tokens refreshed successfully after failures")
                        api_consecutive_failures = 0  # Reset consecutive failures only
                        videos_since_refresh = 0  # Reset refresh counter

                        # Try API again immediately if refresh was successful
                        if not api_success and current_video_id:
                            print("[INFO] Retrying API request with fresh tokens...")
                            # Add a short delay before retry
                            await asyncio.sleep(2)
                            # Record API request time for rate limiting
                            last_api_request_time = time.time()

                            api_comments = await api_integration.get_comments_for_video(
                                current_video_id,
                                current_video_url,
                                max_comments=max_comments,
                            )
                            if api_comments:
                                comments = process_api_comments(api_comments)
                                total_comment_count = len(comments)
                                logs.append(f"Successfully fetched {len(comments)} comments via API after token refresh")
                                api_success = True
                                print(f"[SUCCESS] Scraped {len(comments)} comments from video {videos_processed+1} using API after token refresh")
                                using_fallback = False

                                await save_comments_to_file(
                                    comments,
                                    videos_processed + 1,
                                    folders["comments"],
                                    video_id=current_video_id,
                                    caption=video_caption,
                                    username=username,
                                    extra_metadata={
                                        "url": current_video_url,
                                        "platform": "tiktok",
                                        "discovery_method": "browser_dom",
                                        "metadata_method": "browser_dom",
                                        "comment_method": "tiktok_comment_api",
                                        "fallback_used": True,
                                        **transcript_metadata,
                                    },
                                    filename_prefix=output_prefix,
                                    platform="tiktok",
                                )
                                total_comments_scraped += len(comments)
                                successful_videos += 1
                    else:
                        print("[WARN] Failed to refresh API tokens after failures")

                # Increment counter for token refresh scheduling
                videos_since_refresh += 1
            else:
                if using_fallback:
                    time_passed = (time.time() - last_api_failure_time) / 60
                    time_to_wait = api_backoff_minutes - time_passed
                    print(f"[INFO] Using fallback method ({time_to_wait:.1f} minutes until API retry)")
            # --- END: Try API approach first if enabled ---

            # --- START: Fall back to browser method if API failed ---
            if not api_success:
                print("[INFO] Falling back to browser-based comment extraction...")

                # Try to open the comment section (still necessary to reveal the area)
                comment_section_open = await open_comment_section(page, comment_button_selector)

                if comment_section_open:
                    print("[API] Comment section opened. Attempting to trigger API calls by scrolling...")
                    # Find the comment container to scroll within it
                    # Common selectors - might need adjustment
                    comment_container_selector = (
                        ".css-7whb78-DivCommentListContainer, "
                        ".css-1qp5gj2-DivCommentListContainer, "
                        "div[class*=\"-DivCommentListContainer\"]"
                    )
                    try:
                        comment_container = page.locator(comment_container_selector).first

                        # --- START: Improved Scrolling Logic ---
                        max_scroll_attempts = 50  # Increased attempts significantly
                        scroll_wait_ms = 1000     # Increased wait to ensure API responses complete
                        no_change_count = 0
                        max_no_change = 3         # Stop after 3 scrolls with no position change
                        last_scroll_top = -1

                        print(f"[API] Scrolling comment container (max {max_scroll_attempts} attempts, wait {scroll_wait_ms}ms)...")
                        for i in range(max_scroll_attempts):
                            # Get current scroll position
                            current_scroll_top = await comment_container.evaluate("(element) => { return element.scrollTop; }")

                            # Perform a visible scroll animation to trigger events in browser
                            await page.evaluate("""(selector) => {
                                const container = document.querySelector(selector);
                                if (container) {
                                    // Scroll with animation that's visible to TikTok's JS
                                    const targetScroll = container.scrollHeight;
                                    const startScroll = container.scrollTop;
                                    const scrollDiff = targetScroll - startScroll;
                                    const duration = 500; // ms
                                    const start = performance.now();

                                    function step(timestamp) {
                                        const elapsed = timestamp - start;
                                        const progress = Math.min(elapsed / duration, 1);
                                        const easeProgress = 0.5 - Math.cos(progress * Math.PI) / 2; // Smooth easing
                                        container.scrollTop = startScroll + scrollDiff * easeProgress;
                                        if (progress < 1) requestAnimationFrame(step);
                                    }

                                    requestAnimationFrame(step);
                                }
                            }""", comment_container_selector)

                            # Wait for scrolling to complete and potential API responses
                            await page.wait_for_timeout(scroll_wait_ms)

                            # Check if scroll position changed
                            new_scroll_top = await comment_container.evaluate("(element) => { return element.scrollTop; }")
                            print(f"[API] Scroll attempt {i+1}/{max_scroll_attempts}, Pos: {new_scroll_top}")

                            if new_scroll_top == last_scroll_top: # Use last_scroll_top from *before* this iteration's scroll
                                no_change_count += 1
                                print(f"[API] Scroll position unchanged ({no_change_count}/{max_no_change}).")
                                if no_change_count >= max_no_change:
                                    print("[API] Stopping scroll - likely reached the end.")
                                    break
                            else:
                                no_change_count = 0 # Reset if scroll position changed

                            last_scroll_top = new_scroll_top # Update for next iteration's check

                            # Try to directly trigger the load more comments button if present
                            try:
                                load_more_selector = '.css-y1m958-button-ButtonMore'
                                more_button = page.locator(load_more_selector)
                                if await more_button.count() > 0:
                                    print("[API] Found 'Load more comments' button. Clicking...")
                                    await more_button.click()
                                    await page.wait_for_timeout(1500)  # Wait longer after clicking
                            except Exception as btn_err:
                                pass  # Ignore if button not found or click fails

                        if i == max_scroll_attempts - 1:
                            print("[API] Reached max scroll attempts.")
                        # --- END: Improved Scrolling Logic ---

                        print("[API] Finished scroll attempts. Waiting for final responses...")
                        await page.wait_for_timeout(5000) # Extra wait time for listeners to process

                    except Exception as scroll_err:
                        print(f"[API] Error trying to scroll comment container: {scroll_err}. Captured comments might be incomplete.")
                        await page.wait_for_timeout(5000) # Wait anyway
                else:
                    print("[WARN] Failed to open comment section. Cannot trigger comment API calls. Skipping comment capture for this video.")
                    # Optionally try alternative open methods here if needed, similar to before

                # Retrieve the captured comments from the global dictionary
                comments = global_intercepted_comments.get(current_video_id, [])
                total_comment_count = len(comments)
                logs.append(f"Captured {len(comments)} comments via network interception.")

                # Save the comments to a file if any were found
                if comments:
                    # --- Pass caption/ID to save_comments_to_file ---
                    await save_comments_to_file(
                        comments,
                        videos_processed + 1,
                        folders["comments"],
                        video_id=current_video_id,
                        caption=video_caption,
                        username=username,
                        extra_metadata={
                            "url": current_video_url,
                            "platform": "tiktok",
                            "discovery_method": "browser_dom",
                            "metadata_method": "browser_dom",
                            "comment_method": "browser_dom",
                            "fallback_used": True,
                            **transcript_metadata,
                        },
                        filename_prefix=output_prefix,
                        platform="tiktok",
                    )

                    # Update our stats
                    total_comments_scraped += len(comments)
                    successful_videos += 1
            # --- END: Fall back to browser method if API failed ---

            # Save diagnostic information (using the captured comments)
            await save_diagnostic_info(page, videos_processed+1, comments, total_comment_count, logs, folders["logs"], video_id=current_video_id, caption=video_caption, username=username)

            if not comments:
                await save_error_logs(logs, videos_processed+1, folders["logs"])
            else:
                processed_video_urls.add(current_video_url)
                save_checkpoint(identifier, processed_video_urls, target_url or page.url)
                percentage = 0
                if total_comment_count > 0:
                    percentage = round((len(comments) / total_comment_count) * 100, 2)

                print(f"[SUCCESS] Scraped {len(comments)} comments from video {videos_processed+1}")
                if total_comment_count > 0:
                    print(f"[INFO] That's approximately {percentage}% of the total {total_comment_count} comments")

            # Wait a bit before trying to navigate to next video
            await asyncio.sleep(3)

            # Try to navigate to next video directly within TikTok
            print(f"[INFO] Navigating to next video...")
            next_success = await click_next_video(page, button_selector)

            if not next_success:
                if unlimited_mode:
                    print("[INFO] No more videos available. Completed scraping all accessible videos.")
                else:
                    print("[INFO] No more videos available or navigation failed. Finishing.")
                break

            # Increment counter for processed videos
            videos_processed += 1

        except Exception as e:
            print(f"[ERROR] Error processing video: {e}")

            # Try to navigate to next video and continue
            try:
                next_success = await click_next_video(page, button_selector)
                if not next_success:
                    print("[INFO] Navigation failed after error. Finishing.")
                    break
                videos_processed += 1
            except Exception as nav_err:
                print(f"[ERROR] Failed to navigate after error: {nav_err}")
                break

        # Wait between videos
        await asyncio.sleep(3)

    # Print final stats
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)

    print("\n" + "="*60)
    print(" Scraping Summary ".center(60, "="))
    print("="*60)
    print(f"Total videos processed: {videos_processed}")
    print(f"Videos with comments: {successful_videos}")
    print(f"Total comments scraped: {total_comments_scraped}")
    print(f"Total time: {minutes} minutes, {seconds} seconds")
    print(f"Average time per video: {int(elapsed_time / videos_processed)} seconds")
    print(f"Average comments per video: {round(total_comments_scraped / videos_processed, 1)}")
    print("="*60 + "\n")

    print(f"[INFO] Finished processing {videos_processed} videos.")
    print(f"[INFO] Processed {len(processed_video_urls)} unique video URLs.")

async def save_comments_to_file(
    comments,
    video_number,
    folder_path,
    video_id: Optional[str] = None,
    caption: Optional[str] = None,
    username: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
    filename_prefix: str = "",
    platform: Optional[str] = None,
):
    """Save comments to a JSON file, including video ID, caption, and username.

    Args:
        comments: List of comment objects
        video_number: Current video number being processed
        folder_path: Path to the folder where comments should be saved
        video_id: Extracted video ID (optional)
        caption: Extracted video caption (optional)
        username: Extracted video username (optional)
        extra_metadata: Additional video-level fields to include in the JSON
    """
    if not comments:
        print(f"[WARN] No comments found for video {video_number}; saving zero-comment record")

    # Prefixes isolate per-platform files inside combined sessions while keeping
    # legacy single-platform filenames unchanged.
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", filename_prefix or "")
    filename = f"{safe_prefix}video_{video_number}_comments.json"
    file_path = os.path.join(folder_path, filename)

    # Create the output data structure
    output_data = {
        "video_number": video_number,
        "video_id": video_id,
        "caption": caption,
        "username": username,
        "comment_count": len(comments),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "comments": comments
    }
    if platform:
        output_data["platform"] = platform
    if extra_metadata:
        output_data.update(extra_metadata)
    from .raw_contract import finalize_content_record

    output_data = finalize_content_record(
        output_data,
        platform=platform or output_data.get("platform"),
    )

    # Save to file
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        print(f"[INFO] Saved {len(comments)} comments to {filename}")
        return file_path
    except Exception as e:
        print(f"[ERROR] Failed to save comments to file: {e}")
        raise RuntimeError(f"Failed to save comments to {file_path}: {e}") from e

async def save_diagnostic_info(page, video_number, comments, total_comment_count, logs, folder_path, video_id: Optional[str] = None, caption: Optional[str] = None, username: Optional[str] = None):
    """Save diagnostic information about the scraping process for a video.

    Args:
        page: Playwright page object
        video_number: Current video number being processed
        comments: List of scraped comments
        total_comment_count: Total number of comments reported by TikTok (if available)
        logs: List of log messages for this video
        folder_path: Path to the logs folder
        video_id: Extracted video ID (optional)
        caption: Extracted video caption (optional)
        username: Extracted video username (optional)
    """
    filename = f"video_{video_number}_diagnostics.txt"
    diag_folder = os.path.join(folder_path, "diagnostics") # Specific subfolder
    os.makedirs(diag_folder, exist_ok=True)
    file_path = os.path.join(diag_folder, filename)

    try:
        # Use page.url inside the try block in case page object becomes invalid
        current_url = "N/A"
        try:
             current_url = page.url
        except Exception:
             logs.append("[ERROR] Could not get page URL for diagnostics.")

        caption_display = caption if caption else 'Not Extracted'
        # Truncate long captions for logs
        if len(caption_display) > 200:
            caption_display = caption_display[:200] + "..."

        content = f"""
Diagnostic Report for Video {video_number}
=======================================
Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}
Video URL: {current_url}
Video ID: {video_id if video_id else 'Not Extracted'}
Caption: {caption_display}
Username: {username if username else 'Not Extracted'}

Reported Total Comments: {total_comment_count if total_comment_count is not None else 'N/A'}
Scraped Comments: {len(comments)}

Logs:
-----
"""
        content += "\n".join(logs)

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

    except Exception as e:
        print(f"[ERROR] Failed to save diagnostic info: {e}")

async def save_error_logs(logs, video_number, folder_path):
    """Save logs specifically when errors occur or no comments are scraped."""
    error_folder = os.path.join(folder_path, "errors") # Specific subfolder
    os.makedirs(error_folder, exist_ok=True)
    filename = f"video_{video_number}_error.log"
    file_path = os.path.join(error_folder, filename)

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(f"Error Log for Video {video_number}\n========================\n")
            f.write("\n".join(logs))
        # Only print info if logs actually contain error messages maybe?
        # print(f"[INFO] Saved error log to {filename}")
    except Exception as e:
        print(f"[ERROR] Failed to save error log: {e}")

async def main(
    record_mode=False,
    num_videos=3,
    default_percentage=50,
    target_url=None,
    max_videos=500,
    discovery_videos=0,
    max_comments=0,
    use_api=True,
    reset_checkpoint=False,
    session_name=None,
    output_prefix="",
    candidate_videos=None,
):
    """Main function to run the TikTok scraper.

    Args:
        record_mode: Whether to run in recording mode to capture the comment button
        num_videos: Number of videos to scrape (default reduced to 3 to prioritize quality)
        default_percentage: Default percentage of comments to scrape (if not specified by user)
        target_url: Specific TikTok URL to scrape (e.g., a profile page)
        max_videos: Maximum number of videos to scrape, overrides the detected video count
        discovery_videos: Candidate metadata limit before date/relevance/dedupe gates
        use_api: Whether to attempt using the direct API for comment scraping
        reset_checkpoint: Whether to reset the checkpoint file and start a fresh scrape
        candidate_videos: Optional direct candidate batch, bypassing search/profile discovery
    """
    # Create folder structure for comments and logs
    folders = create_session_folders(target_url=target_url, session_name=session_name)
    discovery_limit = discovery_videos if discovery_videos and discovery_videos > 0 else max_videos

    # This legacy transport may attach only to the already running, verified
    # shared Profile 7 browser. It must never kill Edge, launch an alternate
    # profile, or inject credentials from a file.
    if _env_flag("TIKTOK_CLOSE_EDGE", False):
        raise RuntimeError("TIKTOK_CLOSE_EDGE is disabled; the shared Edge browser must remain running")
    if _env_flag("TIKTOK_FORCE_PLAYWRIGHT", False):
        raise RuntimeError("Standalone Playwright TikTok profiles are disabled; attach to Profile 7 over CDP")

    expected_user_data_dir = Path(
        r"C:\Users\DELL\AppData\Local\Microsoft\Edge\User Data"
    ).resolve()
    expected_profile_directory = "Profile 7"
    expected_profile_path = (expected_user_data_dir / expected_profile_directory).resolve()

    configured_edge_profile = os.environ.get("TIKTOK_EDGE_PROFILE_PATH", "").strip()
    if configured_edge_profile and Path(configured_edge_profile).resolve() != expected_profile_path:
        raise RuntimeError("TIKTOK_EDGE_PROFILE_PATH must resolve to the designated Edge Profile 7")

    persistent_user_data_dir = os.environ.get("TIKTOK_PLAYWRIGHT_USER_DATA_DIR", "").strip()
    if persistent_user_data_dir:
        raise RuntimeError("TIKTOK_PLAYWRIGHT_USER_DATA_DIR is disabled for the legacy TikTok transport")

    requested_profile_directory = (
        os.environ.get("TIKTOK_PROFILE_DIRECTORY", "").strip()
        or expected_profile_directory
    )
    if requested_profile_directory != expected_profile_directory:
        raise RuntimeError("TIKTOK_PROFILE_DIRECTORY must be exactly 'Profile 7'")

    if os.environ.get("TIKTOK_COOKIE_CURL_FILE", "").strip():
        raise RuntimeError("TikTok cookie-file injection is disabled; use the existing Profile 7 session")

    configured_cdp_url = os.environ.get("TIKTOK_CDP_URL", "").strip()
    if not configured_cdp_url:
        raise RuntimeError(
            "The legacy TikTok transport requires an existing verified Profile 7 CDP endpoint; "
            "start or reuse it through social_browser.py and pass --tiktok-cdp-url"
        )

    # Use the provided URL or default to TikTok homepage
    initial_url = target_url if target_url else "https://www.tiktok.com"

    # Checkpoint reset logic
    if reset_checkpoint:
        identifier = get_checkpoint_identifier(initial_url)
        checkpoint_file = os.path.join("comments_data", "checkpoints", f"{identifier}_checkpoint.json")
        if os.path.exists(checkpoint_file):
            try:
                os.remove(checkpoint_file)
                print(f"[INFO] Checkpoint reset for {identifier}")
            except Exception as e:
                print(f"[WARN] Could not reset checkpoint: {e}")

    from social_browser import browser_status as get_social_browser_status

    try:
        preflight = await get_social_browser_status(
            configured_cdp_url,
            expected_profile=expected_user_data_dir,
            expected_profile_directory=expected_profile_directory,
        )
    except Exception as exc:
        raise RuntimeError("Could not verify the existing Profile 7 social browser") from exc

    profile_identity = preflight.get("profile") or {}
    if not preflight.get("reachable") or not profile_identity.get("verified"):
        raise RuntimeError("The supplied CDP endpoint is not verified Edge Profile 7")
    if profile_identity.get("observed_profile_directory") != expected_profile_directory:
        raise RuntimeError("The supplied CDP endpoint is not the designated Profile 7 directory")
    if not (preflight.get("platforms", {}).get("tiktok") or {}).get("authenticated"):
        raise RuntimeError("The verified Profile 7 TikTok session is not authenticated")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(configured_cdp_url)
        except Exception as exc:
            raise RuntimeError("Could not attach to the verified Profile 7 CDP session") from exc

        contexts = list(browser.contexts)
        context_index = int(profile_identity.get("context_index") or 0)
        if not contexts or not 0 <= context_index < len(contexts):
            raise RuntimeError("The verified Profile 7 browser context is unavailable")
        context = contexts[context_index]
        page = await context.new_page()
        await page.goto(initial_url, wait_until="domcontentloaded", timeout=45000)

        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(page)
            print("[INFO] Applied Playwright stealth to main page.")
        except ImportError:
            print("[WARNING] playwright_stealth not installed, skipping stealth mode.")

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        print("[INFO] TikTok browser ready. Current URL =", page.url)

        try:
            tiktok_cookies = await context.cookies(["https://www.tiktok.com"])
        except Exception:
            tiktok_cookies = []
        cookie_names = {str(cookie.get("name") or "") for cookie in tiktok_cookies}
        authenticated = bool({"sessionid", "sessionid_ss", "sid_tt"} & cookie_names)
        print(
            f"[INFO] TikTok session preflight: authenticated={authenticated} "
            f"cookie_count={len(cookie_names)} require_auth={require_auth}"
        )
        if require_auth and not authenticated:
            await browser.close()
            terminate_proc()
            raise RuntimeError(
                "The selected TikTok profile is not authenticated; refusing to run search with a guest session"
            )

        # --- START: Add code to get total video count ---
        total_videos_on_profile = None
        if target_url and "/@" in target_url: # Basic check if it's a profile URL
            try:
                # Attempt to find the element containing the video count
                # NOTE: This selector might need adjustment based on TikTok's current structure
                video_count_selector = 'strong[data-e2e="user-post-count"]'
                # Alternative selectors if the primary one fails
                alt_selectors = [
                    'span[data-e2e="user-tab-count"]', # Sometimes used in tabs
                    '.video-count', # Generic class (less reliable)
                     # Selector based on structure observed (e.g., a div containing "Videos" and a number)
                    '//h2[contains(text(), "Videos")]/following-sibling::span'
                ]

                count_element = page.locator(video_count_selector)
                count_text = await count_element.text_content(timeout=5000)

                if not count_text or not count_text.strip().isdigit():
                     print(f"[WARN] Primary selector '{video_count_selector}' failed or returned non-numeric content. Trying alternatives...")
                     for alt_selector in alt_selectors:
                         try:
                             count_element = page.locator(alt_selector)
                             count_text = await count_element.text_content(timeout=3000)
                             if count_text and count_text.strip().isdigit():
                                 print(f"[INFO] Found count using alternative selector: {alt_selector}")
                                 break # Found a valid count
                         except Exception:
                             print(f"[DEBUG] Alternative selector '{alt_selector}' failed.")
                         count_text = None # Reset if alternative failed

                if count_text and count_text.strip().isdigit():
                    total_videos_on_profile = int(count_text.strip())
                    print(f"\n[INFO] Total videos found on profile: {total_videos_on_profile}\n")
                else:
                    print("[WARN] Could not determine total video count from profile page.")

            except Exception as e:
                print(f"[WARN] Error extracting total video count: {e}")
        # --- END: Add code to get total video count ---

        # Handle record mode if selected
        if record_mode:
            print("\n" + "="*80)
            print("RECORDER ACTIVE: Please manually navigate to a TikTok video".center(80))
            print("="*80)
            print("\nSTEP 1: Navigate to a TikTok video with comments")
            print("STEP 2: When you're ready, you'll be asked to click on 2 elements:")
            print("  * The comment button (to open comments)")
            print("  * The comment count display (showing number of comments)")
            print("\nRecording these elements helps the scraper accurately identify")
            print("comment sections and count totals for better scraping results.")

            # Wait for user to get to a video
            print("\nPress Enter when you're on a TikTok video with comments...")
            input()

            # Record both elements
            success = await record_elements(page, folders)

            if success:
                print("\n" + "="*80)
                print("SUCCESS: Elements recorded successfully!".center(80))
                print("="*80)
                print("\nRun the script without --record next time to use this information.")
                print("\nExiting recording mode...")
            else:
                print("\n" + "="*80)
                print("ERROR: Failed to record elements properly".center(80))
                print("="*80)
                print("\nPossible reasons:")
                print("- No elements were clicked within the time limit")
                print("- The clicked elements weren't properly detected")
                print("- There was an error saving the element information")
                print("\nPlease try again with the --record flag.")

            # Make it clear we're exiting
            print("\nRecording session complete. Closing browser...")
            time.sleep(3)  # Give the user time to read the message

            # Close browser and exit
            await browser.close()
            terminate_proc()
            return  # This explicit return ensures the script doesn't continue

        # Load recorded element information for comment counting and button clicking
        recorded_elements = load_recorded_elements(folder_path=folders["logs"])
        if recorded_elements:
            print("[INFO] Successfully loaded recorded elements for comment counting")

            # If we have recorded elements, use the comment_button from there
            if "comment_button" in recorded_elements:
                comment_button_selector = recorded_elements["comment_button"]["css_selector"]
                print(f"[INFO] Using comment button selector from recorded elements: {comment_button_selector}")
            else:
                # Fall back to loading the old-style single element
                element_info = load_element_info(folder_path=folders["logs"])
                if element_info:
                    comment_button_selector = element_info.get("css_selector")
                    print(f"[INFO] Using legacy recorded comment button selector: {comment_button_selector}")
                else:
                    # Use default selector
                    comment_button_selector = 'span[data-e2e="comment-icon"] > svg > use'
                    print(f"[INFO] No recorded button found. Using default selector: {comment_button_selector}")
        else:
            # Load recorded element information if available (legacy approach)
            element_info = load_element_info(folder_path=folders["logs"])
            if element_info:
                # Use the recorded CSS selector as primary, with XPath as fallback
                comment_button_selector = element_info.get("css_selector")
                print(f"[INFO] Using legacy recorded comment button selector: {comment_button_selector}")
            else:
                # Fallback to the previous approach, but first try our known good selector
                print("[INFO] No recorded element information found. Using pre-recorded selector.")
                # Use the selector we captured with button_recorder.py
                comment_button_selector = 'span[data-e2e="comment-icon"] > svg > use'

                # If that doesn't work, fall back to the original selector
                if not await page.locator(comment_button_selector).count():
                    print("[INFO] Pre-recorded selector not found. Using default selector.")
                    comment_button_selector = 'button[aria-label^="Read or add comments"]'

        # Selector for the video navigation buttons (which may include both next and previous)
        button_selector = "button.TUXButton.TUXButton--capsule.TUXButton--medium.TUXButton--secondary.action-item.css-1egy55o:not([disabled])"

        # Direct content URLs do not expose a profile/explore grid, so route them
        # straight through the minimal API method instead of trying to find tiles.
        if candidate_videos:
            print(f"[INFO] Direct candidate batch detected: {len(candidate_videos)} videos")
            await process_videos_by_clicking(
                page,
                [],
                comment_button_selector,
                folders,
                recorded_elements,
                max_videos,
                discovery_videos=discovery_limit,
                use_api=use_api,
                target_url=target_url or "candidate_batch",
                max_comments=max_comments,
                output_prefix=output_prefix,
                candidate_videos=candidate_videos,
            )
            await browser.close()
            terminate_proc()
            return

        if target_url and re.search(r'/(video|photo)/\d+', target_url):
            current_video_url = page.url if re.search(r'/(video|photo)/\d+', page.url) else target_url
            api_integration = TikTokAPIIntegration(enable_api=use_api)
            current_video_id = api_integration.extract_video_id_from_url(current_video_url) or api_integration.extract_video_id_from_url(target_url)
            username = ""
            video_caption = ""

            url_username_match = re.search(r'/@([^/?]+)', current_video_url)
            if url_username_match:
                username = url_username_match.group(1)

            try:
                caption_container = page.locator("div[data-e2e='browse-video-desc'], [data-e2e='video-desc']").first
                if await caption_container.count() > 0:
                    video_caption = (await caption_container.inner_text(timeout=2000)).strip()
            except Exception:
                video_caption = ""

            if not use_api:
                print("[ERROR] Direct video URL scraping currently requires API mode. Re-run without --no-api.")
                await browser.close()
                terminate_proc()
                return

            if not current_video_id:
                print("[ERROR] Could not extract video ID from direct target URL.")
                await browser.close()
                terminate_proc()
                return

            print(f"[INFO] Direct video URL detected. Fetching comments by API for ID: {current_video_id}")
            api_initialized = await api_integration.initialize_api(page)
            if not api_initialized:
                print("[ERROR] Could not initialize TikTok API for direct video URL.")
                await browser.close()
                terminate_proc()
                return

            api_comments = await api_integration.get_comments_for_video(
                current_video_id,
                current_video_url,
                max_comments=max_comments,
            )
            comments = process_api_comments(api_comments) if api_comments else []
            transcript_metadata = await api_integration.get_transcript_for_video(
                page,
                {
                    "id": current_video_id,
                    "url": current_video_url,
                    "caption": video_caption,
                    "username": username,
                },
            )
            if not comments:
                print("[WARN] Direct API returned no comments; saving the post and transcript result.")

            await save_comments_to_file(
                comments,
                1,
                folders["comments"],
                video_id=current_video_id,
                caption=video_caption,
                username=username,
                extra_metadata={
                    "url": current_video_url,
                    "platform": "tiktok",
                    "discovery_method": "direct_url",
                    "metadata_method": "browser_dom",
                    "comment_method": "tiktok_comment_api",
                    **transcript_metadata,
                },
                filename_prefix=output_prefix,
                platform="tiktok",
            )
            print(
                f"[SUCCESS] Direct API saved {len(comments)} comments and "
                f"{transcript_metadata.get('transcript_segment_count', 0)} transcript segments "
                f"for video ID {current_video_id}"
            )
            await browser.close()
            terminate_proc()
            return

        # If we have a target URL, look for videos on the profile page
        if target_url:
            from .raw_contract import transport_mode

            api_only = transport_mode() == "api-only"
            # Make sure we're on the target URL
            if page.url != target_url:
                print(f"[INFO] Navigating to target URL: {target_url}")
                await navigate_to_url(page, target_url)
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                print(f"[INFO] Successfully navigated to: {page.url}")

            if api_only and "search/video?q=" not in target_url:
                raise RuntimeError(
                    "TikTok API-only profile inventory is not available yet; "
                    "use api-first for explicit, recorded DOM fallback"
                )

            # API-only search does not read DOM cards; the signed search request
            # is replayed by the direct discovery phase below.
            video_elements = [] if api_only else await find_profile_videos(
                page,
                folders["logs"],
                max_videos=discovery_limit,
            )

            if not video_elements or len(video_elements) == 0:
                if "search/video?q=" in target_url:
                    print("[WARN] No DOM video elements found on search page; continuing with Search API discovery.")
                    video_elements = []
                else:
                    print("[ERROR] No video elements found on the profile page. Exiting.")

                    # Before giving up, take screenshots for debugging
                    await save_page_structure(page, folders["logs"])

                    # Attempt to salvage the session by opening the Videos tab explicitly
                    try:
                        print("[INFO] Attempting to click on the Videos tab...")
                        # Try clicking on the Videos tab (common on profile pages)
                        await page.click('text="Videos"', timeout=5000)
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)

                        # Try finding videos again
                        video_elements = await find_profile_videos(page, folders["logs"], max_videos=discovery_limit)
                        if not video_elements or len(video_elements) == 0:
                            print("[ERROR] Still no video elements found after clicking Videos tab. Exiting.")
                            await browser.close()
                            terminate_proc()
                            return
                    except Exception as tab_err:
                        print(f"[ERROR] Failed to click Videos tab: {tab_err}")
                        await browser.close()
                        terminate_proc()
                        return

            print(f"[INFO] Found {len(video_elements)} video elements to scrape")

            # Process videos by clicking on them directly - pass the use_api parameter
            try:
                await process_videos_by_clicking(
                    page,
                    video_elements,
                    comment_button_selector,
                    folders,
                    recorded_elements,
                    max_videos,
                    discovery_videos=discovery_limit,
                    use_api=use_api,
                    target_url=target_url,
                    max_comments=max_comments,
                    output_prefix=output_prefix,
                )
            except TikTokSearchSessionError as exc:
                print(f"[ERROR] {exc}")
                try:
                    await browser.close()
                except Exception:
                    pass
                terminate_proc()
                raise

        else:
            # Original flow: Process a fixed number of videos in sequence using the "next" button
            for i in range(num_videos):
                print(f"\n[INFO] Processing video {i+1} of {num_videos}")
                await page.wait_for_load_state("networkidle", timeout=15000)

                # Try to open the comment section
                await open_comment_section(page, comment_button_selector)

                # Get the total comment count using the recorded elements
                total_comment_count = await extract_total_comment_count(page, recorded_elements)
                print(f"\n[INFO] Total comment count for video {i+1}: {total_comment_count}")

                # Ask the user what percentage of comments to scrape
                target_percentage = default_percentage
                try:
                    user_input = input(f"What percentage of comments do you want to scrape for this video? (1-100, default: {default_percentage}%): ")
                    if user_input.strip():
                        user_percentage = int(user_input.strip())
                        if 1 <= user_percentage <= 100:
                            target_percentage = user_percentage
                        else:
                            print(f"[INFO] Invalid percentage. Using default: {default_percentage}%")
                    else:
                        print(f"[INFO] Using default percentage: {default_percentage}%")
                except Exception as e:
                    print(f"[INFO] Error reading percentage: {e}. Using default: {default_percentage}%")

                # Execute the scraping script to extract comments
                logs = []
                page.on("console", lambda msg: logs.append(f"[BROWSER] {msg.type}: {msg.text}"))

                print(f"[INFO] Scraping comments for video {i+1}. This may take some time as we're aiming for {target_percentage}% of comments...")
                comments, total_comment_count_scraped, logs = await scrape_comments(page, i+1, folders["comments"], target_percentage)

                # Update total_comment_count if the scraped version is more accurate
                if total_comment_count_scraped > total_comment_count:
                    total_comment_count = total_comment_count_scraped
                    print(f"[INFO] Updated total comment count to {total_comment_count} based on scraper results")

                # Save diagnostic information
                await save_diagnostic_info(page, i+1, comments, total_comment_count, logs, folders["logs"])

                if not comments:
                    await save_error_logs(logs, i+1, folders["logs"])
                else:
                    percentage = 0
                    if total_comment_count > 0:
                        percentage = round((len(comments) / total_comment_count) * 100, 2)

                    print(f"[SUCCESS] Scraped {len(comments)} comments from video {i+1}")
                    if total_comment_count > 0:
                        print(f"[INFO] That's approximately {percentage}% of the total {total_comment_count} comments")

                # Wait a bit longer before moving to the next video to ensure everything is complete
                await asyncio.sleep(3)

                # Define selector for next video button
                button_selector = "button.TUXButton.TUXButton--capsule.TUXButton--medium.TUXButton--secondary.action-item.css-1egy55o:not([disabled])"

                # Use the helper function to click the next video button (based on vertical position)
                success = await click_next_video(page, button_selector)
                if not success:
                    print(f"[ERROR] Could not navigate to next video from video {i+1}.")
                    break

                await asyncio.sleep(5)

        if external_cdp:
            await page.close()
            print("[INFO] Disconnected from the dedicated social browser; browser remains running.")
        else:
            await browser.close()
            print("[INFO] Browser closed; process complete.")
    terminate_proc()

if __name__ == "__main__":
    # Check if the script was run with the --record flag
    record_mode = "--record" in sys.argv

    # Parse number of videos to scrape
    num_videos = 3  # Default reduced to 3 to prioritize quality over quantity
    default_percentage = 50  # Default percentage to scrape

    for i, arg in enumerate(sys.argv):
        if arg == "--videos" and i + 1 < len(sys.argv):
            try:
                num_videos = int(sys.argv[i + 1])
            except ValueError:
                pass
        elif arg == "--percentage" and i + 1 < len(sys.argv):
            try:
                default_percentage = int(sys.argv[i + 1])
                if default_percentage < 1 or default_percentage > 100:
                    print(f"Invalid percentage: {default_percentage}. Using default (50%).")
                    default_percentage = 50
            except ValueError:
                print(f"Invalid percentage format. Using default (50%).")

    # Determine target_url and max_videos for direct execution
    # These would normally come from run_scraper.py
    target_url_for_direct_run = None # Default to None when run directly
    max_videos_for_direct_run = 500  # Use the default from the main function signature

    if record_mode:
        print("\n" + "="*50)
        print("RUNNING IN RECORDING MODE")
        print("="*50)
        print("\nIn this mode, you will record key TikTok UI elements")
        print("to help the scraper navigate and collect data more effectively.")
        print("\nPress Ctrl+C now if you want to cancel.")
        print("Otherwise, the browser will launch in 5 seconds...")
        time.sleep(5)

    # Parse whether to use API
    use_api = "--no-api" not in sys.argv

    # Use the locally defined defaults for direct execution
    asyncio.run(main(record_mode=record_mode,
                     num_videos=num_videos,
                     default_percentage=default_percentage,
                     target_url=target_url_for_direct_run, # Use defined variable
                     max_videos=max_videos_for_direct_run, # Use defined variable
                     use_api=use_api,
                     reset_checkpoint=False))
