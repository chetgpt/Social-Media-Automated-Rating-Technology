"""Trace a user's public comments by searching the live platform first.

The tracer starts from an explicit username, account ID, or profile URL. It
never imports candidate posts or comment text from a local corpus. Platforms
with an author timeline use it directly; post-indexed platforms search for
candidate posts and verify matching comment authors inside each live pool.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from tiktok_scraper.profile_contract import (
    canonical_profile_url,
    normalize_platform,
    normalize_user_id,
    normalize_username,
    text_value,
)


AUTHOR_INDEX_PLATFORMS = {"x"}
CANDIDATE_POOL_PLATFORMS = {
    "youtube",
    "instagram",
    "facebook",
    "tiktok",
}
SUPPORTED_PLATFORMS = AUTHOR_INDEX_PLATFORMS | CANDIDATE_POOL_PLATFORMS


def text(value: Any) -> str:
    return text_value(value)


def normalized_username(platform: str, value: Any) -> str:
    return normalize_username(platform, value).casefold()


def _profile_target_parts(platform: str, value: str) -> tuple[str, str]:
    raw = text(value)
    if not raw:
        return "", ""
    if not raw.startswith(("http://", "https://")):
        return normalize_username(platform, raw), ""

    parsed = urlparse(raw)
    parts = [part for part in parsed.path.split("/") if part]
    if platform == "tiktok":
        username = parts[0].lstrip("@") if parts else ""
        return normalize_username(platform, username), ""
    if platform == "youtube":
        if len(parts) >= 2 and parts[0].casefold() == "channel":
            return "", normalize_user_id(platform, parts[1])
        if parts and parts[0].startswith("@"):
            return normalize_username(platform, parts[0]), ""
        if len(parts) >= 2 and parts[0].casefold() in {"c", "user"}:
            return normalize_username(platform, parts[1]), ""
        return "", ""
    if platform in {"instagram", "x"}:
        return (
            normalize_username(platform, parts[0]) if parts else "",
            "",
        )
    if platform == "facebook":
        query_id = parse_qs(parsed.query).get("id", [""])[0]
        if query_id:
            return "", normalize_user_id(platform, query_id)
        if parts and parts[0].casefold() not in {
            "profile.php",
            "people",
            "pages",
        }:
            return normalize_username(platform, parts[0]), ""
        if len(parts) >= 3 and parts[0].casefold() == "people":
            return (
                normalize_username(platform, parts[1]),
                normalize_user_id(platform, parts[2]),
            )
    return "", ""


@dataclass
class AuthorIdentity:
    platform: str
    input_value: str
    username: str = ""
    user_id: str = ""
    usernames: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)

    @classmethod
    def from_target(
        cls,
        *,
        platform: str,
        target: str,
        username: str = "",
        user_id: str = "",
    ) -> "AuthorIdentity":
        normalized_platform = normalize_platform(platform)
        if normalized_platform not in SUPPORTED_PLATFORMS:
            raise ValueError(
                "Platform must be one of: "
                + ", ".join(sorted(SUPPORTED_PLATFORMS))
            )
        target_username, target_id = _profile_target_parts(
            normalized_platform,
            target,
        )
        resolved_username = normalize_username(
            normalized_platform,
            username or target_username,
        )
        resolved_id = normalize_user_id(
            normalized_platform,
            user_id or target_id,
        )
        if not (resolved_username or resolved_id):
            raise ValueError(
                "Target must provide a username, account ID, or profile URL"
            )
        identity = cls(
            platform=normalized_platform,
            input_value=text(target or username or user_id),
            username=resolved_username,
            user_id=resolved_id,
        )
        if resolved_username:
            identity.usernames.add(resolved_username.casefold())
        if resolved_id:
            identity.identifiers.add(resolved_id)
        return identity

    def learn(
        self,
        *,
        usernames: Iterable[Any] = (),
        identifiers: Iterable[Any] = (),
    ) -> None:
        for value in usernames:
            username = normalize_username(self.platform, value)
            if username:
                self.usernames.add(username.casefold())
                if not self.username:
                    self.username = username
        for value in identifiers:
            identifier = normalize_user_id(self.platform, value)
            if identifier:
                self.identifiers.add(identifier)
                if not self.user_id:
                    self.user_id = identifier

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "input": self.input_value,
            "username": self.username,
            "user_id": self.user_id,
            "known_usernames": sorted(self.usernames),
            "known_public_identifiers": sorted(self.identifiers),
            "profile_url": canonical_profile_url(
                self.platform,
                self.username,
                self.user_id,
            ),
        }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _author_containers(record: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [record]
    for key in (
        "author_profile",
        "user",
        "owner",
        "from",
        "commenter",
        "actor",
        "profile",
        "x_api_author",
        "x_graphql_author",
    ):
        value = record.get(key)
        if isinstance(value, dict):
            containers.append(value)
            legacy = value.get("legacy")
            core = value.get("core")
            if isinstance(legacy, dict):
                containers.append(legacy)
            if isinstance(core, dict):
                containers.append(core)
    return containers


def comment_author(record: dict[str, Any], platform: str) -> dict[str, Any]:
    containers = _author_containers(record)
    usernames: set[str] = set()
    identifiers: set[str] = set()
    display_names: list[str] = []

    top_author = record.get("author")
    if isinstance(top_author, str):
        username = normalize_username(platform, top_author)
        if username:
            usernames.add(username.casefold())
    for container in containers:
        for key in (
            "username",
            "unique_id",
            "uniqueId",
            "screen_name",
            "handle",
            "author",
        ):
            value = container.get(key)
            if isinstance(value, str):
                username = normalize_username(platform, value)
                if username:
                    usernames.add(username.casefold())
        for key in (
            "author_id",
            "author_user_id",
            "user_id",
            "uid",
            "id",
            "pk",
            "profile_id",
            "rest_id",
            "sec_uid",
            "secUid",
            "channel",
        ):
            identifier = normalize_user_id(platform, container.get(key))
            if identifier:
                identifiers.add(identifier)
        for key in (
            "display_name",
            "displayName",
            "full_name",
            "name",
            "nickname",
            "author_display_name",
        ):
            value = text(container.get(key))
            if value and value not in display_names:
                display_names.append(value)
    return {
        "username": sorted(usernames)[0] if usernames else "",
        "usernames": sorted(usernames),
        "identifiers": sorted(identifiers),
        "display_name": display_names[0] if display_names else "",
    }


def match_author(
    identity: AuthorIdentity,
    record: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    author = comment_author(record, identity.platform)
    author_ids = set(author["identifiers"])
    author_usernames = set(author["usernames"])
    if identity.identifiers.intersection(author_ids):
        identity.learn(
            usernames=author_usernames,
            identifiers=author_ids,
        )
        return "stable_id", author
    if identity.usernames.intersection(author_usernames):
        identity.learn(
            usernames=author_usernames,
            identifiers=author_ids,
        )
        return "exact_username", author
    return "", author


def candidate_owner(
    candidate: dict[str, Any],
    platform: str,
) -> dict[str, set[str]]:
    identifiers = {
        normalize_user_id(platform, value)
        for value in (
            candidate.get("creator_id"),
            candidate.get("owner_id"),
            candidate.get("author_id"),
            _dict(candidate.get("creator_profile")).get("user_id"),
            _dict(candidate.get("creator_profile")).get("id"),
        )
        if normalize_user_id(platform, value)
    }
    usernames = {
        normalized_username(platform, value)
        for value in (
            candidate.get("username"),
            candidate.get("owner_username"),
            candidate.get("content_creator"),
            _dict(candidate.get("creator_profile")).get("username"),
        )
        if normalized_username(platform, value)
    }
    return {
        "identifiers": identifiers,
        "usernames": usernames,
    }


def is_target_owned(
    identity: AuthorIdentity,
    candidate: dict[str, Any],
) -> bool:
    owner = candidate_owner(candidate, identity.platform)
    return bool(
        identity.identifiers.intersection(owner["identifiers"])
        or identity.usernames.intersection(owner["usernames"])
    )


def iter_comments(values: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for value in values:
        if not isinstance(value, dict):
            continue
        yield value
        replies = value.get("replies")
        if isinstance(replies, list):
            yield from iter_comments(replies)


def normalize_comment(
    record: dict[str, Any],
    *,
    identity: AuthorIdentity,
    author: dict[str, Any],
    match_basis: str,
    source_post: dict[str, Any],
) -> dict[str, Any]:
    comment_id = text(
        record.get("comment_id")
        or record.get("id")
        or record.get("cid")
        or record.get("video_id")
    )
    parent_id = text(
        record.get("parent_comment_id")
        or record.get("parent_id")
        or record.get("reply_to_post_id")
        or record.get("in_reply_to_post_id")
    )
    return {
        "platform": identity.platform,
        "comment_id": comment_id,
        "parent_comment_id": parent_id,
        "text": text(
            record.get("text")
            or record.get("comment_text")
            or record.get("caption")
            or record.get("title")
        ),
        "created_at": text(
            record.get("created_at")
            or record.get("time")
            or record.get("create_time")
            or record.get("published_at")
        ),
        "like_count": (
            record.get("like_count")
            if record.get("like_count") is not None
            else record.get("likes")
        ),
        "reply_count": record.get("reply_count"),
        "is_reply": bool(
            record.get("is_reply")
            or parent_id
            or record.get("in_reply_to_user_id")
        ),
        "url": text(record.get("url")),
        "match_basis": match_basis,
        "author": {
            "username": author["username"],
            "display_name": author["display_name"],
            "public_identifiers": author["identifiers"],
        },
        "source_post": {
            "post_id": text(
                source_post.get("video_id")
                or source_post.get("post_id")
                or source_post.get("id")
            ),
            "url": text(source_post.get("url")),
            "owner_username": text(
                source_post.get("username")
                or source_post.get("owner_username")
                or source_post.get("content_creator")
            ),
            "owner_id": text(
                source_post.get("creator_id")
                or source_post.get("owner_id")
            ),
            "caption": text(
                source_post.get("caption")
                or source_post.get("title")
            ),
        },
    }


def _dedupe_candidates(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        marker = text(
            candidate.get("video_id")
            or candidate.get("post_id")
            or candidate.get("id")
            or candidate.get("url")
        )
        if not marker or marker in seen:
            continue
        seen.add(marker)
        output.append(candidate)
    return output


def _parent_post_id(candidate: dict[str, Any]) -> str:
    direct = text(
        candidate.get("in_reply_to_post_id")
        or candidate.get("reply_to_post_id")
    )
    if direct:
        return direct
    references = candidate.get("referenced_tweets")
    if isinstance(references, list):
        for reference in references:
            if (
                isinstance(reference, dict)
                and reference.get("type") == "replied_to"
            ):
                return text(reference.get("id"))
    return ""


def candidate_pool_status(
    *,
    candidate_count: int,
    available_posts: int,
    error_posts: int,
    discovery_error: str = "",
) -> str:
    if not candidate_count:
        return "blocked" if discovery_error else "no_candidates_observed"
    if available_posts and error_posts:
        return "partial"
    if available_posts:
        return "available"
    if error_posts:
        return "blocked"
    return "no_comments_observed"


def all_attempted_posts_complete(
    post_results: Iterable[dict[str, Any]],
) -> bool:
    attempted = [
        row
        for row in post_results
        if row.get("status") != "self_owned_excluded"
    ]
    return bool(attempted) and all(
        row.get("status") == "available" and bool(row.get("complete"))
        for row in attempted
    )


class AuthorActivityTracer:
    def __init__(
        self,
        *,
        platform: str,
        target: str,
        username: str = "",
        user_id: str = "",
        cdp_url: str = "",
        browser_channel: str = "msedge",
        headless: bool = True,
        max_posts: int = 20,
        max_comments_per_post: int = 0,
        discovery_timeout_seconds: float = 90,
        post_timeout_seconds: float = 180,
        max_search_pages: int = 3,
        x_search_mode: str = "recent",
    ) -> None:
        self.identity = AuthorIdentity.from_target(
            platform=platform,
            target=target,
            username=username,
            user_id=user_id,
        )
        self.cdp_url = text(cdp_url)
        self.browser_channel = text(browser_channel)
        self.headless = bool(headless)
        self.max_posts = max(0, int(max_posts))
        self.max_comments_per_post = max(
            0,
            int(max_comments_per_post),
        )
        self.discovery_timeout_seconds = max(
            5.0,
            float(discovery_timeout_seconds),
        )
        self.post_timeout_seconds = max(
            10.0,
            float(post_timeout_seconds),
        )
        self.max_search_pages = max(1, int(max_search_pages))
        self.x_search_mode = text(x_search_mode) or "recent"
        self._seen_comment_markers: set[str] = set()

    def _search_seed(self) -> str:
        return (
            self.identity.username
            or self.identity.user_id
            or self.identity.input_value
        )

    def _search_queries(self) -> list[str]:
        username = self.identity.username
        user_id = self.identity.user_id
        platform = self.identity.platform
        values: list[str] = []
        if platform == "youtube" and username:
            values.extend((f"@{username}", username))
        elif username:
            values.append(username)
        if user_id and user_id not in values:
            values.append(user_id)
        return list(dict.fromkeys(value for value in values if value))

    def _comment_marker(
        self,
        comment: dict[str, Any],
    ) -> str:
        comment_id = text(comment.get("comment_id"))
        if comment_id:
            return f"{self.identity.platform}:{comment_id}"
        source = _dict(comment.get("source_post"))
        return "\x1f".join(
            (
                self.identity.platform,
                text(source.get("post_id")),
                text(comment.get("author", {}).get("username"))
                if isinstance(comment.get("author"), dict)
                else "",
                text(comment.get("text")),
                text(comment.get("created_at")),
            )
        )

    def _retain(
        self,
        comment: dict[str, Any],
        output: list[dict[str, Any]],
    ) -> None:
        marker = self._comment_marker(comment)
        if marker in self._seen_comment_markers:
            return
        self._seen_comment_markers.add(marker)
        output.append(comment)

    async def run(self) -> dict[str, Any]:
        if self.identity.platform == "x":
            return await self._run_x()
        if self.identity.platform == "tiktok":
            return await self._run_tiktok()
        return await self._run_candidate_pool_platform()

    async def _open_browser(self):
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        external = False
        owns_context = False
        if self.cdp_url:
            browser = await playwright.chromium.connect_over_cdp(
                self.cdp_url
            )
            external = True
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context(locale="en-US")
                owns_context = True
        else:
            launch_options: dict[str, Any] = {
                "headless": self.headless,
            }
            if self.browser_channel:
                launch_options["channel"] = self.browser_channel
            browser = await playwright.chromium.launch(**launch_options)
            context = await browser.new_context(locale="en-US")
            owns_context = True
        return playwright, browser, context, external, owns_context

    @staticmethod
    async def _close_browser(
        playwright: Any,
        browser: Any,
        context: Any,
        *,
        external: bool,
        owns_context: bool,
    ) -> None:
        if owns_context and context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if not external and browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    def _base_payload(
        self,
        *,
        status: str,
        coverage_mode: str,
        discovery: dict[str, Any],
        coverage: dict[str, Any],
        comments: list[dict[str, Any]],
        timings: dict[str, float],
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generated_at": dt.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
            "status": status,
            "scope": {
                "platform": self.identity.platform,
                "start_direction": "author_first_live_platform_search",
                "seed": self._search_seed(),
                "seed_types": [
                    key
                    for key, value in (
                        ("username", self.identity.username),
                        ("user_id", self.identity.user_id),
                        ("profile_url", self.identity.input_value)
                        if self.identity.input_value.startswith(
                            ("http://", "https://")
                        )
                        else ("profile_url", ""),
                    )
                    if value
                ],
                "source": "live_platform_fetch",
                "local_comment_files_used": False,
                "target_post_policy": (
                    "exclude target-owned posts and retain comments or replies "
                    "authored by the target under other users' posts"
                ),
                "coverage_mode": coverage_mode,
            },
            "target_identity": self.identity.as_dict(),
            "discovery": discovery,
            "coverage": coverage,
            "comments": comments,
            "timings_seconds": {
                key: round(value, 3)
                for key, value in timings.items()
            },
            "error": error,
        }

    async def _run_candidate_pool_platform(self) -> dict[str, Any]:
        platform = self.identity.platform
        started = time.perf_counter()
        timings: dict[str, float] = {}
        discovery_error = ""
        browser_resources: tuple[Any, Any, Any, bool, bool] | None = None
        context = None

        if platform == "youtube":
            from tiktok_scraper.scrapers.youtube_scraper import YouTubeScraper

            scraper: Any = YouTubeScraper()
        elif platform == "instagram":
            from tiktok_scraper.scrapers.instagram_scraper import (
                InstagramScraper,
            )

            scraper = InstagramScraper()
        elif platform == "facebook":
            from tiktok_scraper.scrapers.facebook_scraper import (
                FacebookScraper,
            )

            scraper = FacebookScraper()
        else:
            raise ValueError(f"Unsupported candidate-pool platform: {platform}")

        try:
            if platform in {"instagram", "facebook"}:
                browser_resources = await self._open_browser()
                context = browser_resources[2]

            stage = time.perf_counter()
            discovered: list[dict[str, Any]] = []
            query_results: list[dict[str, Any]] = []
            per_query_limit = (
                max(10, self.max_posts * 3)
                if self.max_posts
                else 0
            )
            for query in self._search_queries():
                try:
                    rows = await asyncio.wait_for(
                        scraper.search(
                            query,
                            max_videos=per_query_limit,
                            context=context,
                        ),
                        timeout=self.discovery_timeout_seconds,
                    )
                except Exception as exc:
                    query_results.append(
                        {
                            "query": query,
                            "status": "error",
                            "candidate_count": 0,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    discovery_error = (
                        discovery_error
                        or f"{type(exc).__name__}: {exc}"
                    )
                    continue
                rows = [
                    row for row in rows if isinstance(row, dict)
                ]
                query_results.append(
                    {
                        "query": query,
                        "status": "available",
                        "candidate_count": len(rows),
                        "error": "",
                    }
                )
                discovered.extend(rows)
            candidates = _dedupe_candidates(discovered)
            timings["platform_search"] = time.perf_counter() - stage

            known_self_owned = [
                candidate
                for candidate in candidates
                if is_target_owned(self.identity, candidate)
            ]
            eligible = [
                candidate
                for candidate in candidates
                if not is_target_owned(self.identity, candidate)
            ]
            if self.max_posts:
                eligible = eligible[: self.max_posts]

            stage = time.perf_counter()
            post_results: list[dict[str, Any]] = []
            matched_comments: list[dict[str, Any]] = []
            for candidate in eligible:
                post_started = time.perf_counter()
                try:
                    extraction = await asyncio.wait_for(
                        scraper.extract_comments(
                            candidate,
                            max_comments=self.max_comments_per_post,
                            context=context,
                        ),
                        timeout=self.post_timeout_seconds,
                    )
                except Exception as exc:
                    post_results.append(
                        {
                            "post_id": text(
                                candidate.get("video_id")
                                or candidate.get("id")
                            ),
                            "url": text(candidate.get("url")),
                            "status": "error",
                            "comments_scanned": 0,
                            "target_matches": 0,
                            "complete": False,
                            "limit_reached": False,
                            "elapsed_seconds": round(
                                time.perf_counter() - post_started,
                                3,
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                combined_owner = {
                    **candidate,
                    "creator_id": (
                        extraction.get("creator_id")
                        or candidate.get("creator_id")
                    ),
                    "username": (
                        extraction.get("username")
                        or candidate.get("username")
                    ),
                    "creator_profile": (
                        extraction.get("creator_profile")
                        or candidate.get("creator_profile")
                    ),
                }
                if is_target_owned(self.identity, combined_owner):
                    post_results.append(
                        {
                            "post_id": text(
                                candidate.get("video_id")
                                or candidate.get("id")
                            ),
                            "url": text(candidate.get("url")),
                            "status": "self_owned_excluded",
                            "comments_scanned": 0,
                            "target_matches": 0,
                            "complete": False,
                            "limit_reached": False,
                            "elapsed_seconds": round(
                                time.perf_counter() - post_started,
                                3,
                            ),
                            "error": "",
                        }
                    )
                    continue

                comments = list(
                    iter_comments(extraction.get("comments") or [])
                )
                before = len(matched_comments)
                basis_counts: dict[str, int] = {
                    "stable_id": 0,
                    "exact_username": 0,
                }
                for record in comments:
                    basis, author = match_author(self.identity, record)
                    if not basis:
                        continue
                    normalized = normalize_comment(
                        record,
                        identity=self.identity,
                        author=author,
                        match_basis=basis,
                        source_post=combined_owner,
                    )
                    marker_before = len(matched_comments)
                    self._retain(normalized, matched_comments)
                    if len(matched_comments) > marker_before:
                        basis_counts[basis] += 1
                comments_seen = int(
                    extraction.get("comments_seen_in_response")
                    if extraction.get("comments_seen_in_response")
                    is not None
                    else len(comments)
                )
                error = text(
                    extraction.get("error")
                    or extraction.get("comment_error")
                )
                post_results.append(
                    {
                        "post_id": text(
                            extraction.get("video_id")
                            or candidate.get("video_id")
                            or candidate.get("id")
                        ),
                        "url": text(
                            extraction.get("url")
                            or candidate.get("url")
                        ),
                        "status": (
                            "available"
                            if comments_seen or not error
                            else "error"
                        ),
                        "comments_scanned": comments_seen,
                        "target_matches": (
                            len(matched_comments) - before
                        ),
                        "match_basis_counts": basis_counts,
                        "complete": (
                            extraction.get("comments_exhausted") is True
                        ),
                        "limit_reached": bool(
                            extraction.get("comment_limit_reached")
                        ),
                        "comment_method": text(
                            extraction.get("comment_method")
                        ),
                        "elapsed_seconds": round(
                            time.perf_counter() - post_started,
                            3,
                        ),
                        "error": error,
                    }
                )
            timings["comment_pool_scan"] = time.perf_counter() - stage
        finally:
            if browser_resources is not None:
                await self._close_browser(
                    *browser_resources[:3],
                    external=browser_resources[3],
                    owns_context=browser_resources[4],
                )

        timings["total"] = time.perf_counter() - started
        posts_scanned = sum(
            row.get("status") == "available" for row in post_results
        )
        error_posts = sum(
            row.get("status") == "error" for row in post_results
        )
        comments_scanned = sum(
            int(row.get("comments_scanned") or 0)
            for row in post_results
        )
        complete_posts = sum(
            bool(row.get("complete")) for row in post_results
        )
        status = candidate_pool_status(
            candidate_count=len(candidates),
            available_posts=posts_scanned,
            error_posts=error_posts,
            discovery_error=discovery_error,
        )

        return self._base_payload(
            status=status,
            coverage_mode="search_discovered_post_pools",
            discovery={
                "seed_is_author_identity": True,
                "queries": query_results,
                "candidate_count": len(candidates),
                "known_target_owned_candidates_excluded": len(
                    known_self_owned
                ),
                "eligible_candidates_selected": len(eligible),
                "post_limit": self.max_posts,
                "error": discovery_error,
            },
            coverage={
                "posts_scanned": posts_scanned,
                "posts_failed": error_posts,
                "posts_complete": complete_posts,
                "comments_and_replies_scanned": comments_scanned,
                "target_comments_found": len(matched_comments),
                "all_scanned_posts_complete": (
                    all_attempted_posts_complete(post_results)
                ),
                "claim": (
                    "Every exact target-author match observed in the live "
                    "comment pools of platform-search-discovered, "
                    "non-target-owned posts. This is not a platform-wide "
                    "completeness claim because the platform lacks a public "
                    "author-to-comment index."
                ),
                "post_results": post_results,
            },
            comments=matched_comments,
            timings=timings,
            error=discovery_error if status == "blocked" else "",
        )

    async def _run_tiktok(self) -> dict[str, Any]:
        from tiktok_scraper.username_comment_trace import (
            TikTokUsernameCommentTracer,
            _hydrate_owner,
        )

        started = time.perf_counter()
        timings: dict[str, float] = {}
        resources = await self._open_browser()
        context = resources[2]
        page = await context.new_page()
        tracer = TikTokUsernameCommentTracer(
            username=self._search_seed(),
            cdp_url=self.cdp_url,
            max_search_pages=self.max_search_pages,
            max_posts=self.max_posts or 100_000,
            max_comments_per_post=self.max_comments_per_post,
            max_comment_pages=100,
            max_reply_pages=100,
            request_timeout_seconds=min(
                30.0,
                self.post_timeout_seconds,
            ),
            navigation_timeout_seconds=min(
                60.0,
                self.post_timeout_seconds,
            ),
            discovery_timeout_seconds=self.discovery_timeout_seconds,
        )
        tracer.target_identifiers.update(self.identity.identifiers)
        discovery_error = ""
        candidates: list[dict[str, Any]] = []
        discovery: dict[str, Any] = {}
        post_results: list[dict[str, Any]] = []
        matched_comments: list[dict[str, Any]] = []
        auth_markers: list[str] = []

        try:
            cookies = await context.cookies(["https://www.tiktok.com"])
            cookie_names = {text(cookie.get("name")) for cookie in cookies}
            auth_markers = sorted(
                cookie_names
                & {
                    "sessionid",
                    "sessionid_ss",
                    "sid_tt",
                    "sid_guard",
                }
            )
            stage = time.perf_counter()
            try:
                candidates, discovery = await asyncio.wait_for(
                    tracer._discover_candidates(page),
                    timeout=self.discovery_timeout_seconds,
                )
            except Exception as exc:
                discovery_error = f"{type(exc).__name__}: {exc}"
                discovery = {
                    "search_query": self._search_seed(),
                    "candidate_count_before_owner_filter": 0,
                    "search_error": discovery_error,
                }
            timings["platform_search"] = time.perf_counter() - stage
            discovery_error = (
                discovery_error or text(discovery.get("search_error"))
            )

            for candidate in candidates:
                await _hydrate_owner(
                    context,
                    candidate,
                    timeout_seconds=min(
                        30.0,
                        self.post_timeout_seconds,
                    ),
                )
            known_self_owned = [
                candidate
                for candidate in candidates
                if is_target_owned(self.identity, candidate)
            ]
            eligible = [
                candidate
                for candidate in candidates
                if not is_target_owned(self.identity, candidate)
            ]
            if self.max_posts:
                eligible = eligible[: self.max_posts]

            stage = time.perf_counter()
            for candidate in eligible:
                post_started = time.perf_counter()
                try:
                    coverage, comments = await asyncio.wait_for(
                        tracer._scan_post(page, candidate),
                        timeout=self.post_timeout_seconds,
                    )
                except Exception as exc:
                    post_results.append(
                        {
                            "post_id": text(candidate.get("post_id")),
                            "url": text(candidate.get("url")),
                            "status": "error",
                            "comments_scanned": 0,
                            "target_matches": 0,
                            "complete": False,
                            "limit_reached": False,
                            "elapsed_seconds": round(
                                time.perf_counter() - post_started,
                                3,
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                before = len(matched_comments)
                basis_counts = {
                    "stable_id": 0,
                    "exact_username": 0,
                }
                for comment in comments:
                    author_data = _dict(comment.get("author"))
                    author_ids = {
                        text(value)
                        for value in author_data.get(
                            "public_identifiers",
                            [],
                        )
                        if text(value)
                    }
                    author_username = normalize_username(
                        "tiktok",
                        author_data.get("username"),
                    )
                    if self.identity.identifiers.intersection(author_ids):
                        basis = "stable_id"
                    elif (
                        author_username
                        and author_username.casefold()
                        in self.identity.usernames
                    ):
                        basis = "exact_username"
                    else:
                        continue
                    self.identity.learn(
                        usernames=[author_username],
                        identifiers=author_ids,
                    )
                    comment["match_basis"] = basis
                    marker_before = len(matched_comments)
                    self._retain(comment, matched_comments)
                    if len(matched_comments) > marker_before:
                        basis_counts[basis] += 1

                post_results.append(
                    {
                        "post_id": text(candidate.get("post_id")),
                        "url": text(candidate.get("url")),
                        "status": "available"
                        if coverage.get("comments_scanned")
                        or not coverage.get("errors")
                        else "error",
                        "comments_scanned": int(
                            coverage.get("comments_scanned") or 0
                        ),
                        "target_matches": (
                            len(matched_comments) - before
                        ),
                        "match_basis_counts": basis_counts,
                        "complete": bool(coverage.get("complete")),
                        "limit_reached": bool(
                            coverage.get("limit_reached")
                        ),
                        "comment_method": "tiktok_web_comment_api",
                        "elapsed_seconds": round(
                            time.perf_counter() - post_started,
                            3,
                        ),
                        "error": "; ".join(
                            text(error)
                            for error in coverage.get("errors", [])
                            if text(error)
                        ),
                    }
                )
            timings["comment_pool_scan"] = time.perf_counter() - stage
        finally:
            try:
                await page.close()
            except Exception:
                pass
            await self._close_browser(
                *resources[:3],
                external=resources[3],
                owns_context=resources[4],
            )

        self.identity.learn(
            identifiers=tracer.target_identifiers,
        )
        timings["total"] = time.perf_counter() - started
        posts_scanned = sum(
            row.get("status") == "available" for row in post_results
        )
        error_posts = sum(
            row.get("status") == "error" for row in post_results
        )
        complete_posts = sum(
            bool(row.get("complete")) for row in post_results
        )
        comments_scanned = sum(
            int(row.get("comments_scanned") or 0)
            for row in post_results
        )
        status = candidate_pool_status(
            candidate_count=len(candidates),
            available_posts=posts_scanned,
            error_posts=error_posts,
            discovery_error=discovery_error,
        )
        return self._base_payload(
            status=status,
            coverage_mode="search_discovered_post_pools",
            discovery={
                **discovery,
                "seed_is_author_identity": True,
                "known_target_owned_candidates_excluded": len(
                    known_self_owned
                ),
                "eligible_candidates_selected": len(eligible),
                "post_limit": self.max_posts,
            },
            coverage={
                "posts_scanned": posts_scanned,
                "posts_failed": error_posts,
                "posts_complete": complete_posts,
                "comments_and_replies_scanned": comments_scanned,
                "target_comments_found": len(matched_comments),
                "all_scanned_posts_complete": (
                    all_attempted_posts_complete(post_results)
                ),
                "claim": (
                    "Every exact target-author match observed in the live "
                    "comment pools of TikTok-search-discovered, "
                    "non-target-owned posts. This is not a platform-wide "
                    "completeness claim."
                ),
                "post_results": post_results,
            },
            comments=matched_comments,
            timings=timings,
            error=discovery_error if status == "blocked" else "",
        )

    async def _run_x(self) -> dict[str, Any]:
        from tiktok_scraper.scrapers.x_scraper import XAPIError, XScraper

        started = time.perf_counter()
        timings: dict[str, float] = {}
        scraper = XScraper(search_mode=self.x_search_mode)
        try:
            scraper._require_token()
        except XAPIError as exc:
            timings["total"] = time.perf_counter() - started
            return self._base_payload(
                status="credentials_required",
                coverage_mode="native_author_timeline",
                discovery={
                    "seed_is_author_identity": True,
                    "timeline_endpoint_attempted": False,
                },
                coverage={
                    "posts_scanned": 0,
                    "posts_complete": 0,
                    "comments_and_replies_scanned": 0,
                    "target_comments_found": 0,
                    "claim": (
                        "No X author timeline was queried because an official "
                        "app bearer token is not configured."
                    ),
                    "post_results": [],
                },
                comments=[],
                timings=timings,
                error=str(exc),
            )

        stage = time.perf_counter()
        try:
            if not self.identity.user_id:
                payload = await asyncio.to_thread(
                    scraper._request_json,
                    f"{scraper.API_BASE}/users/by/username/"
                    f"{self.identity.username}",
                    params={
                        "user.fields": (
                            "id,name,username,protected,verified"
                        )
                    },
                    resource_type="users",
                )
                user = _dict(payload.get("data"))
                if not user.get("id"):
                    raise XAPIError("X user lookup returned no user")
                self.identity.learn(
                    usernames=[user.get("username")],
                    identifiers=[user.get("id")],
                )

            candidates, exhausted = await asyncio.to_thread(
                self._x_user_timeline_sync,
                scraper,
            )
        except Exception as exc:
            timings["native_author_timeline"] = (
                time.perf_counter() - stage
            )
            timings["total"] = time.perf_counter() - started
            return self._base_payload(
                status="blocked",
                coverage_mode="native_author_timeline",
                discovery={
                    "seed_is_author_identity": True,
                    "timeline_endpoint_attempted": True,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                coverage={
                    "posts_scanned": 0,
                    "posts_complete": 0,
                    "comments_and_replies_scanned": 0,
                    "target_comments_found": 0,
                    "claim": "The X author timeline request failed.",
                    "post_results": [],
                },
                comments=[],
                timings=timings,
                error=f"{type(exc).__name__}: {exc}",
            )
        timings["native_author_timeline"] = time.perf_counter() - stage

        comments: list[dict[str, Any]] = []
        own_replies_excluded = 0
        non_reply_posts_excluded = 0
        for candidate in candidates:
            parent_id = _parent_post_id(candidate)
            if not parent_id:
                non_reply_posts_excluded += 1
                continue
            if (
                self.identity.user_id
                and text(candidate.get("in_reply_to_user_id"))
                == self.identity.user_id
            ):
                own_replies_excluded += 1
                continue
            basis, author = match_author(self.identity, candidate)
            if not basis:
                continue
            record = {
                **candidate,
                "comment_id": candidate.get("video_id"),
                "parent_comment_id": parent_id,
                "is_reply": True,
            }
            source = {
                "video_id": parent_id,
                "url": f"https://x.com/i/status/{parent_id}",
                "creator_id": candidate.get("in_reply_to_user_id"),
            }
            normalized = normalize_comment(
                record,
                identity=self.identity,
                author=author,
                match_basis=basis,
                source_post=source,
            )
            self._retain(normalized, comments)
        timings["total"] = time.perf_counter() - started

        return self._base_payload(
            status="available",
            coverage_mode="native_author_timeline",
            discovery={
                "seed_is_author_identity": True,
                "timeline_endpoint": (
                    f"/2/users/{self.identity.user_id}/tweets"
                ),
                "timeline_posts_returned": len(candidates),
                "timeline_exhausted": exhausted,
                "post_limit": self.max_posts,
                "non_reply_posts_excluded": non_reply_posts_excluded,
                "replies_to_target_owned_posts_excluded": (
                    own_replies_excluded
                ),
            },
            coverage={
                "posts_scanned": len(candidates),
                "posts_complete": len(candidates) if exhausted else 0,
                "comments_and_replies_scanned": len(candidates),
                "target_comments_found": len(comments),
                "all_scanned_posts_complete": exhausted,
                "claim": (
                    "Target-authored replies returned by the X user timeline "
                    "within the endpoint's retained-history and configured "
                    "pagination limits."
                ),
                "post_results": [],
            },
            comments=comments,
            timings=timings,
        )

    def _x_user_timeline_sync(
        self,
        scraper: Any,
    ) -> tuple[list[dict[str, Any]], bool]:
        endpoint = (
            f"{scraper.API_BASE}/users/{self.identity.user_id}/tweets"
        )
        params: dict[str, Any] = {
            **scraper._request_fields(),
            "exclude": "retweets",
            "max_results": 100,
        }
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        pagination_token = ""
        exhausted = False

        while True:
            remaining = (
                self.max_posts - len(found)
                if self.max_posts
                else None
            )
            if remaining is not None and remaining <= 0:
                break
            page_params = dict(params)
            if remaining is not None:
                page_params["max_results"] = max(
                    5,
                    min(100, remaining),
                )
            if pagination_token:
                page_params["pagination_token"] = pagination_token
            payload = scraper._request_json(
                endpoint,
                params=page_params,
                resource_type="posts",
            )
            includes = scraper._include_maps(payload)
            rows = (
                payload.get("data")
                if isinstance(payload.get("data"), list)
                else []
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                post_id = text(row.get("id"))
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                found.append(
                    scraper._post_to_candidate(
                        row,
                        includes,
                        discovery_method="x_api_v2_user_timeline",
                    )
                )
                if self.max_posts and len(found) >= self.max_posts:
                    break
            meta = _dict(payload.get("meta"))
            pagination_token = text(
                meta.get("next_token")
                or meta.get("pagination_token")
            )
            if not pagination_token:
                exhausted = True
                break
            if self.max_posts and len(found) >= self.max_posts:
                break
            if not rows:
                break
        return found, exhausted
