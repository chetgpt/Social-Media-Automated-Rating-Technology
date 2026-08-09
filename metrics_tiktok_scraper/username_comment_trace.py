"""Username-first tracing of public TikTok comments.

The username is the sole pre-discovery seed. Search results create a bounded
post pool, then public comments and replies are scanned for exact author
matches. Only comments authored by the target are retained.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import re
import time
from typing import Any
import unicodedata
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page, async_playwright

from tiktok_scraper.api_integration import (
    TikTokAPIIntegration,
    TikTokSearchSessionError,
)


AUTH_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt"}
TOP_LEVEL_COMMENT_PATH = "/api/comment/list/"
REPLY_COMMENT_PATH = "/api/comment/list/reply/"


def text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_username(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", text(value))
    return normalized.lstrip("@").casefold()


def post_id_from_url(value: Any) -> str:
    match = re.search(r"/(?:video|photo)/(\d+)", text(value))
    return match.group(1) if match else ""


def username_from_post_url(value: Any) -> str:
    match = re.search(r"tiktok\.com/@([^/?#]+)", text(value), re.IGNORECASE)
    return text(match.group(1)) if match else ""


def comment_author(comment: dict[str, Any]) -> dict[str, Any]:
    user = as_dict(
        comment.get("user")
        or comment.get("author")
        or comment.get("user_info")
        or comment.get("userInfo")
    )
    username = text(
        user.get("unique_id")
        or user.get("uniqueId")
        or comment.get("author_username")
        or comment.get("username")
    )
    identifiers = {
        text(value)
        for value in (
            user.get("uid"),
            user.get("id"),
            user.get("user_id"),
            user.get("sec_uid"),
            user.get("secUid"),
            comment.get("author_user_id"),
        )
        if text(value)
    }
    return {
        "username": username,
        "normalized_username": normalize_username(username),
        "display_name": text(
            user.get("nickname")
            or user.get("display_name")
            or user.get("displayName")
        ),
        "identifiers": sorted(identifiers),
    }


def target_author_match(
    comment: dict[str, Any],
    target_username: str,
    target_identifiers: set[str],
) -> tuple[bool, dict[str, Any]]:
    author = comment_author(comment)
    username_match = (
        bool(author["normalized_username"])
        and author["normalized_username"] == normalize_username(target_username)
    )
    identifier_match = bool(target_identifiers.intersection(author["identifiers"]))
    if username_match:
        target_identifiers.update(author["identifiers"])
    return username_match or identifier_match, author


def normalize_target_comment(
    comment: dict[str, Any],
    *,
    author: dict[str, Any],
    post: dict[str, Any],
    parent_comment_id: str = "",
) -> dict[str, Any]:
    comment_id = text(comment.get("cid") or comment.get("id"))
    return {
        "platform": "tiktok",
        "comment_id": comment_id,
        "parent_comment_id": text(
            parent_comment_id
            or comment.get("parent_comment_id")
            or comment.get("reply_id")
        ),
        "text": text(comment.get("text") or comment.get("comment_text")),
        "created_at": comment.get("create_time") or comment.get("createTime") or "",
        "like_count": comment.get("digg_count")
        if comment.get("digg_count") is not None
        else comment.get("like_count"),
        "author": {
            "username": author["username"],
            "display_name": author["display_name"],
            "public_identifiers": author["identifiers"],
        },
        "source_post": {
            "post_id": text(post.get("post_id")),
            "url": text(post.get("url")),
            "owner_username": text(post.get("owner_username")),
            "caption": text(post.get("caption")),
        },
        "discovery": {
            "pre_discovery_seed": text(post.get("pre_discovery_seed")),
            "query": text(post.get("search_query")),
            "method": text(post.get("discovery_method")),
        },
    }


def candidate_from_search_result(
    value: dict[str, Any],
    *,
    username_seed: str,
) -> dict[str, Any]:
    url = text(value.get("url"))
    post_id = text(value.get("id") or value.get("post_id") or post_id_from_url(url))
    owner_username = text(value.get("username") or username_from_post_url(url))
    if not post_id or not url:
        return {}
    return {
        "post_id": post_id,
        "url": url.split("?", 1)[0],
        "owner_username": owner_username,
        "caption": text(value.get("caption") or value.get("title")),
        "pre_discovery_seed": username_seed,
        "search_query": username_seed,
        "discovery_method": text(
            value.get("discovery_method") or "tiktok_username_search"
        ),
    }


async def _browser_fetch_json(
    page: Page,
    path: str,
    params: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    result = await page.evaluate(
        """
        async ({path, params, timeoutMs}) => {
          const url = new URL(path, "https://www.tiktok.com");
          for (const [key, value] of Object.entries(params)) {
            url.searchParams.set(key, String(value));
          }
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), timeoutMs);
          try {
            const response = await fetch(url.toString(), {
              credentials: "include",
              headers: {"accept": "application/json, text/plain, */*"},
              signal: controller.signal
            });
            const body = await response.text();
            let data = null;
            try { data = JSON.parse(body); } catch (_) {}
            return {
              http_status: response.status,
              body_length: body.length,
              data
            };
          } catch (error) {
            return {
              http_status: null,
              body_length: 0,
              data: null,
              error: String(error)
            };
          } finally {
            clearTimeout(timer);
          }
        }
        """,
        {
            "path": path,
            "params": params,
            "timeoutMs": int(max(1.0, timeout_seconds) * 1000),
        },
    )
    return result if isinstance(result, dict) else {}


async def _hydrate_owner(
    context: BrowserContext,
    candidate: dict[str, Any],
    *,
    timeout_seconds: float,
) -> None:
    if text(candidate.get("owner_username")):
        return
    endpoint = (
        "https://www.tiktok.com/oembed?url="
        + quote(candidate["url"], safe="")
    )
    try:
        response = await context.request.get(
            endpoint,
            timeout=int(max(1.0, timeout_seconds) * 1000),
        )
        payload = await response.json() if response.ok else {}
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    candidate["caption"] = text(candidate.get("caption") or payload.get("title"))
    candidate["owner_username"] = username_from_post_url(payload.get("author_url"))


class TikTokUsernameCommentTracer:
    def __init__(
        self,
        *,
        username: str,
        cdp_url: str,
        max_search_pages: int = 3,
        max_posts: int = 20,
        max_comments_per_post: int = 0,
        max_comment_pages: int = 100,
        max_reply_pages: int = 100,
        request_timeout_seconds: float = 20,
        navigation_timeout_seconds: float = 45,
        discovery_timeout_seconds: float = 90,
    ) -> None:
        self.username = text(username).lstrip("@")
        self.cdp_url = text(cdp_url)
        self.max_search_pages = max(1, int(max_search_pages))
        self.max_posts = max(1, int(max_posts))
        self.max_comments_per_post = max(0, int(max_comments_per_post))
        self.max_comment_pages = max(1, int(max_comment_pages))
        self.max_reply_pages = max(1, int(max_reply_pages))
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self.navigation_timeout_seconds = max(
            1.0, float(navigation_timeout_seconds)
        )
        self.discovery_timeout_seconds = max(
            5.0, float(discovery_timeout_seconds)
        )
        self.target_identifiers: set[str] = set()
        self.seen_target_comment_ids: set[str] = set()

    async def _discover_candidates(
        self,
        page: Page,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        integration = TikTokAPIIntegration(enable_api=False)
        search_error = ""
        api_candidates: list[dict[str, Any]] = []
        try:
            rows = await integration.discover_search_videos(
                page,
                self.username,
                max_offsets=self.max_search_pages,
                include_related_queries=False,
            )
            api_candidates = [
                candidate_from_search_result(row, username_seed=self.username)
                for row in rows
            ]
            api_candidates = [item for item in api_candidates if item]
        except TikTokSearchSessionError as exc:
            search_error = str(exc)
        except Exception as exc:
            search_error = f"{type(exc).__name__}: {exc}"

        dom_candidates: list[dict[str, Any]] = []
        try:
            links = await page.locator('a[href*="/video/"]').evaluate_all(
                """
                nodes => [...new Set(nodes.map(node => node.href).filter(Boolean))]
                """
            )
        except Exception:
            links = []
        for url in links:
            candidate = candidate_from_search_result(
                {"url": url, "discovery_method": "tiktok_username_search_dom"},
                username_seed=self.username,
            )
            if candidate:
                dom_candidates.append(candidate)

        combined: dict[str, dict[str, Any]] = {}
        for candidate in [*api_candidates, *dom_candidates]:
            combined.setdefault(candidate["post_id"], candidate)
        diagnostics = {
            "search_query": self.username,
            "search_api_candidates": len(api_candidates),
            "search_dom_candidates": len(dom_candidates),
            "candidate_count_before_owner_filter": len(combined),
            "search_error": search_error,
        }
        return list(combined.values()), diagnostics

    def _record_if_target(
        self,
        comment: dict[str, Any],
        *,
        post: dict[str, Any],
        target_comments: list[dict[str, Any]],
        parent_comment_id: str = "",
    ) -> None:
        matched, author = target_author_match(
            comment,
            self.username,
            self.target_identifiers,
        )
        if not matched:
            return
        normalized = normalize_target_comment(
            comment,
            author=author,
            post=post,
            parent_comment_id=parent_comment_id,
        )
        marker = normalized["comment_id"] or (
            normalized["source_post"]["post_id"] + ":" + normalized["text"]
        )
        if marker in self.seen_target_comment_ids:
            return
        self.seen_target_comment_ids.add(marker)
        target_comments.append(normalized)

    async def _scan_replies(
        self,
        page: Page,
        *,
        post: dict[str, Any],
        top_comment: dict[str, Any],
        target_comments: list[dict[str, Any]],
        scanned_ids: set[str],
        remaining_budget: int | None,
    ) -> dict[str, Any]:
        comment_id = text(top_comment.get("cid") or top_comment.get("id"))
        try:
            expected = int(top_comment.get("reply_comment_total") or 0)
        except (TypeError, ValueError):
            expected = 0
        embedded = as_list(
            top_comment.get("reply_comment")
            or top_comment.get("reply_comments")
            or top_comment.get("replies")
        )
        scanned = 0
        for reply in embedded:
            if not isinstance(reply, dict):
                continue
            reply_id = text(reply.get("cid") or reply.get("id"))
            if reply_id and reply_id in scanned_ids:
                continue
            if reply_id:
                scanned_ids.add(reply_id)
            scanned += 1
            self._record_if_target(
                reply,
                post=post,
                target_comments=target_comments,
                parent_comment_id=comment_id,
            )
            if remaining_budget is not None and scanned >= remaining_budget:
                return {
                    "scanned": scanned,
                    "complete": False,
                    "limit_reached": True,
                    "error": "",
                }

        if expected <= len(embedded) or not comment_id:
            return {
                "scanned": scanned,
                "complete": expected <= len(embedded),
                "limit_reached": False,
                "error": "",
            }

        cursor = 0
        for _ in range(self.max_reply_pages):
            result = await _browser_fetch_json(
                page,
                REPLY_COMMENT_PATH,
                {
                    "item_id": post["post_id"],
                    "comment_id": comment_id,
                    "count": 50,
                    "cursor": cursor,
                    "aid": 1988,
                },
                timeout_seconds=self.request_timeout_seconds,
            )
            data = as_dict(result.get("data"))
            status_code = data.get("status_code", data.get("statusCode", 0))
            if not data or status_code not in (None, 0):
                return {
                    "scanned": scanned,
                    "complete": False,
                    "limit_reached": False,
                    "error": text(
                        result.get("error")
                        or data.get("status_msg")
                        or f"reply_http_{result.get('http_status')}"
                    ),
                }
            rows = as_list(data.get("comments"))
            for reply in rows:
                if not isinstance(reply, dict):
                    continue
                reply_id = text(reply.get("cid") or reply.get("id"))
                if reply_id and reply_id in scanned_ids:
                    continue
                if reply_id:
                    scanned_ids.add(reply_id)
                scanned += 1
                self._record_if_target(
                    reply,
                    post=post,
                    target_comments=target_comments,
                    parent_comment_id=comment_id,
                )
                if remaining_budget is not None and scanned >= remaining_budget:
                    return {
                        "scanned": scanned,
                        "complete": False,
                        "limit_reached": True,
                        "error": "",
                    }
            has_more = bool(data.get("has_more", data.get("hasMore", False)))
            if not has_more:
                return {
                    "scanned": scanned,
                    "complete": True,
                    "limit_reached": False,
                    "error": "",
                }
            next_cursor = data.get("cursor")
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError):
                next_cursor = cursor + max(1, len(rows))
            if next_cursor <= cursor:
                return {
                    "scanned": scanned,
                    "complete": False,
                    "limit_reached": False,
                    "error": "reply_cursor_did_not_advance",
                }
            cursor = next_cursor
        return {
            "scanned": scanned,
            "complete": False,
            "limit_reached": False,
            "error": "reply_page_safety_limit",
        }

    async def _scan_post(
        self,
        page: Page,
        post: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        target_comments: list[dict[str, Any]] = []
        scanned_ids: set[str] = set()
        top_level_scanned = 0
        replies_scanned = 0
        cursor = 0
        complete = False
        limit_reached = False
        errors: list[str] = []
        reply_complete = True

        try:
            await page.goto(
                post["url"],
                wait_until="domcontentloaded",
                timeout=int(self.navigation_timeout_seconds * 1000),
            )
            await page.wait_for_timeout(1500)
        except Exception as exc:
            errors.append(f"navigation: {type(exc).__name__}: {exc}")

        for _ in range(self.max_comment_pages):
            result = await _browser_fetch_json(
                page,
                TOP_LEVEL_COMMENT_PATH,
                {
                    "aweme_id": post["post_id"],
                    "count": 50,
                    "cursor": cursor,
                    "aid": 1988,
                },
                timeout_seconds=self.request_timeout_seconds,
            )
            data = as_dict(result.get("data"))
            status_code = data.get("status_code", data.get("statusCode", 0))
            if not data or status_code not in (None, 0):
                errors.append(
                    text(
                        result.get("error")
                        or data.get("status_msg")
                        or f"comment_http_{result.get('http_status')}"
                    )
                )
                break

            rows = as_list(data.get("comments"))
            for comment in rows:
                if not isinstance(comment, dict):
                    continue
                comment_id = text(comment.get("cid") or comment.get("id"))
                if comment_id and comment_id in scanned_ids:
                    continue
                if comment_id:
                    scanned_ids.add(comment_id)
                top_level_scanned += 1
                self._record_if_target(
                    comment,
                    post=post,
                    target_comments=target_comments,
                )

                remaining_budget = None
                if self.max_comments_per_post:
                    remaining_budget = max(
                        0,
                        self.max_comments_per_post
                        - top_level_scanned
                        - replies_scanned,
                    )
                    if remaining_budget == 0:
                        limit_reached = True
                        break
                reply_result = await self._scan_replies(
                    page,
                    post=post,
                    top_comment=comment,
                    target_comments=target_comments,
                    scanned_ids=scanned_ids,
                    remaining_budget=remaining_budget,
                )
                replies_scanned += int(reply_result["scanned"])
                reply_complete = reply_complete and bool(reply_result["complete"])
                if reply_result["error"]:
                    errors.append(text(reply_result["error"]))
                if reply_result["limit_reached"]:
                    limit_reached = True
                    break
            if limit_reached:
                break

            has_more = bool(data.get("has_more", data.get("hasMore", False)))
            if not has_more:
                complete = True
                break
            next_cursor = data.get("cursor")
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError):
                next_cursor = cursor + max(1, len(rows))
            if next_cursor <= cursor:
                errors.append("comment_cursor_did_not_advance")
                break
            cursor = next_cursor
        else:
            errors.append("comment_page_safety_limit")

        coverage = {
            "post_id": post["post_id"],
            "url": post["url"],
            "owner_username": post["owner_username"],
            "top_level_comments_scanned": top_level_scanned,
            "replies_scanned": replies_scanned,
            "comments_scanned": top_level_scanned + replies_scanned,
            "target_comments_found": len(target_comments),
            "top_level_pagination_exhausted": complete,
            "reply_pagination_exhausted": reply_complete,
            "complete": complete and reply_complete and not errors,
            "limit_reached": limit_reached,
            "errors": list(dict.fromkeys(error for error in errors if error)),
        }
        return coverage, target_comments

    async def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(self.cdp_url)
            context = (
                browser.contexts[0]
                if browser.contexts
                else await browser.new_context()
            )
            page = await context.new_page()

            stage = time.perf_counter()
            cookies = await context.cookies(["https://www.tiktok.com"])
            cookie_names = {text(cookie.get("name")) for cookie in cookies}
            auth_markers = sorted(cookie_names & AUTH_COOKIE_NAMES)
            timings["anonymous_session_check"] = time.perf_counter() - stage

            stage = time.perf_counter()
            discovery_task = asyncio.create_task(
                self._discover_candidates(page)
            )
            done, _ = await asyncio.wait(
                {discovery_task},
                timeout=self.discovery_timeout_seconds,
            )
            if discovery_task in done:
                candidates, discovery = discovery_task.result()
            else:
                discovery_task.cancel()
                try:
                    await page.close()
                except Exception:
                    pass
                page = await context.new_page()
                await asyncio.wait({discovery_task}, timeout=2)
                if not discovery_task.done():
                    discovery_task.add_done_callback(
                        lambda task: (
                            None if task.cancelled() else task.exception()
                        )
                    )
                candidates = []
                discovery = {
                    "search_query": self.username,
                    "search_api_candidates": 0,
                    "search_dom_candidates": 0,
                    "candidate_count_before_owner_filter": 0,
                    "search_error": (
                        "Username pre-discovery exceeded the whole-operation "
                        f"timeout of {self.discovery_timeout_seconds:g} seconds."
                    ),
                }
            timings["username_pre_discovery"] = time.perf_counter() - stage

            stage = time.perf_counter()
            for candidate in candidates:
                await _hydrate_owner(
                    context,
                    candidate,
                    timeout_seconds=self.request_timeout_seconds,
                )
            target_owned = [
                candidate
                for candidate in candidates
                if normalize_username(candidate.get("owner_username"))
                == normalize_username(self.username)
            ]
            unknown_owner = [
                candidate
                for candidate in candidates
                if not normalize_username(candidate.get("owner_username"))
            ]
            eligible = [
                candidate
                for candidate in candidates
                if normalize_username(candidate.get("owner_username"))
                and normalize_username(candidate.get("owner_username"))
                != normalize_username(self.username)
            ]
            eligible = eligible[: self.max_posts]
            timings["candidate_owner_filter"] = time.perf_counter() - stage

            stage = time.perf_counter()
            post_coverage: list[dict[str, Any]] = []
            target_comments: list[dict[str, Any]] = []
            for candidate in eligible:
                coverage, matches = await self._scan_post(page, candidate)
                post_coverage.append(coverage)
                target_comments.extend(matches)
            timings["comment_pool_scan"] = time.perf_counter() - stage

            await page.close()
            await browser.close()

        timings = {key: round(value, 3) for key, value in timings.items()}
        timings["total"] = round(time.perf_counter() - started, 3)
        comments_scanned = sum(
            int(item["comments_scanned"]) for item in post_coverage
        )
        complete_posts = sum(bool(item["complete"]) for item in post_coverage)
        if discovery["search_error"] and not candidates:
            status = "blocked"
        elif not candidates:
            status = "no_candidates_observed"
        else:
            status = "available"
        return {
            "schema_version": "1.0",
            "generated_at": datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
            "status": status,
            "scope": {
                "platform": "tiktok",
                "pre_discovery_seed_type": "username",
                "pre_discovery_seed": self.username,
                "source": "live_public_fetch",
                "local_comment_files_used": False,
                "authenticated": bool(auth_markers),
                "auth_cookie_names_present": auth_markers,
                "target_post_ownership_policy": (
                    "exclude posts owned by the target; retain only target-authored "
                    "comments under other users' posts"
                ),
            },
            "discovery": {
                **discovery,
                "target_owned_posts_excluded": len(target_owned),
                "unknown_owner_posts_excluded": len(unknown_owner),
                "eligible_other_user_posts": len(eligible),
                "post_limit": self.max_posts,
            },
            "coverage": {
                "posts_scanned": len(post_coverage),
                "posts_complete": complete_posts,
                "comments_and_replies_scanned": comments_scanned,
                "target_comments_found": len(target_comments),
                "all_scanned_posts_complete": (
                    bool(post_coverage)
                    and complete_posts == len(post_coverage)
                ),
                "claim": (
                    "Every exact target-author match observed in the live public "
                    "comment pools of username-discovered, non-target-owned posts."
                ),
                "post_results": post_coverage,
            },
            "target_identity": {
                "username": self.username,
                "learned_public_identifiers": sorted(self.target_identifiers),
            },
            "comments": target_comments,
            "timings_seconds": timings,
        }
