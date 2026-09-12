#!/usr/bin/env python
"""
TikTok Scraper - CLI Entry Point

This script provides a command-line interface to run the TikTok scraper.
It supports different modes of operation and various configuration options.
"""

import sys
import argparse
import asyncio
import json
import os
import re
from tiktok_scraper.main import main as run_scraper


def sanitize_search_keyword(value):
    """Remove quote operators that platform search treats as literal text."""
    keyword = (value or "").strip()
    keyword = keyword.replace('"', " ").replace("“", " ").replace("”", " ")
    keyword = keyword.replace("'", " ").replace("‘", " ").replace("’", " ")
    return re.sub(r"\s+", " ", keyword).strip()


def split_keywords(raw_value):
    """Split comma, semicolon, pipe, or newline separated keyword input."""
    keywords = []
    seen = set()
    for part in re.split(r"[\n,;|]+", raw_value or ""):
        keyword = sanitize_search_keyword(part)
        if keyword and keyword.lower() not in seen:
            seen.add(keyword.lower())
            keywords.append(keyword)
    return keywords


def load_keywords_from_file(path):
    with open(path, "r", encoding="utf-8-sig") as file:
        lines = [
            line.strip()
            for line in file
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return split_keywords("\n".join(lines))


def collect_keywords(args):
    keywords = []
    for keyword in split_keywords(args.keywords):
        if keyword.lower() not in {value.lower() for value in keywords}:
            keywords.append(keyword)
    if args.keyword_file:
        for keyword in load_keywords_from_file(args.keyword_file):
            if keyword.lower() not in {value.lower() for value in keywords}:
                keywords.append(keyword)
    return keywords


def load_candidate_file(path, platform):
    if not path:
        return []
    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        raw_candidates = payload
    elif isinstance(payload, dict):
        raw_candidates = payload.get("videos") or payload.get("candidates") or []
    else:
        raise ValueError("candidate file must contain a JSON list or an object with videos/candidates")

    candidates = []
    seen = set()
    from tiktok_scraper.known_content import candidate_content_keys

    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else raw
        candidate = dict(candidate)
        candidate_platform = str(candidate.get("platform") or raw.get("platform") or "").lower()
        if candidate_platform and platform != "all" and candidate_platform != platform:
            continue
        video_id = candidate.get("video_id") or candidate.get("id") or candidate.get("media_id")
        url = candidate.get("url") or candidate.get("video_url") or candidate.get("permalink")
        keys = candidate_content_keys(platform, candidate)
        key = str(keys[0] if keys else "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        candidate.setdefault("platform", platform)
        candidate.setdefault("video_id", video_id)
        candidate.setdefault("url", url)
        candidate.setdefault("title", candidate.get("caption") or "")
        candidate.setdefault("username", candidate.get("content_creator") or candidate.get("creator") or "")
        candidate.setdefault("discovery_method", "candidate_file")
        candidate.setdefault("metadata_method", "candidate_file")
        candidates.append(candidate)
    return candidates

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="TikTok Comment Scraper")

    # Core functionality options
    parser.add_argument("--url", type=str, help="URL of profile or video to scrape")
    parser.add_argument("--platform", type=str, choices=["tiktok", "youtube", "instagram", "facebook", "x", "all"], default="tiktok",
                        help="Platform to scrape (default: tiktok). Use 'all' for sequential multi-platform scrape.")
    parser.add_argument("--videos", type=int, default=0,
                        help="Maximum number of videos to scrape (default: 0 = unlimited)")
    parser.add_argument(
        "--discovery-videos",
        type=int,
        default=0,
        help="Maximum candidate metadata records to discover before date, relevance, and known-content gates (default: --videos; 0 = uncapped)",
    )
    parser.add_argument("--comments", type=int, default=0,
                        help="Maximum comments to scrape per video/post where supported (default: 0 = unlimited)")
    parser.add_argument("--percentage", type=int, default=50,
                        help="Percentage of comments to scrape per video (default: 50)")
    parser.add_argument("--auto-analyze", action="store_true",
                        help="Automatically run AI analysis on the scraped comments after scraping finishes")
    parser.add_argument("--session-name", type=str,
                        help="Use or resume a specific comments_data session folder name")
    parser.add_argument("--candidate-file", type=str,
                        help="JSON candidate batch to extract directly without running platform search")
    parser.add_argument("--relevance-profile", type=str,
                        help="JSON topic profile used to score candidates before scraping comments")
    parser.add_argument("--relevance-mode", choices=["off", "audit", "filter"], default="off",
                        help="Candidate relevance mode: off, audit only, or filter before comment scraping")
    parser.add_argument("--relevance-review-action", choices=["skip", "scrape"], default="skip",
                        help="What to do with borderline review candidates when relevance-mode=filter")
    parser.add_argument("--relevance-accept-threshold", type=int, default=0,
                        help="Override profile accept threshold")
    parser.add_argument("--relevance-review-threshold", type=int, default=0,
                        help="Override profile review threshold")
    parser.add_argument("--known-content-file", type=str,
                        help="JSON file of known content keys to skip before scraping comments")
    parser.add_argument("--date-start", type=str,
                        help="Optional ISO-8601 publication start date for standalone runs")
    parser.add_argument("--date-end", type=str,
                        help="Optional ISO-8601 publication end date for standalone runs")

    parser.add_argument("--tiktok-user-data-dir", type=str,
                        help="Edge/Chrome user-data directory containing the TikTok browser profile")
    parser.add_argument("--tiktok-profile-directory", type=str,
                        help="TikTok profile folder inside the user-data directory, e.g. Default or Profile 7")
    parser.add_argument("--tiktok-browser-channel", type=str,
                        help="Playwright browser channel for TikTok, e.g. chrome or msedge")
    parser.add_argument("--tiktok-cdp-url", type=str,
                        help="Attach to an already-running TikTok browser over CDP")
    parser.add_argument("--tiktok-cookie-curl", type=str,
                        help="DevTools Copy-as-cURL (cmd) file used to inject a TikTok login session")
    parser.add_argument("--tiktok-require-auth", action="store_true",
                        help="Fail before search when the selected TikTok browser session is not logged in")
    parser.add_argument("--tiktok-transcript-langs", type=str, default="",
                        help="Preferred TikTok subtitle languages, comma-separated (default: id,en)")
    parser.add_argument("--tiktok-transcript-timeout", type=float,
                        help="Seconds allowed for each TikTok subtitle metadata or track request")
    parser.add_argument("--tiktok-transcript-retries", type=int,
                        help="Retries for transient TikTok subtitle requests (default: 1)")
    parser.add_argument("--no-tiktok-transcripts", action="store_true",
                        help="Disable TikTok web-player subtitle collection")

    # API-related options
    parser.add_argument("--no-api", action="store_true",
                        help="Disable API-based scraping, use only browser-based method")
    parser.add_argument(
        "--transport-mode",
        choices=["api-first", "api-only", "hybrid", "dom"],
        default="api-first",
        help="Collection transport policy. api-only forbids DOM data extraction; browser bootstrapping may still provide authenticated API tokens.",
    )
    parser.add_argument("--source-key", default="", help="Stable discovery source key recorded in raw provenance")
    parser.add_argument("--source-kind", default="", help="Discovery source kind recorded in raw provenance")
    parser.add_argument("--source-value", default="", help="Discovery query/account target recorded in raw provenance")
    parser.add_argument("--source-label", default="", help="Human-readable discovery source label")
    parser.add_argument("--source-role", default="", help="Owned, talent, publisher, query, or other campaign role")
    parser.add_argument("--source-target-mode", default="", help="Source target mode, such as search or url")
    parser.add_argument("--source-taxonomy-json", default="", help="JSON account/source taxonomy recorded in raw provenance")
    parser.add_argument("--campaign-spec-digest", default="", help="Campaign specification digest recorded in raw provenance")
    parser.add_argument("--instagram-storage-state", type=str,
                        help="Path to a Playwright storage_state JSON file for an authenticated Instagram browser session")
    parser.add_argument("--instagram-user-data-dir", type=str,
                        help="Path to a persistent Playwright browser profile directory for Instagram login reuse")
    parser.add_argument("--instagram-profile-directory", type=str,
                        help="Chrome/Edge profile folder name inside the user data dir, e.g. Default or Profile 1")
    parser.add_argument("--instagram-browser-channel", type=str,
                        help="Playwright browser channel for existing profiles, e.g. chrome or msedge")
    parser.add_argument("--instagram-cdp-url", type=str,
                        help="Attach to an already-running Instagram browser over CDP, e.g. http://127.0.0.1:9223")
    parser.add_argument("--instagram-headed", action="store_true",
                        help="Run Instagram browser visibly, useful for manual login/debugging")
    parser.add_argument(
        "--youtube-transcript-method",
        choices=["api", "auto", "innertube", "timedtext", "watch"],
        default="api",
        help="YouTube transcript transport: api=maintained transcript client (recommended), auto=guarded fallbacks, innertube=panel API, timedtext/watch=signed caption URL",
    )
    parser.add_argument("--youtube-transcript-langs", type=str, default="",
                        help="Preferred YouTube transcript languages, comma-separated, e.g. id,en")
    parser.add_argument("--youtube-transcript-delay", type=float,
                        help="Minimum seconds between starting YouTube transcript requests (default: 0.75)")
    parser.add_argument("--youtube-transcript-retries", type=int,
                        help="Retries for transient YouTube transcript request failures (default: 1)")
    parser.add_argument("--youtube-transcript-cooldown", type=float,
                        help="Seconds to pause transcript requests after blocking/rate limiting while comments continue (default: 900)")
    parser.add_argument("--no-youtube-transcripts", action="store_true",
                        help="Disable YouTube transcript scraping")
    parser.add_argument("--facebook-storage-state", type=str,
                        help="Path to a Playwright storage_state JSON file for an authenticated Facebook browser session")
    parser.add_argument("--facebook-user-data-dir", type=str,
                        help="Path to a persistent Playwright browser profile directory for Facebook login reuse")
    parser.add_argument("--facebook-profile-directory", type=str,
                        help="Chrome/Edge profile folder name inside the user data dir, e.g. Default or Profile 1")
    parser.add_argument("--facebook-browser-channel", type=str,
                        help="Playwright browser channel for existing profiles, e.g. chrome or msedge")
    parser.add_argument("--facebook-cdp-url", type=str,
                        help="Attach to an already-running Facebook browser over CDP, e.g. http://127.0.0.1:9223")
    parser.add_argument("--facebook-headed", action="store_true",
                        help="Run Facebook browser visibly, useful for manual login/debugging")
    parser.add_argument("--facebook-extraction-mode", choices=["hybrid", "browser-api", "dom"], default="browser-api",
                        help="Facebook comment extraction mode: hybrid=network+DOM, browser-api=network GraphQL only, dom=DOM/basic only")
    parser.add_argument("--x-extraction-mode", choices=["browser-api", "official"], default="browser-api",
                        help="X transport: browser-api captures authenticated web GraphQL JSON without paid API access; official uses API v2")
    parser.add_argument("--x-storage-state", type=str,
                        help="Path to a Playwright storage_state JSON file for an authenticated X session")
    parser.add_argument("--x-user-data-dir", type=str,
                        help="Path to a dedicated persistent browser profile for X login reuse")
    parser.add_argument("--x-profile-directory", type=str,
                        help="Chrome/Edge profile folder inside --x-user-data-dir, e.g. Default or Profile 7")
    parser.add_argument("--x-browser-channel", type=str, default="msedge",
                        help="Playwright browser channel for X browser-api mode (default: msedge)")
    parser.add_argument("--x-cdp-url", type=str,
                        help="Attach to an already-running X browser over CDP, e.g. http://127.0.0.1:9223")
    parser.add_argument("--x-headed", action="store_true",
                        help="Run the X browser visibly for login or transport diagnostics")
    parser.add_argument("--x-search-rounds", type=int, default=0,
                        help="Maximum X search scroll rounds in browser-api mode (0 = adaptive default)")
    parser.add_argument("--x-stall-rounds", type=int, default=0,
                        help="Stop X search after this many rounds without new GraphQL posts (0 = adaptive default)")
    parser.add_argument("--x-comment-rounds", type=int, default=0,
                        help="Maximum X conversation scroll rounds in browser-api mode (0 = adaptive default)")
    parser.add_argument("--x-comment-stall-rounds", type=int, default=0,
                        help="Stop an X conversation after this many rounds without new replies (0 = adaptive default)")
    parser.add_argument("--x-bearer-token-file", type=str,
                        help="File containing an official X API v2 bearer token, used only with --x-extraction-mode official")
    parser.add_argument("--x-search-mode", choices=["recent", "all"], default="recent",
                        help="X API search scope: recent=last 7 days, all=full archive with eligible paid access")
    parser.add_argument("--x-since-id", type=str, default="",
                        help="Only discover X posts newer than this post ID")
    parser.add_argument("--x-api-read-limit", type=int, default=1000,
                        help="Maximum X post resources returned in this worker run (default: 1000; 0 = unlimited)")
    parser.add_argument("--x-max-rate-limit-wait", type=float, default=60.0,
                        help="Maximum seconds to wait automatically for an X API rate-limit reset")

    # Music Matcher options
    parser.add_argument("--match-music", action="store_true",
                        help="Run the comment-to-music matcher pipeline after scraping finishes")

    # Mode options
    parser.add_argument("--search", type=str,
                        help="Target the search page with a specific query")
    parser.add_argument("--keywords", type=str,
                        help="Instagram multi-keyword search; separate terms with comma, semicolon, pipe, or newline")
    parser.add_argument("--keyword-file", type=str,
                        help="Instagram multi-keyword search file, one keyword per line; lines starting with # are ignored")
    parser.add_argument("--per-keyword-videos", type=int, default=0,
                        help="Instagram multi-keyword search results per keyword before dedupe (default: --videos; 0 = uncapped discovery)")
    parser.add_argument("--instagram-search-rounds", type=int, default=0,
                        help="Maximum Instagram search scroll rounds per keyword (default scraper setting; raise for broader coverage)")
    parser.add_argument("--instagram-stall-rounds", type=int, default=0,
                        help="Stop Instagram search after this many scroll rounds without new posts (default scraper setting)")
    parser.add_argument("--instagram-discovery-only", action="store_true",
                        help="Only discover Instagram posts and write instagram_comments.json without scraping comments")
    parser.add_argument("--facebook-search-rounds", type=int, default=0,
                        help="Maximum Facebook search/profile scroll rounds (default scraper setting; raise for broader coverage)")
    parser.add_argument("--facebook-stall-rounds", type=int, default=0,
                        help="Stop Facebook search after this many scroll rounds without new posts (default scraper setting)")
    parser.add_argument("--trending", action="store_true",
                        help="Target the Explore/Trending page instead of a specific profile")
    parser.add_argument("--record", action="store_true",
                        help="Run in recording mode to capture UI elements")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug mode with more verbose logging")

    # Checkpointing options
    parser.add_argument("--reset-checkpoint", action="store_true",
                        help="Reset the checkpoint file to start a fresh scrape")

    # AI Analysis
    parser.add_argument("--analyze", type=str, metavar="FILE_PATH",
                        help="Run AI analysis pipeline on a given comments JSON file")

    return parser.parse_args()

def main():
    """Main entry point for the CLI."""
    args = parse_arguments()
    if args.no_api:
        args.transport_mode = "dom"
    if args.platform == "x" and args.transport_mode == "dom":
        raise ValueError("X requires API-derived JSON; use browser-api or official mode with api-first/api-only transport")
    os.environ["SCRAPER_TRANSPORT_MODE"] = args.transport_mode
    if args.date_start or args.date_end:
        os.environ["SCRAPER_DATE_WINDOWS_JSON"] = json.dumps([{
            "name": "cli_window",
            "start": args.date_start or "",
            "end": args.date_end or "",
        }])
    if args.tiktok_cdp_url and args.tiktok_user_data_dir:
        raise ValueError("Use either --tiktok-cdp-url or --tiktok-user-data-dir, not both")
    if args.instagram_cdp_url and args.instagram_user_data_dir:
        raise ValueError("Use either --instagram-cdp-url or --instagram-user-data-dir, not both")
    if args.facebook_cdp_url and args.facebook_user_data_dir:
        raise ValueError("Use either --facebook-cdp-url or --facebook-user-data-dir, not both")
    if args.x_cdp_url and args.x_user_data_dir:
        raise ValueError("Use either --x-cdp-url or --x-user-data-dir, not both")
    if args.tiktok_user_data_dir:
        os.environ["TIKTOK_PLAYWRIGHT_USER_DATA_DIR"] = args.tiktok_user_data_dir
        os.environ["TIKTOK_FORCE_PLAYWRIGHT"] = "1"
        os.environ["TIKTOK_PROFILE_STRICT"] = "1"
    if args.tiktok_profile_directory:
        os.environ["TIKTOK_PROFILE_DIRECTORY"] = args.tiktok_profile_directory
        os.environ["TIKTOK_PROFILE_STRICT"] = "1"
    if args.tiktok_browser_channel:
        os.environ["TIKTOK_BROWSER_CHANNEL"] = args.tiktok_browser_channel
    if args.tiktok_cdp_url:
        os.environ["TIKTOK_CDP_URL"] = args.tiktok_cdp_url
        os.environ["TIKTOK_FORCE_PLAYWRIGHT"] = "0"
        os.environ["TIKTOK_PROFILE_STRICT"] = "1"
    if args.tiktok_cookie_curl:
        os.environ["TIKTOK_COOKIE_CURL_FILE"] = args.tiktok_cookie_curl
    if args.tiktok_require_auth:
        os.environ["TIKTOK_REQUIRE_AUTH"] = "1"
    if args.tiktok_transcript_langs:
        os.environ["TIKTOK_TRANSCRIPT_LANGS"] = args.tiktok_transcript_langs
    if args.tiktok_transcript_timeout is not None:
        os.environ["TIKTOK_TRANSCRIPT_TIMEOUT_SECONDS"] = str(
            max(1.0, args.tiktok_transcript_timeout)
        )
    if args.tiktok_transcript_retries is not None:
        os.environ["TIKTOK_TRANSCRIPT_RETRIES"] = str(
            max(0, args.tiktok_transcript_retries)
        )
    if args.no_tiktok_transcripts:
        os.environ["TIKTOK_SCRAPE_TRANSCRIPTS"] = "0"
    candidate_videos = load_candidate_file(args.candidate_file, args.platform) if args.candidate_file else []
    candidate_mode = bool(args.candidate_file)
    if candidate_mode and not candidate_videos:
        raise ValueError(f"No {args.platform} candidates found in {args.candidate_file}")
    instagram_keywords = collect_keywords(args)
    instagram_keyword_mode = bool(instagram_keywords) and args.platform in {"instagram", "all"}
    if args.instagram_storage_state:
        os.environ["INSTAGRAM_STORAGE_STATE"] = args.instagram_storage_state
    if args.instagram_user_data_dir:
        os.environ["INSTAGRAM_USER_DATA_DIR"] = args.instagram_user_data_dir
    if args.instagram_profile_directory:
        os.environ["INSTAGRAM_PROFILE_DIRECTORY"] = args.instagram_profile_directory
    if args.instagram_browser_channel:
        os.environ["INSTAGRAM_BROWSER_CHANNEL"] = args.instagram_browser_channel
    if args.instagram_cdp_url:
        os.environ["INSTAGRAM_CDP_URL"] = args.instagram_cdp_url
    if args.instagram_headed:
        os.environ["INSTAGRAM_HEADLESS"] = "0"
    if args.youtube_transcript_method:
        method = args.youtube_transcript_method
        os.environ["YOUTUBE_TRANSCRIPT_ENDPOINT_METHOD"] = method
        if method in {"watch", "timedtext"}:
            os.environ["YOUTUBE_TRANSCRIPT_PLAYER_METHOD"] = "watch"
        elif method == "auto":
            os.environ["YOUTUBE_TRANSCRIPT_PLAYER_METHOD"] = "auto"
        else:
            os.environ["YOUTUBE_TRANSCRIPT_PLAYER_METHOD"] = "api"
    if args.youtube_transcript_langs:
        os.environ["YOUTUBE_TRANSCRIPT_LANGS"] = args.youtube_transcript_langs
    if args.youtube_transcript_delay is not None:
        os.environ["YOUTUBE_TRANSCRIPT_DELAY_SECONDS"] = str(max(0.0, args.youtube_transcript_delay))
    if args.youtube_transcript_retries is not None:
        os.environ["YOUTUBE_TRANSCRIPT_RETRIES"] = str(max(0, args.youtube_transcript_retries))
    if args.youtube_transcript_cooldown is not None:
        os.environ["YOUTUBE_TRANSCRIPT_COOLDOWN_SECONDS"] = str(max(0.0, args.youtube_transcript_cooldown))
    if args.no_youtube_transcripts:
        os.environ["YOUTUBE_SCRAPE_TRANSCRIPTS"] = "0"
    if args.instagram_search_rounds > 0:
        os.environ["INSTAGRAM_SEARCH_SCROLL_ROUNDS"] = str(args.instagram_search_rounds)
    if args.instagram_stall_rounds > 0:
        os.environ["INSTAGRAM_SEARCH_STALL_ROUNDS"] = str(args.instagram_stall_rounds)
    if args.facebook_storage_state:
        os.environ["FACEBOOK_STORAGE_STATE"] = args.facebook_storage_state
    if args.facebook_user_data_dir:
        os.environ["FACEBOOK_USER_DATA_DIR"] = args.facebook_user_data_dir
    if args.facebook_profile_directory:
        os.environ["FACEBOOK_PROFILE_DIRECTORY"] = args.facebook_profile_directory
    if args.facebook_browser_channel:
        os.environ["FACEBOOK_BROWSER_CHANNEL"] = args.facebook_browser_channel
    if args.facebook_cdp_url:
        os.environ["FACEBOOK_CDP_URL"] = args.facebook_cdp_url
    if args.facebook_headed:
        os.environ["FACEBOOK_HEADLESS"] = "0"
    if args.facebook_extraction_mode:
        os.environ["FACEBOOK_EXTRACTION_MODE"] = args.facebook_extraction_mode
    if args.transport_mode == "api-only":
        os.environ["INSTAGRAM_API_FIRST"] = "1"
        os.environ["INSTAGRAM_DIRECT_ONLY"] = "1"
        os.environ["FACEBOOK_EXTRACTION_MODE"] = "browser-api"
    elif args.transport_mode == "dom":
        os.environ["FACEBOOK_EXTRACTION_MODE"] = "dom"
    if args.facebook_search_rounds > 0:
        os.environ["FACEBOOK_SEARCH_SCROLL_ROUNDS"] = str(args.facebook_search_rounds)
    if args.facebook_stall_rounds > 0:
        os.environ["FACEBOOK_SEARCH_STALL_ROUNDS"] = str(args.facebook_stall_rounds)
    if args.x_bearer_token_file:
        os.environ["X_BEARER_TOKEN_FILE"] = args.x_bearer_token_file
    if args.x_storage_state:
        os.environ["X_STORAGE_STATE"] = args.x_storage_state
    if args.x_user_data_dir:
        os.environ["X_USER_DATA_DIR"] = args.x_user_data_dir
    if args.x_profile_directory:
        os.environ["X_PROFILE_DIRECTORY"] = args.x_profile_directory
    if args.x_browser_channel:
        os.environ["X_BROWSER_CHANNEL"] = args.x_browser_channel
    if args.x_cdp_url:
        os.environ["X_CDP_URL"] = args.x_cdp_url
    if args.x_headed:
        os.environ["X_HEADLESS"] = "0"
    if args.x_search_rounds > 0:
        os.environ["X_SEARCH_SCROLL_ROUNDS"] = str(args.x_search_rounds)
    if args.x_stall_rounds > 0:
        os.environ["X_SEARCH_STALL_ROUNDS"] = str(args.x_stall_rounds)
    if args.x_comment_rounds > 0:
        os.environ["X_COMMENT_SCROLL_ROUNDS"] = str(args.x_comment_rounds)
    if args.x_comment_stall_rounds > 0:
        os.environ["X_COMMENT_STALL_ROUNDS"] = str(args.x_comment_stall_rounds)
    if args.x_search_mode:
        os.environ["X_SEARCH_MODE"] = args.x_search_mode
    if args.x_api_read_limit > 0:
        os.environ["X_API_READ_LIMIT"] = str(args.x_api_read_limit)
    if args.x_max_rate_limit_wait is not None:
        os.environ["X_MAX_RATE_LIMIT_WAIT_SECONDS"] = str(max(0.0, args.x_max_rate_limit_wait))

    # Check if analysis mode is requested
    if args.analyze:
        print(f"[INFO] Running AI Analysis on: {args.analyze}")
        try:
            # Infer session directory from the json path
            # e.g., comments_data/explore_20260628/comments/video_2.json -> comments_data/explore_20260628
            comments_dir = os.path.dirname(args.analyze)
            session_dir = os.path.dirname(comments_dir)
            output_dir = os.path.join(session_dir, "analysis_reports")

            from tiktok_scraper.ai_analytics import run_analysis_pipeline
            run_analysis_pipeline(args.analyze, output_dir=output_dir)
        except ImportError as e:
            print(f"[ERROR] Could not import ai_analytics: {e}")
        return

    # If no URL is provided and not trending/search, prompt the user
    url = args.url
    if args.search:
        args.search = sanitize_search_keyword(args.search)
        if args.platform == 'tiktok':
            import urllib.parse
            encoded_query = urllib.parse.quote(args.search)
            url = f"https://www.tiktok.com/search/video?q={encoded_query}"
        else:
            url = args.search
    elif args.trending:
        url = "https://www.tiktok.com/explore"
    elif not url and not instagram_keyword_mode and not candidate_mode:
        url = input("Enter URL or search query to scrape: ")
        if not url:
            print("No input provided. Exiting.")
            sys.exit(1)
    target_display = (
        f"candidate batch: {args.candidate_file}"
        if candidate_mode
        else url or (" | ".join(instagram_keywords) if instagram_keyword_mode else "")
    )

    # Set maximum videos based on input
    max_videos = args.videos
    if max_videos <= 0:
        max_videos = float('inf')  # Set to infinity for unlimited scraping
        videos_display = "unlimited"
    else:
        videos_display = str(max_videos)
    discovery_video_limit = args.discovery_videos if args.discovery_videos > 0 else args.videos
    discovery_display = str(discovery_video_limit) if discovery_video_limit > 0 else "unlimited"

    # Run the scraper with the provided arguments
    print("\n" + "="*60)
    print(" Social Media Comment Scraper ".center(60, "="))
    print("="*60)

    print(f"\nTarget Platform: {args.platform}")
    print(f"Target: {target_display}")
    print(f"Maximum discovery candidates: {discovery_display}")
    print(f"Maximum videos to process: {videos_display}")

    print("\nStarting scraper...")
    print("="*60 + "\n")

    import datetime
    import urllib.parse
    import re
    import glob

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_query = "query"
    if candidate_mode:
        safe_query = "candidate_batch"
    elif instagram_keyword_mode:
        safe_query = "multi_" + "_".join("".join(c if c.isalnum() else "_" for c in keyword) for keyword in instagram_keywords[:2])
        safe_query = safe_query[:30]
    elif args.search:
        safe_query = "".join(c if c.isalnum() else "_" for c in args.search)[:20]
    elif url:
        safe_query = "".join(c if c.isalnum() else "_" for c in url)[:20]

    # Define session name based on platform
    if args.session_name:
        session_name = args.session_name
    elif args.platform == 'all':
        session_name = f"multiplatform_{safe_query}_{timestamp}"
    else:
        session_name = f"{args.platform}_{safe_query}_{timestamp}"

    try:
        source_taxonomy = json.loads(args.source_taxonomy_json) if args.source_taxonomy_json else {}
    except (TypeError, ValueError):
        raise ValueError("--source-taxonomy-json must contain a JSON object")
    if not isinstance(source_taxonomy, dict):
        raise ValueError("--source-taxonomy-json must contain a JSON object")
    source_context = {
        "source_key": args.source_key,
        "kind": args.source_kind,
        "value": args.source_value or target_display,
        "label": args.source_label or args.source_value or target_display,
        "role": args.source_role,
        "target_mode": args.source_target_mode,
        "taxonomy": source_taxonomy,
    }
    collection_context = {
        "posts_per_source": args.videos if args.videos > 0 else 0,
        "discovery_candidates_per_source": discovery_video_limit if discovery_video_limit > 0 else 0,
        "comments_per_post": args.comments if args.comments > 0 else 0,
        "transport_mode": args.transport_mode,
    }
    run_context = {
        "run_id": session_name,
        "platform": args.platform,
        "target": target_display,
        "campaign_spec_digest": args.campaign_spec_digest,
        "transport_mode": args.transport_mode,
    }
    os.environ["SCRAPER_SOURCE_CONTEXT_JSON"] = json.dumps(source_context, ensure_ascii=False)
    os.environ["SCRAPER_COLLECTION_CONTEXT_JSON"] = json.dumps(collection_context, ensure_ascii=False)
    os.environ["SCRAPER_RUN_CONTEXT_JSON"] = json.dumps(run_context, ensure_ascii=False)

    from tiktok_scraper.raw_contract import finalize_content_record, finalize_payload

    def finalize_video_record(record, platform):
        return finalize_content_record(
            record,
            platform=platform,
            source_context=source_context,
            collection_context=collection_context,
        )

    def finalize_raw_payload(payload, platform):
        return finalize_payload(
            payload,
            platform=platform,
            source_context=source_context,
            collection_context=collection_context,
            run_context={**run_context, "platform": platform},
        )

    relevance_profile = None
    if args.relevance_mode != "off" or args.relevance_profile:
        from tiktok_scraper.relevance import load_topic_profile

        profile_keywords = instagram_keywords or split_keywords(args.search or target_display)
        relevance_profile = load_topic_profile(
            args.relevance_profile or "",
            topic=args.search or target_display,
            keywords=profile_keywords,
        )
        if args.relevance_accept_threshold > 0:
            relevance_profile["accept_threshold"] = args.relevance_accept_threshold
        if args.relevance_review_threshold > 0:
            relevance_profile["review_threshold"] = args.relevance_review_threshold

    from tiktok_scraper.date_filter import filter_candidates_by_date, windows_from_env, write_date_audit

    date_windows = windows_from_env()

    def annotate_discovery_source(candidates, source_value):
        source_value = str(source_value or "").strip()
        if not source_value:
            return candidates
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            matched = candidate.get("matched_keywords")
            if isinstance(matched, str):
                matched = split_keywords(matched)
            elif not isinstance(matched, list):
                matched = []
            if source_value.casefold() not in {str(value).casefold() for value in matched}:
                matched.append(source_value)
            candidate["matched_keywords"] = matched
        return candidates

    def apply_date_gate(candidates, platform, folders):
        if not date_windows:
            return candidates
        kept, audit = filter_candidates_by_date(candidates, date_windows)
        audit_path = write_date_audit(folders["logs"], platform, audit, date_windows)
        summary = {"within_date": 0, "outside_date": 0, "unknown_date": 0}
        for item in audit:
            summary[item["decision"]] += 1
        print(
            f"[INFO] Date-window {platform}: within={summary['within_date']} "
            f"outside={summary['outside_date']} unknown={summary['unknown_date']}; "
            f"scrape={len(kept)} audit={audit_path}",
            flush=True,
        )
        return kept

    def apply_relevance_gate(candidates, platform, folders):
        if args.relevance_mode == "off" or not relevance_profile:
            return candidates
        from tiktok_scraper.relevance import filter_candidates, write_relevance_audit

        accepted, review, rejected, scored = filter_candidates(
            candidates,
            relevance_profile,
            platform=platform,
            mode=args.relevance_mode,
            review_action=args.relevance_review_action,
        )
        audit_path = write_relevance_audit(folders["logs"], platform, scored, relevance_profile)
        print(
            f"[INFO] Relevance {platform}: accepted={len(accepted)} "
            f"review={len(review)} rejected={len(rejected)} mode={args.relevance_mode}; "
            f"audit={audit_path}",
            flush=True,
        )
        return accepted

    known_content_keys = set()
    known_content_meta = {}
    if args.known_content_file:
        from tiktok_scraper.known_content import load_known_content_file

        known_content_keys, known_content_meta = load_known_content_file(args.known_content_file)

    def apply_known_content_gate(candidates, platform, folders):
        if not known_content_keys:
            return candidates
        from tiktok_scraper.known_content import filter_new_candidates, write_known_content_audit

        fresh, skipped, audit = filter_new_candidates(candidates, platform, known_content_keys)
        audit_path = write_known_content_audit(folders["logs"], platform, audit, known_content_meta)
        print(
            f"[INFO] Known-content {platform}: scrape_new={len(fresh)} "
            f"skip_known={len(skipped)} baseline={known_content_meta.get('last_successful_scrape_at', '')}; "
            f"audit={audit_path}",
            flush=True,
        )
        return fresh

    def apply_discovery_cap(candidates, platform):
        if discovery_video_limit <= 0:
            return candidates
        kept = candidates[:discovery_video_limit]
        if len(candidates) > len(kept):
            print(
                f"[INFO] Discovery cap {platform}: observed={len(candidates)} "
                f"retained={len(kept)} limit={discovery_video_limit}",
                flush=True,
            )
        return kept

    def apply_extraction_cap(candidates, platform):
        if args.videos <= 0:
            return candidates
        kept = candidates[:args.videos]
        if len(candidates) > len(kept):
            print(
                f"[INFO] Extraction cap {platform}: eligible={len(candidates)} "
                f"scrape={len(kept)} limit={args.videos}",
                flush=True,
            )
        return kept

    async def run_yt(session_override=None):
        from tiktok_scraper.scrapers.youtube_scraper import YouTubeScraper
        from tiktok_scraper.utils.file_utils import create_session_folders
        from tiktok_scraper.main import save_comments_to_file

        scraper = YouTubeScraper()
        filename_prefix = "youtube_" if args.platform == "all" else ""
        # If the user passed a URL that is a youtube watch URL, just do that one video
        direct_youtube_url = bool(url and ("youtube.com/watch" in url or "youtu.be" in url or "youtube.com/shorts/" in url))
        if candidate_mode:
            videos = [dict(candidate) for candidate in candidate_videos]
        elif direct_youtube_url:
            videos = [{
                "url": url,
                "video_id": scraper.extract_video_id(url),
                "title": "Direct URL",
                "username": "YouTube Channel",
                "discovery_method": "direct_url",
            }]
        else:
            videos = await scraper.search(
                url or args.search,
                max_videos=discovery_video_limit if discovery_video_limit > 0 else 0,
            )
            videos = annotate_discovery_source(videos, url or args.search)
        videos = apply_discovery_cap(videos, "youtube")

        folders = create_session_folders(session_name=session_override)
        videos = apply_date_gate(videos, "youtube", folders)
        if not direct_youtube_url:
            videos = apply_relevance_gate(videos, "youtube", folders)
            videos = apply_known_content_gate(videos, "youtube", folders)
        videos = apply_extraction_cap(videos, "youtube")

        try:
            concurrency = max(1, int(os.environ.get("YOUTUBE_CONCURRENCY", "2")))
        except ValueError:
            concurrency = 2
        try:
            per_video_timeout = max(0.0, float(os.environ.get("YOUTUBE_PER_VIDEO_TIMEOUT", "300")))
        except ValueError:
            print("[WARN] Ignoring invalid YOUTUBE_PER_VIDEO_TIMEOUT; using 300s", flush=True)
            per_video_timeout = 300.0
        semaphore = asyncio.Semaphore(concurrency)
        combined_videos = [None] * len(videos)

        async def fetch_youtube_video(i, v):
            async with semaphore:
                scrape_coro = scraper.extract_comments(v, max_comments=args.comments if args.comments > 0 else 0)
                try:
                    if per_video_timeout:
                        res = await asyncio.wait_for(scrape_coro, timeout=per_video_timeout)
                    else:
                        res = await scrape_coro
                except asyncio.TimeoutError:
                    video_label = v.get("url") or v.get("video_id") or v.get("title") or f"video {i + 1}"
                    print(
                        f"[WARN] YouTube video {i + 1}/{len(videos)} timed out after {per_video_timeout:g}s: {video_label}",
                        flush=True,
                    )
                    res = {
                        "video_id": v.get("video_id") or scraper.extract_video_id(v.get("url", "")),
                        "url": v.get("url"),
                        "caption": v.get("caption") or v.get("title") or "",
                        "title": v.get("title") or v.get("caption") or "",
                        "description": v.get("description", ""),
                        "published": v.get("published", ""),
                        "views": v.get("views", ""),
                        "username": v.get("username") or v.get("creator") or "Unknown Creator",
                        "comments": [],
                        "comment_error": f"youtube_comment_timeout_after_{per_video_timeout:g}s",
                        "comment_method": "youtube_internal_api_timeout",
                        "metadata_method": v.get("metadata_method") or "not_collected_timeout",
                        "discovery_method": v.get("discovery_method") or "youtube_innertube_api",
                    }
                video_metadata = {
                    "title": res.get("title") or v.get("title"),
                    "description": res.get("description") or v.get("description", ""),
                    "published": res.get("published") or v.get("published", ""),
                    "views": res.get("views") or v.get("views", ""),
                    "content_type": res.get("content_type") or v.get("content_type") or v.get("youtube_renderer", ""),
                    "transcript": res.get("transcript", ""),
                    "transcript_available": bool(res.get("transcript_available")),
                    "transcript_language": res.get("transcript_language", ""),
                    "transcript_language_name": res.get("transcript_language_name", ""),
                    "transcript_is_auto_generated": bool(res.get("transcript_is_auto_generated")),
                    "transcript_segment_count": res.get("transcript_segment_count", 0),
                    "transcript_error": res.get("transcript_error", ""),
                    "transcript_status": res.get("transcript_status", ""),
                    "transcript_source": res.get("transcript_source", ""),
                    "transcript_attempt_count": res.get("transcript_attempt_count", 0),
                    "metadata_error": res.get("metadata_error", ""),
                    "comment_error": res.get("comment_error", ""),
                    "creator_id": res.get("creator_id") or v.get("creator_id") or "",
                    "duration_seconds": res.get("duration_seconds") or v.get("duration_seconds") or 0,
                    "thumbnail_url": res.get("thumbnail_url") or v.get("thumbnail_url") or "",
                    "content_language": res.get("content_language") or v.get("content_language") or "",
                    "keywords": res.get("keywords") or v.get("keywords") or [],
                    "category": res.get("category") or v.get("category") or "",
                    "is_live_content": res.get("is_live_content") if res.get("is_live_content") is not None else v.get("is_live_content"),
                    "discovery_method": res.get("discovery_method") or v.get("discovery_method") or "youtube_innertube_api",
                    "metadata_method": res.get("metadata_method") or "youtube_watch_player_api",
                    "comment_method": res.get("comment_method") or "youtube_internal_api",
                    "transcript_method": res.get("transcript_source", "") or "not_available",
                }
                await save_comments_to_file(
                    res["comments"],
                    i + 1,
                    folders["comments"],
                    video_id=res.get("video_id"),
                    caption=res.get("caption"),
                    username=res.get("username"),
                    extra_metadata=video_metadata,
                    filename_prefix=filename_prefix,
                    platform="youtube",
                )

                return i, finalize_video_record({
                    "video_number": i + 1,
                    "video_id": res.get("video_id"),
                    "url": res.get("url") or v.get("url"),
                    "caption": res.get("caption"),
                    "content_creator": res.get("username", "Unknown Creator"),
                    "comment_count": len(res["comments"]),
                    "comments": res["comments"],
                    "platform": "youtube",
                    **video_metadata,
                }, "youtube")

        completed = await asyncio.gather(*(fetch_youtube_video(i, v) for i, v in enumerate(videos)))
        for i, video_obj in completed:
            combined_videos[i] = video_obj
        combined_videos = [video for video in combined_videos if video is not None]

        combined_json_path = os.path.join(folders["comments"], "youtube_comments.json")
        with open(combined_json_path, "w", encoding="utf-8") as f:
            json.dump(
                finalize_raw_payload({"videos": combined_videos}, "youtube"),
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )

        total_comments = sum(len(v["comments"]) for v in combined_videos)
        print(f"\nSuccess! Scraped {total_comments} total YouTube comments from {len(videos)} videos.")
        return folders

    async def run_ig(session_override=None):
        from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper
        from tiktok_scraper.utils.file_utils import create_session_folders

        scraper = InstagramScraper()
        target = url or args.search or ""
        ig_discovery_videos = discovery_video_limit if discovery_video_limit > 0 else 0
        keyword_terms = instagram_keywords
        playwright = browser = context = None

        def env_timeout(name, default_seconds):
            value = os.environ.get(name, "").strip()
            if not value:
                return default_seconds
            try:
                return max(0.0, float(value))
            except ValueError:
                print(f"[WARN] Ignoring invalid {name}={value!r}", flush=True)
                return default_seconds

        async def with_timeout(coro, seconds, label):
            if not seconds:
                return await coro
            try:
                return await asyncio.wait_for(coro, timeout=seconds)
            except asyncio.TimeoutError:
                raise TimeoutError(f"{label} timed out after {seconds:g}s")

        def merge_keyword_results(results_by_keyword):
            ordered_posts = {}
            max_found = max((len(posts) for _, posts in results_by_keyword), default=0)

            for index in range(max_found):
                for keyword, posts in results_by_keyword:
                    if index >= len(posts):
                        continue

                    post = dict(posts[index])
                    video_id = post.get("video_id") or scraper.extract_shortcode(post.get("url", ""))
                    if not video_id:
                        continue
                    post["video_id"] = video_id

                    if video_id not in ordered_posts:
                        post["matched_keywords"] = [keyword]
                        ordered_posts[video_id] = post
                        continue

                    existing = ordered_posts[video_id]
                    if keyword not in existing["matched_keywords"]:
                        existing["matched_keywords"].append(keyword)
                    for field in ("url", "title", "username"):
                        if not existing.get(field) and post.get(field):
                            existing[field] = post[field]

            return list(ordered_posts.values())

        try:
            browser_timeout = env_timeout("INSTAGRAM_BROWSER_TIMEOUT_SECONDS", 120)
            search_timeout = env_timeout("INSTAGRAM_SEARCH_TIMEOUT_SECONDS", 120)
            post_timeout = env_timeout("INSTAGRAM_POST_TIMEOUT_SECONDS", 0)
            playwright, browser, context = await with_timeout(
                scraper._new_context(),
                browser_timeout,
                "Instagram browser startup",
            )

            direct_instagram_post = bool(target and scraper.is_instagram_url(target) and scraper.is_post_url(target))
            if candidate_mode:
                videos = [dict(candidate) for candidate in candidate_videos]
            elif keyword_terms:
                per_keyword_limit = args.per_keyword_videos if args.per_keyword_videos > 0 else ig_discovery_videos
                results_by_keyword = []
                for index, keyword in enumerate(keyword_terms, start=1):
                    print(f"[INFO] Instagram keyword {index}/{len(keyword_terms)}: {keyword}", flush=True)
                    try:
                        found = await with_timeout(
                            scraper.search(keyword, max_videos=per_keyword_limit, context=context),
                            search_timeout,
                            f"Instagram search for {keyword!r}",
                        )
                    except TimeoutError as e:
                        print(f"[WARN] {e}; continuing with next keyword", flush=True)
                        found = []
                    print(f"[INFO]   Found {len(found)} posts before dedupe", flush=True)
                    results_by_keyword.append((keyword, found))

                videos = merge_keyword_results(results_by_keyword)
                discovered_count = len(videos)
                print(
                    f"[INFO] Instagram multi-keyword discovery: "
                    f"{discovered_count} unique candidate posts"
                    ,
                    flush=True,
                )
            elif direct_instagram_post:
                normalized_url = scraper._normalize_url(target)
                videos = [{
                    "url": normalized_url,
                    "video_id": scraper.extract_shortcode(normalized_url),
                    "title": "",
                    "username": "",
                    "discovery_method": "direct_url",
                }]
            else:
                try:
                    videos = await with_timeout(
                        scraper.search(target, max_videos=ig_discovery_videos, context=context),
                        search_timeout,
                        f"Instagram search for {target!r}",
                    )
                except TimeoutError as e:
                    print(f"[WARN] {e}; no Instagram posts discovered", flush=True)
                    videos = []
                videos = annotate_discovery_source(videos, target)

            videos = apply_discovery_cap(videos, "instagram")
            folders = create_session_folders(session_name=session_override)
            videos = apply_date_gate(videos, "instagram", folders)
            if not direct_instagram_post:
                videos = apply_relevance_gate(videos, "instagram", folders)
                videos = apply_known_content_gate(videos, "instagram", folders)
            videos = apply_extraction_cap(videos, "instagram")
            default_concurrency = "1" if os.environ.get("INSTAGRAM_USER_DATA_DIR") or os.environ.get("IG_USER_DATA_DIR") else "2"
            concurrency = max(1, int(os.environ.get("INSTAGRAM_CONCURRENCY", default_concurrency)))
            semaphore = asyncio.Semaphore(concurrency)
            combined_videos = [None] * len(videos)
            write_lock = asyncio.Lock()
            combined_json_path = os.path.join(folders["comments"], "instagram_comments.json")
            progress_tmp_path = os.path.join(folders["comments"], "ig.tmp")

            progress_candidates = []
            for progress_path in (combined_json_path, progress_tmp_path):
                if not os.path.exists(progress_path):
                    continue
                try:
                    with open(progress_path, "r", encoding="utf-8") as f:
                        existing_payload = json.load(f)
                    progress_candidates.append((
                        len(existing_payload.get("videos", [])),
                        os.path.getmtime(progress_path),
                        progress_path,
                        existing_payload,
                    ))
                except Exception as e:
                    print(f"[WARN] Could not load Instagram progress from {progress_path}: {e}", flush=True)

            if progress_candidates:
                try:
                    _, _, progress_path, existing_payload = max(
                        progress_candidates,
                        key=lambda item: (item[0], item[1]),
                    )
                    retry_zero_comments = os.environ.get("INSTAGRAM_RETRY_ZERO_COMMENTS", "0").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    existing_by_id = {
                        str(video.get("video_id")): video
                        for video in existing_payload.get("videos", [])
                        if video.get("video_id")
                    }
                    restored = 0
                    for i, video in enumerate(videos):
                        video_id = video.get("video_id") or scraper.extract_shortcode(video.get("url", ""))
                        if video_id and str(video_id) in existing_by_id:
                            restored_video = dict(existing_by_id[str(video_id)])
                            error_text = str(restored_video.get("error") or "").lower()
                            if (
                                "timed out" in error_text
                                or "target page" in error_text
                                or "browser has been closed" in error_text
                            ):
                                continue
                            if retry_zero_comments and not error_text and not restored_video.get("comments"):
                                continue
                            restored_video["video_number"] = i + 1
                            combined_videos[i] = restored_video
                            restored += 1
                    if restored:
                        if progress_path.endswith(".tmp"):
                            print(f"[INFO] Recovered newer Instagram progress from {progress_path}", flush=True)
                        print(f"[INFO] Resuming Instagram session: restored {restored}/{len(videos)} saved posts", flush=True)
                except Exception as e:
                    print(f"[WARN] Could not load existing Instagram progress: {e}", flush=True)

            async def persist_instagram_payload(complete=False):
                async with write_lock:
                    saved_videos = [video for video in combined_videos if video is not None]
                    payload = {
                        "videos": saved_videos,
                        "complete": complete,
                        "completed_posts": len(saved_videos),
                        "expected_posts": len(videos),
                    }
                    if keyword_terms:
                        payload["keywords"] = keyword_terms
                    payload = finalize_raw_payload(payload, "instagram")
                    max_attempts = 8
                    for attempt in range(1, max_attempts + 1):
                        os.makedirs(folders["comments"], exist_ok=True)
                        tmp_path = progress_tmp_path
                        try:
                            with open(tmp_path, "w", encoding="utf-8") as f:
                                json.dump(payload, f, ensure_ascii=False, indent=2)
                            os.replace(tmp_path, combined_json_path)
                            return
                        except OSError as e:
                            try:
                                if os.path.exists(tmp_path):
                                    os.remove(tmp_path)
                            except OSError:
                                pass
                            if attempt == max_attempts:
                                raise
                            delay = min(0.25 * attempt, 2.0)
                            print(
                                f"[WARN] Instagram progress file locked; retrying save "
                                f"in {delay:.2f}s ({attempt}/{max_attempts}): {e}",
                                flush=True,
                            )
                            await asyncio.sleep(delay)

            if args.instagram_discovery_only:
                combined_videos = [
                    finalize_video_record({
                        "video_number": i + 1,
                        "video_id": v.get("video_id") or scraper.extract_shortcode(v.get("url", "")),
                        "url": v.get("url"),
                        "caption": v.get("title", ""),
                        "content_creator": v.get("username") or "Instagram User",
                        "comment_count": 0,
                        "comments": [],
                        "platform": "instagram",
                        "matched_keywords": v.get("matched_keywords", []),
                        "error": "",
                        "discovery_method": v.get("discovery_method") or "browser_dom",
                        "metadata_method": v.get("metadata_method") or "instagram_graphql_response",
                        "comment_method": "not_collected",
                    }, "instagram")
                    for i, v in enumerate(videos)
                ]
                await persist_instagram_payload(complete=True)
                print(f"\nSuccess! Discovered {len(combined_videos)} Instagram posts without scraping comments.")
                return folders

            async def fetch_instagram_post(i, v):
                async with semaphore:
                    post_label = v.get("url") or v.get("video_id") or f"post {i + 1}"
                    try:
                        res = await with_timeout(
                            scraper.extract_comments(
                                v,
                                max_comments=args.comments if args.comments > 0 else 0,
                                context=context,
                            ),
                            post_timeout,
                            f"Instagram comment scrape for {post_label}",
                        )
                    except Exception as e:
                        print(f"[WARN] Instagram post {i + 1}/{len(videos)} failed: {e}", flush=True)
                        res = {
                            "video_id": v.get("video_id"),
                            "url": v.get("url"),
                            "caption": v.get("title"),
                            "username": v.get("username"),
                            "comments": [],
                            "error": str(e),
                        }
                    return i, finalize_video_record({
                        "video_number": i + 1,
                        "video_id": res.get("video_id"),
                        "media_id": res.get("media_id") or v.get("media_id"),
                        "url": res.get("url") or v.get("url"),
                        "caption": res.get("caption") or v.get("title"),
                        "content_creator": res.get("username") or v.get("username") or "Instagram User",
                        "reported_comment_count": res.get("reported_comment_count") if res.get("reported_comment_count") is not None else v.get("reported_comment_count"),
                        "published_at": res.get("published_at") or v.get("published_at") or v.get("create_time") or "",
                        "content_type": res.get("content_type") or v.get("content_type") or "",
                        "view_count": res.get("view_count") or v.get("view_count") or 0,
                        "like_count": res.get("like_count") or v.get("like_count") or 0,
                        "share_count": res.get("share_count") or v.get("share_count") or 0,
                        "save_count": res.get("save_count") or v.get("save_count") or 0,
                        "follower_count": res.get("follower_count") or v.get("follower_count") or 0,
                        "creator_id": res.get("creator_id") or v.get("creator_id") or "",
                        "creator_display_name": res.get("creator_display_name") or v.get("creator_display_name") or "",
                        "creator_verified": res.get("creator_verified") if res.get("creator_verified") is not None else v.get("creator_verified"),
                        "music_id": res.get("music_id") or v.get("music_id") or "",
                        "music_title": res.get("music_title") or v.get("music_title") or "",
                        "music_author": res.get("music_author") or v.get("music_author") or "",
                        "music_album": res.get("music_album") or v.get("music_album") or "",
                        "music_audio_type": res.get("music_audio_type") or v.get("music_audio_type") or "",
                        "music_is_original": res.get("music_is_original") if res.get("music_is_original") is not None else v.get("music_is_original"),
                        "music_duration_ms": res.get("music_duration_ms") if res.get("music_duration_ms") is not None else v.get("music_duration_ms"),
                        "music_metadata_attempted": res.get("music_metadata_attempted") if res.get("music_metadata_attempted") is not None else v.get("music_metadata_attempted", True),
                        "music_metadata_status": res.get("music_metadata_status") or v.get("music_metadata_status") or "not_provided",
                        "music_metadata_source": res.get("music_metadata_source") or v.get("music_metadata_source") or "instagram_authenticated_web_metadata",
                        "music_metadata_authority": res.get("music_metadata_authority") or v.get("music_metadata_authority") or "experimental_authenticated_web",
                        "music_metadata_access_scope": res.get("music_metadata_access_scope") or v.get("music_metadata_access_scope") or "authenticated_web_api_not_official_graph_api",
                        "duration_seconds": res.get("duration_seconds") or v.get("duration_seconds") or 0,
                        "thumbnail_url": res.get("thumbnail_url") or v.get("thumbnail_url") or "",
                        "content_location_id": res.get("content_location_id") or v.get("content_location_id") or "",
                        "content_location_name": res.get("content_location_name") or v.get("content_location_name") or "",
                        "content_location_latitude": res.get("content_location_latitude") if res.get("content_location_latitude") is not None else v.get("content_location_latitude"),
                        "content_location_longitude": res.get("content_location_longitude") if res.get("content_location_longitude") is not None else v.get("content_location_longitude"),
                        "comment_count": len(res.get("comments", [])),
                        "comments": res.get("comments", []),
                        "platform": "instagram",
                        "matched_keywords": v.get("matched_keywords", []),
                        "error": res.get("error", ""),
                        "discovery_method": res.get("discovery_method") or v.get("discovery_method") or "browser_dom",
                        "metadata_method": res.get("metadata_method") or v.get("metadata_method") or "instagram_graphql_api",
                        "comment_method": res.get("comment_method") or "instagram_direct_api",
                        "comments_exhausted": res.get("comments_exhausted"),
                        "comment_limit_reached": res.get("comment_limit_reached"),
                        "comments_seen_in_response": res.get("comments_seen_in_response"),
                        "fallback_used": res.get("fallback_used", False),
                    }, "instagram")

            await persist_instagram_payload(complete=False)
            tasks = [
                asyncio.create_task(fetch_instagram_post(i, v))
                for i, v in enumerate(videos)
                if combined_videos[i] is None
            ]
            for task in asyncio.as_completed(tasks):
                i, video_obj = await task
                combined_videos[i] = video_obj
                await persist_instagram_payload(complete=False)
                print(
                    f"[INFO] Instagram post {i + 1}/{len(videos)} saved "
                    f"({video_obj['comment_count']} comments)",
                    flush=True,
                )
            combined_videos = [video for video in combined_videos if video is not None]

            combined_videos_for_payload = combined_videos
            combined_videos = [None] * len(combined_videos_for_payload)
            for index, video in enumerate(combined_videos_for_payload):
                combined_videos[index] = video
            await persist_instagram_payload(complete=True)
            combined_videos = combined_videos_for_payload

            total_comments = sum(len(v["comments"]) for v in combined_videos)
            print(f"\nSuccess! Scraped {total_comments} total Instagram comments from {len(combined_videos)} posts.")
            return folders
        finally:
            if context and not getattr(scraper, "_external_context", False):
                await context.close()
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()

    async def run_x(session_override=None):
        from tiktok_scraper.main import save_comments_to_file
        from tiktok_scraper.scrapers.x_scraper import XScraper
        from tiktok_scraper.utils.file_utils import create_session_folders

        extraction_mode = args.x_extraction_mode
        if extraction_mode == "official":
            scraper = XScraper(search_mode=args.x_search_mode)
        else:
            from tiktok_scraper.scrapers.x_browser_scraper import XBrowserScraper

            scraper = XBrowserScraper()

        filename_prefix = "x_" if args.platform == "all" else ""
        target = url or args.search or ""
        direct_x_post = bool(XScraper.extract_post_id(target))
        playwright = browser = context = None

        try:
            if extraction_mode == "browser-api":
                playwright, browser, context = await scraper._new_context()
                await scraper.require_authenticated(context)

            if candidate_mode:
                posts = [dict(candidate) for candidate in candidate_videos]
            else:
                search_kwargs = {
                    "max_videos": discovery_video_limit if discovery_video_limit > 0 else 0,
                    "since_id": args.x_since_id,
                }
                if context is not None:
                    search_kwargs["context"] = context
                posts = await scraper.search(target, **search_kwargs)
                posts = annotate_discovery_source(posts, target)

            posts = apply_discovery_cap(posts, "x")
            folders = create_session_folders(session_name=session_override)
            posts = apply_date_gate(posts, "x", folders)
            if not direct_x_post:
                posts = apply_relevance_gate(posts, "x", folders)
                posts = apply_known_content_gate(posts, "x", folders)
            posts = apply_extraction_cap(posts, "x")

            combined_posts = []
            try:
                per_post_timeout = max(0.0, float(os.environ.get("X_PER_POST_TIMEOUT", "300")))
            except ValueError:
                per_post_timeout = 300.0

            for index, post in enumerate(posts, start=1):
                extraction_kwargs = {
                    "max_comments": args.comments if args.comments > 0 else 0,
                }
                if context is not None:
                    extraction_kwargs["context"] = context
                extraction = scraper.extract_comments(post, **extraction_kwargs)
                try:
                    result = (
                        await asyncio.wait_for(extraction, timeout=per_post_timeout)
                        if per_post_timeout
                        else await extraction
                    )
                except asyncio.TimeoutError:
                    browser_api = extraction_mode == "browser-api"
                    result = {
                        **dict(post),
                        "comments": [],
                        "comments_seen_in_response": 0,
                        "comments_exhausted": None,
                        "comment_error": f"x_comment_timeout_after_{per_post_timeout:g}s",
                        "comment_method": (
                            "x_browser_graphql_timeout"
                            if browser_api
                            else "x_api_v2_conversation_search_timeout"
                        ),
                        "metadata_method": post.get("metadata_method") or (
                            "x_graphql_response" if browser_api else "x_api_v2"
                        ),
                    }

                comments = result.get("comments") if isinstance(result.get("comments"), list) else []
                metadata = {
                    key: value
                    for key, value in result.items()
                    if key not in {"comments", "video_number", "comment_count", "platform"}
                }
                await save_comments_to_file(
                    comments,
                    index,
                    folders["comments"],
                    video_id=result.get("video_id") or post.get("video_id"),
                    caption=result.get("caption") or result.get("title") or post.get("title"),
                    username=result.get("username") or post.get("username") or "X User",
                    extra_metadata=metadata,
                    filename_prefix=filename_prefix,
                    platform="x",
                )
                combined_posts.append(
                    finalize_video_record(
                        {
                            **result,
                            "video_number": index,
                            "video_id": result.get("video_id") or post.get("video_id"),
                            "url": result.get("url") or post.get("url"),
                            "caption": result.get("caption") or result.get("title") or post.get("title") or "",
                            "content_creator": result.get("username") or post.get("username") or "X User",
                            "comment_count": len(comments),
                            "comments": comments,
                            "platform": "x",
                        },
                        "x",
                    )
                )

            usage = scraper.usage_summary()
            raw_payload = {
                "videos": combined_posts,
                "x_extraction_mode": extraction_mode,
                "x_transport_usage": usage,
            }
            if extraction_mode == "official":
                raw_payload["x_api_usage"] = usage
                raw_payload["x_search_mode"] = scraper.search_mode
            else:
                raw_payload["x_browser_api_usage"] = usage

            combined_json_path = os.path.join(folders["comments"], "x_comments.json")
            with open(combined_json_path, "w", encoding="utf-8") as handle:
                json.dump(
                    finalize_raw_payload(raw_payload, "x"),
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            flat_comments = sum(
                int(post.get("comments_seen_in_response") or 0)
                for post in combined_posts
            )
            if extraction_mode == "official":
                detail = (
                    f"API post reads={usage['post_reads']} "
                    f"estimated_cost_usd={usage['estimated_cost_usd']:.4f}"
                )
            else:
                detail = (
                    f"web GraphQL responses={usage['graphql_response_count']} "
                    "official API reads=0"
                )
            print(
                f"\nSuccess! Scraped {flat_comments} X replies from {len(combined_posts)} posts; {detail}."
            )
            return folders
        finally:
            if context and not getattr(scraper, "_external_context", False):
                await context.close()
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()

    async def run_fb(session_override=None):
        from tiktok_scraper.scrapers.facebook_scraper import FacebookScraper
        from tiktok_scraper.utils.file_utils import create_session_folders
        from tiktok_scraper.main import save_comments_to_file

        scraper = FacebookScraper()
        filename_prefix = "facebook_" if args.platform == "all" else ""
        target = url or args.search or ""
        fb_discovery_videos = discovery_video_limit if discovery_video_limit > 0 else 0
        playwright = browser = context = None

        try:
            playwright, browser, context = await scraper._new_context()
            direct_facebook_post = bool(
                target
                and scraper.is_facebook_url(target)
                and scraper.extract_content_id(target)
            )
            if candidate_mode:
                videos = [dict(candidate) for candidate in candidate_videos]
            elif direct_facebook_post:
                normalized_url = scraper._normalize_url(target)
                videos = [{
                    "video_id": scraper.extract_content_id(normalized_url),
                    "url": normalized_url,
                    "title": "Direct Facebook URL",
                    "username": scraper._creator_from_url(normalized_url),
                    "discovery_method": "direct_url",
                }]
            else:
                videos = await scraper.search(target, max_videos=fb_discovery_videos, context=context)
                videos = annotate_discovery_source(videos, target)

            videos = apply_discovery_cap(videos, "facebook")
            folders = create_session_folders(session_name=session_override)
            videos = apply_date_gate(videos, "facebook", folders)
            if not direct_facebook_post:
                videos = apply_relevance_gate(videos, "facebook", folders)
                videos = apply_known_content_gate(videos, "facebook", folders)
            videos = apply_extraction_cap(videos, "facebook")

            default_concurrency = "1" if os.environ.get("FACEBOOK_USER_DATA_DIR") or os.environ.get("FB_USER_DATA_DIR") else "2"
            concurrency = max(1, int(os.environ.get("FACEBOOK_CONCURRENCY", default_concurrency)))
            semaphore = asyncio.Semaphore(concurrency)
            combined_videos = [None] * len(videos)
            write_lock = asyncio.Lock()
            combined_json_path = os.path.join(folders["comments"], "facebook_comments.json")

            def facebook_payload_from_saved(idx, payload, candidate):
                comments = payload.get("comments")
                if not isinstance(comments, list):
                    comments = []
                return {
                    "video_number": idx + 1,
                    "video_id": payload.get("video_id") or candidate.get("video_id"),
                    "url": payload.get("url") or candidate.get("url"),
                    "caption": payload.get("caption") or candidate.get("title"),
                    "content_creator": payload.get("content_creator") or payload.get("username") or candidate.get("username") or "Facebook User",
                    "reported_comment_count": payload.get("reported_comment_count"),
                    "published_at": payload.get("published_at") or candidate.get("published_at") or "",
                    "content_type": payload.get("content_type") or candidate.get("content_type") or "",
                    "view_count": payload.get("view_count") if payload.get("view_count") is not None else candidate.get("view_count"),
                    "like_count": payload.get("like_count") if payload.get("like_count") is not None else candidate.get("like_count"),
                    "share_count": payload.get("share_count") if payload.get("share_count") is not None else candidate.get("share_count"),
                    "save_count": payload.get("save_count") if payload.get("save_count") is not None else candidate.get("save_count"),
                    "follower_count": payload.get("follower_count") if payload.get("follower_count") is not None else candidate.get("follower_count"),
                    "creator_id": payload.get("creator_id") or candidate.get("creator_id") or "",
                    "creator_verified": payload.get("creator_verified") if payload.get("creator_verified") is not None else candidate.get("creator_verified"),
                    "comment_count": len(comments),
                    "comments": comments,
                    "platform": "facebook",
                    "matched_keywords": payload.get("matched_keywords") or candidate.get("matched_keywords", []),
                    "error": payload.get("error", ""),
                    "source": payload.get("source", ""),
                    "browser_api_comment_count": payload.get("browser_api_comment_count"),
                    "browser_api_replay_attempts": payload.get("browser_api_replay_attempts"),
                    "browser_api_replay_new_comments": payload.get("browser_api_replay_new_comments"),
                    "browser_api_replay_error": payload.get("browser_api_replay_error"),
                    "browser_api_cursor_present": payload.get("browser_api_cursor_present"),
                    "browser_api_candidate_request_count": payload.get("browser_api_candidate_request_count"),
                    "browser_api_comment_response_count": payload.get("browser_api_comment_response_count"),
                    "browser_api_cursor_update_count": payload.get("browser_api_cursor_update_count"),
                    "discovery_method": payload.get("discovery_method") or candidate.get("discovery_method") or "browser_dom",
                    "metadata_method": payload.get("metadata_method") or candidate.get("metadata_method") or "facebook_page_meta",
                    "comment_method": payload.get("comment_method") or payload.get("source") or "unknown",
                    "comments_exhausted": payload.get("comments_exhausted"),
                    "comment_limit_reached": payload.get("comment_limit_reached"),
                    "comments_seen_in_response": payload.get("comments_seen_in_response") or payload.get("browser_api_comment_count"),
                    "fallback_used": payload.get("fallback_used", False),
                }

            if session_override:
                candidates_by_id = {
                    str(video.get("video_id")): index
                    for index, video in enumerate(videos)
                    if video.get("video_id")
                }
                for filename in os.listdir(folders["comments"]):
                    match = re.match(
                        rf"{re.escape(filename_prefix)}video_(\d+)_comments\.json$",
                        filename,
                    )
                    if not match:
                        continue
                    file_path = os.path.join(folders["comments"], filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            payload = json.load(f)
                        saved_id = str(payload.get("video_id") or "")
                        idx = candidates_by_id.get(saved_id)
                        if idx is None:
                            numbered_idx = int(match.group(1)) - 1
                            if 0 <= numbered_idx < len(videos) and not saved_id:
                                idx = numbered_idx
                            else:
                                print(
                                    f"[WARN] Ignoring stale Facebook resume file {filename}: "
                                    f"saved post {saved_id or 'unknown'} is not in current discovery",
                                    flush=True,
                                )
                                continue
                        combined_videos[idx] = facebook_payload_from_saved(idx, payload, videos[idx])
                    except Exception as e:
                        print(f"[WARN] Could not load saved Facebook post {idx + 1} from {filename}: {e}", flush=True)

                loaded_posts = sum(1 for video in combined_videos if video is not None)
                if loaded_posts:
                    print(
                        f"[INFO] Facebook resume loaded {loaded_posts}/{len(videos)} saved posts "
                        f"from {folders['comments']}",
                        flush=True,
                    )

            async def persist_facebook_payload(complete=False):
                async with write_lock:
                    saved_videos = [video for video in combined_videos if video is not None]
                    payload = {
                        "videos": saved_videos,
                        "complete": complete,
                        "completed_posts": len(saved_videos),
                        "expected_posts": len(videos),
                    }
                    payload = finalize_raw_payload(payload, "facebook")
                    tmp_path = f"{combined_json_path}.{os.getpid()}.tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, combined_json_path)

            async def fetch_facebook_post(i, v):
                async with semaphore:
                    try:
                        res = await scraper.extract_comments(
                            v,
                            max_comments=args.comments if args.comments > 0 else 0,
                            context=context,
                        )
                    except Exception as e:
                        print(f"[WARN] Facebook post {i + 1}/{len(videos)} failed: {e}", flush=True)
                        res = {
                            "video_id": v.get("video_id"),
                            "url": v.get("url"),
                            "caption": v.get("title"),
                            "username": v.get("username"),
                            "comments": [],
                            "error": str(e),
                        }

                    extra_metadata = {
                        "url": res.get("url") or v.get("url"),
                        "platform": "facebook",
                        "reported_comment_count": res.get("reported_comment_count") if res.get("reported_comment_count") is not None else v.get("reported_comment_count"),
                        "published_at": res.get("published_at") or v.get("published_at") or "",
                        "content_type": res.get("content_type") or v.get("content_type") or "",
                        "view_count": res.get("view_count") if res.get("view_count") is not None else v.get("view_count"),
                        "like_count": res.get("like_count") if res.get("like_count") is not None else v.get("like_count"),
                        "share_count": res.get("share_count") if res.get("share_count") is not None else v.get("share_count"),
                        "save_count": res.get("save_count") if res.get("save_count") is not None else v.get("save_count"),
                        "follower_count": res.get("follower_count") if res.get("follower_count") is not None else v.get("follower_count"),
                        "creator_id": res.get("creator_id") or v.get("creator_id") or "",
                        "creator_verified": res.get("creator_verified") if res.get("creator_verified") is not None else v.get("creator_verified"),
                        "matched_keywords": v.get("matched_keywords", []),
                        "error": res.get("error", ""),
                        "source": res.get("source", ""),
                        "browser_api_comment_count": res.get("browser_api_comment_count"),
                        "browser_api_replay_attempts": res.get("browser_api_replay_attempts"),
                        "browser_api_replay_new_comments": res.get("browser_api_replay_new_comments"),
                        "browser_api_replay_error": res.get("browser_api_replay_error"),
                        "browser_api_cursor_present": res.get("browser_api_cursor_present"),
                        "browser_api_candidate_request_count": res.get("browser_api_candidate_request_count"),
                        "browser_api_comment_response_count": res.get("browser_api_comment_response_count"),
                        "browser_api_cursor_update_count": res.get("browser_api_cursor_update_count"),
                        "discovery_method": v.get("discovery_method") or "browser_dom",
                        "metadata_method": res.get("metadata_method") or "facebook_page_meta",
                        "comment_method": res.get("comment_method") or res.get("source") or "unknown",
                        "comments_exhausted": res.get("comments_exhausted"),
                        "comment_limit_reached": res.get("comment_limit_reached"),
                        "comments_seen_in_response": res.get("comments_seen_in_response") or res.get("browser_api_comment_count"),
                        "fallback_used": res.get("fallback_used", False),
                    }
                    await save_comments_to_file(
                        res.get("comments", []),
                        i + 1,
                        folders["comments"],
                        video_id=res.get("video_id") or v.get("video_id"),
                        caption=res.get("caption") or v.get("title"),
                        username=res.get("username") or v.get("username") or "Facebook User",
                        extra_metadata=extra_metadata,
                        filename_prefix=filename_prefix,
                        platform="facebook",
                    )

                    return i, finalize_video_record({
                        "video_number": i + 1,
                        "video_id": res.get("video_id") or v.get("video_id"),
                        "url": res.get("url") or v.get("url"),
                        "caption": res.get("caption") or v.get("title"),
                        "content_creator": res.get("username") or v.get("username") or "Facebook User",
                        "reported_comment_count": res.get("reported_comment_count") if res.get("reported_comment_count") is not None else v.get("reported_comment_count"),
                        "published_at": res.get("published_at") or v.get("published_at") or "",
                        "content_type": res.get("content_type") or v.get("content_type") or "",
                        "view_count": res.get("view_count") if res.get("view_count") is not None else v.get("view_count"),
                        "like_count": res.get("like_count") if res.get("like_count") is not None else v.get("like_count"),
                        "share_count": res.get("share_count") if res.get("share_count") is not None else v.get("share_count"),
                        "save_count": res.get("save_count") if res.get("save_count") is not None else v.get("save_count"),
                        "follower_count": res.get("follower_count") if res.get("follower_count") is not None else v.get("follower_count"),
                        "creator_id": res.get("creator_id") or v.get("creator_id") or "",
                        "creator_verified": res.get("creator_verified") if res.get("creator_verified") is not None else v.get("creator_verified"),
                        "comment_count": len(res.get("comments", [])),
                        "comments": res.get("comments", []),
                        "platform": "facebook",
                        "matched_keywords": v.get("matched_keywords", []),
                        "error": res.get("error", ""),
                        "source": res.get("source", ""),
                        "browser_api_comment_count": res.get("browser_api_comment_count"),
                        "browser_api_replay_attempts": res.get("browser_api_replay_attempts"),
                        "browser_api_replay_new_comments": res.get("browser_api_replay_new_comments"),
                        "browser_api_replay_error": res.get("browser_api_replay_error"),
                        "browser_api_cursor_present": res.get("browser_api_cursor_present"),
                        "browser_api_candidate_request_count": res.get("browser_api_candidate_request_count"),
                        "browser_api_comment_response_count": res.get("browser_api_comment_response_count"),
                        "browser_api_cursor_update_count": res.get("browser_api_cursor_update_count"),
                        "discovery_method": v.get("discovery_method") or "browser_dom",
                        "metadata_method": res.get("metadata_method") or "facebook_page_meta",
                        "comment_method": res.get("comment_method") or res.get("source") or "unknown",
                        "comments_exhausted": res.get("comments_exhausted"),
                        "comment_limit_reached": res.get("comment_limit_reached"),
                        "comments_seen_in_response": res.get("comments_seen_in_response") or res.get("browser_api_comment_count"),
                        "fallback_used": res.get("fallback_used", False),
                    }, "facebook")

            await persist_facebook_payload(complete=False)
            pending_videos = [
                (i, v)
                for i, v in enumerate(videos)
                if combined_videos[i] is None
            ]
            if len(pending_videos) < len(videos):
                print(
                    f"[INFO] Facebook resume will scrape {len(pending_videos)} remaining posts",
                    flush=True,
                )

            tasks = [asyncio.create_task(fetch_facebook_post(i, v)) for i, v in pending_videos]
            for task in asyncio.as_completed(tasks):
                i, video_obj = await task
                combined_videos[i] = video_obj
                await persist_facebook_payload(complete=False)
                print(
                    f"[INFO] Facebook post {i + 1}/{len(videos)} saved "
                    f"({video_obj['comment_count']} comments)",
                    flush=True,
                )

            combined_videos = [video for video in combined_videos if video is not None]
            await persist_facebook_payload(complete=True)
            total_comments = sum(len(v["comments"]) for v in combined_videos)
            print(f"\nSuccess! Scraped {total_comments} total Facebook comments from {len(combined_videos)} posts.")
            return folders
        finally:
            if context and not scraper._external_context:
                await context.close()
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()

    async def run_tk(session_override=None):
        tk_url = url
        if args.search and args.platform == 'all':
            import urllib.parse
            encoded_query = urllib.parse.quote(sanitize_search_keyword(args.search))
            tk_url = f"https://www.tiktok.com/search/video?q={encoded_query}"

        if args.relevance_mode != "off" and relevance_profile:
            os.environ["SCRAPER_RELEVANCE_MODE"] = args.relevance_mode
            os.environ["SCRAPER_RELEVANCE_REVIEW_ACTION"] = args.relevance_review_action
            os.environ["SCRAPER_RELEVANCE_PROFILE_JSON"] = json.dumps(relevance_profile, ensure_ascii=False)
        else:
            os.environ.pop("SCRAPER_RELEVANCE_MODE", None)
            os.environ.pop("SCRAPER_RELEVANCE_REVIEW_ACTION", None)
            os.environ.pop("SCRAPER_RELEVANCE_PROFILE_JSON", None)

        if args.known_content_file:
            os.environ["SCRAPER_KNOWN_CONTENT_FILE"] = args.known_content_file
        else:
            os.environ.pop("SCRAPER_KNOWN_CONTENT_FILE", None)

        # Run the TikTok scraper
        await run_scraper(
            record_mode=args.record,
            num_videos=args.videos,
            default_percentage=args.percentage,
            target_url=tk_url,
            max_videos=max_videos,
            discovery_videos=discovery_video_limit,
            max_comments=args.comments if args.comments > 0 else 0,
            use_api=args.transport_mode != "dom",
            reset_checkpoint=args.reset_checkpoint,
            session_name=session_override,
            output_prefix="tiktok_" if args.platform == "all" else "",
            candidate_videos=candidate_videos if candidate_mode else None,
        )
        from tiktok_scraper.utils.file_utils import create_session_folders
        return create_session_folders(session_name=session_override)

    async def orchestrate():
        final_folders = None
        if args.platform == 'youtube' or args.platform == 'all':
            print("\n[INFO] Starting YouTube Scraper...")
            final_folders = await run_yt(session_override=session_name)
        if args.platform == 'tiktok' or args.platform == 'all':
            print("\n[INFO] Starting TikTok Scraper...")
            final_folders = await run_tk(session_override=session_name)
        if args.platform == 'instagram' or args.platform == 'all':
            print("\n[INFO] Starting Instagram Scraper...")
            try:
                final_folders = await run_ig(session_override=session_name)
            except Exception as e:
                if args.platform == 'instagram':
                    raise
                print(f"[WARN] Instagram scraper failed; continuing multi-platform run: {e}")
        if args.platform == 'facebook' or args.platform == 'all':
            print("\n[INFO] Starting Facebook Scraper...")
            try:
                final_folders = await run_fb(session_override=session_name)
            except Exception as e:
                if args.platform == 'facebook':
                    raise
                print(f"[WARN] Facebook scraper failed; continuing multi-platform run: {e}")
        if args.platform == 'x' or args.platform == 'all':
            print("\n[INFO] Starting X Scraper...")
            try:
                final_folders = await run_x(session_override=session_name)
            except Exception as e:
                if args.platform == 'x':
                    raise
                print(f"[WARN] X scraper failed; continuing multi-platform run: {e}")

        if not final_folders:
            print(f"Platform {args.platform} is currently stubbed and not fully implemented.")
            return

        if args.auto_analyze or args.platform == 'all':
            # Prefer aggregate exports and use stable platform/content identities
            # so per-video checkpoint files cannot duplicate aggregate records.
            session_path = os.path.dirname(final_folders["comments"])
            comments_path = os.path.join(session_path, "comments")
            aggregate_names = [
                "youtube_comments.json",
                "instagram_comments.json",
                "facebook_comments.json",
                "tiktok_comments.json",
                "x_comments.json",
            ]
            aggregate_files = [
                os.path.join(comments_path, name)
                for name in aggregate_names
                if os.path.exists(os.path.join(comments_path, name))
            ]
            aggregate_set = set(aggregate_files)
            per_video_files = sorted(
                path
                for path in glob.glob(os.path.join(comments_path, "*_comments.json"))
                if path not in aggregate_set and "combined_comments.json" not in path
            )
            json_files = aggregate_files + per_video_files

            def record_identity(video, source_path):
                platform = str(video.get("platform") or "").strip().lower()
                url_value = str(video.get("url") or video.get("video_url") or "").lower()
                source_name = os.path.basename(source_path).lower()
                if not platform:
                    for candidate in ("youtube", "tiktok", "instagram", "facebook"):
                        if source_name.startswith(candidate) or candidate in url_value:
                            platform = candidate
                            break
                    if not platform and (
                        source_name.startswith("x_")
                        or "x.com/" in url_value
                        or "twitter.com/" in url_value
                    ):
                        platform = "x"
                identity = str(
                    video.get("video_id")
                    or video.get("media_id")
                    or video.get("url")
                    or video.get("video_url")
                    or ""
                ).strip()
                return (platform or "unknown", identity) if identity else None

            combined_videos = []
            seen_records = set()
            for f in json_files:
                with open(f, 'r', encoding='utf-8') as file:
                    try:
                        data = json.load(file)
                        if isinstance(data, list):
                            records = data
                        elif 'comments' in data:
                            records = [data]
                        elif 'videos' in data:
                            records = data['videos']
                        else:
                            records = []
                        for record in records:
                            if not isinstance(record, dict):
                                continue
                            identity = record_identity(record, f)
                            if identity and identity in seen_records:
                                continue
                            if identity:
                                seen_records.add(identity)
                            combined_videos.append(record)
                    except Exception as exc:
                        print(f"[WARN] Could not combine {f}: {exc}", flush=True)
                        continue

            if combined_videos:
                combined_path = os.path.join(session_path, 'combined_comments.json')
                with open(combined_path, 'w', encoding='utf-8') as out:
                    json.dump({"session_id": session_name, "videos": combined_videos}, out, indent=2, ensure_ascii=False)
                print(f'Combined {len(json_files)} files into {combined_path}')

                if args.auto_analyze:
                    print(f"\n[INFO] Starting auto-analysis on {combined_path}")
                    from tiktok_scraper.ai_analytics import run_analysis_pipeline
                    # Output directly to session_path instead of analysis_reports
                    run_analysis_pipeline(combined_path, output_dir=session_path)

    asyncio.run(orchestrate())

    if args.match_music:
        import subprocess

        print("\n" + "="*60)
        print(" Running Music Matcher ".center(60, "="))
        print("="*60)

        # Find the latest session folder
        comments_dir = "comments_data"
        if os.path.exists(comments_dir):
            sessions = [d for d in os.listdir(comments_dir) if d.startswith("session_")]
            if sessions:
                latest_session = sorted(sessions)[-1]
                session_path = os.path.join(comments_dir, latest_session)
                print(f"Applying music matcher to latest session: {session_path}")

                try:
                    subprocess.run([sys.executable, "run_music_matcher.py", session_path])
                except Exception as e:
                    print(f"Failed to run music matcher: {e}")
            else:
                print("No session folders found in comments_data.")

if __name__ == "__main__":
    main()
