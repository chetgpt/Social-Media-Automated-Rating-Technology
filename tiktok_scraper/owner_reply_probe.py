"""Learn public comment identifiers from replies on an account's own posts.

The probe starts from a live public profile, inventories public posts on that
profile, and keeps only replies authored by the profile owner. It records a
sanitized schema fingerprint: field names, public identifiers, endpoint path
templates, scope keys, and pagination keys. Cookies, tokens, request headers,
and unrelated commenter text are never included in the result.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

from tiktok_scraper.author_activity_trace import (
    AuthorActivityTracer,
    AuthorIdentity,
    candidate_owner,
    comment_author,
    iter_comments,
    match_author,
    normalize_comment,
)
from tiktok_scraper.profile_contract import canonical_profile_url, text_value


AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "c_user",
    "xs",
}


PLATFORM_REPLY_MODELS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "comment_feed_requests": [
            {
                "path_template": "/api/comment/list/",
                "scope_fields": ["aweme_id"],
                "pagination_fields": ["cursor", "count"],
            },
            {
                "path_template": "/api/comment/list/reply/",
                "scope_fields": ["item_id", "comment_id"],
                "pagination_fields": ["cursor", "count"],
            },
        ],
        "response_author_fields": [
            "user.uniqueId",
            "user.uid",
            "user.secUid",
        ],
        "accepted_author_filter_fields": [],
        "capability": "post_scoped_only",
        "author_identifier_role": "response_verification_only",
    },
    "youtube": {
        "comment_feed_requests": [
            {
                "path_template": "/youtubei/v1/next",
                "scope_fields": ["video continuation", "thread continuation"],
                "pagination_fields": ["continuation"],
            }
        ],
        "response_author_fields": [
            "authorText",
            "authorEndpoint.browseId",
            "authorChannelId",
        ],
        "accepted_author_filter_fields": [],
        "capability": "post_scoped_only",
        "author_identifier_role": "response_verification_only",
    },
    "instagram": {
        "comment_feed_requests": [
            {
                "path_template": "/api/graphql",
                "operation": "PolarisPostCommentsPaginationQuery",
                "scope_fields": ["media_id"],
                "pagination_fields": ["after", "before", "first", "last"],
            },
            {
                "path_template": "/api/v1/media/{media_id}/comments/",
                "scope_fields": ["media_id"],
                "pagination_fields": ["min_id", "max_id"],
            },
        ],
        "response_author_fields": [
            "user.username",
            "user.pk",
            "user.id",
        ],
        "accepted_author_filter_fields": [],
        "capability": "post_scoped_only",
        "author_identifier_role": "response_verification_only",
    },
    "facebook": {
        "comment_feed_requests": [
            {
                "path_template": "/api/graphql",
                "scope_fields": ["feedbackID", "targetID", "feedback_id"],
                "pagination_fields": ["cursor", "count", "first"],
            }
        ],
        "response_author_fields": [
            "author.id",
            "author.name",
            "actor.id",
            "from.id",
        ],
        "accepted_author_filter_fields": [],
        "capability": "post_scoped_only",
        "author_identifier_role": "response_verification_only",
    },
    "x": {
        "comment_feed_requests": [
            {
                "path_template": "/2/users/{user_id}/tweets",
                "scope_fields": ["user_id"],
                "pagination_fields": ["pagination_token", "max_results"],
            }
        ],
        "response_author_fields": ["author_id", "username"],
        "accepted_author_filter_fields": ["user_id"],
        "capability": "native_author_query",
        "author_identifier_role": "author_timeline_query_key",
    },
}


def text(value: Any) -> str:
    return text_value(value)


def platform_reply_model(platform: str) -> dict[str, Any]:
    """Return a copy so callers cannot mutate the shared model."""
    return copy.deepcopy(PLATFORM_REPLY_MODELS[platform])


def record_schema_paths(
    value: Any,
    *,
    max_depth: int = 4,
) -> list[str]:
    """Return key paths only, never values."""
    paths: set[str] = set()

    def visit(node: Any, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, dict):
            for raw_key, child in node.items():
                key = str(raw_key)
                path = f"{prefix}.{key}" if prefix else key
                paths.add(path)
                visit(child, path, depth + 1)
        elif isinstance(node, list):
            for child in node[:10]:
                visit(child, prefix, depth + 1)

    visit(value, "", 0)
    return sorted(paths)


def is_reply_record(record: dict[str, Any]) -> bool:
    return bool(
        record.get("is_reply")
        or record.get("parent_comment_id")
        or record.get("parent_id")
        or record.get("reply_id")
        or record.get("in_reply_to_user_id")
        or record.get("in_reply_to_post_id")
    )


def owner_reply_status(
    *,
    candidate_count: int,
    available_posts: int,
    failed_posts: int,
    reply_count: int,
    discovery_error: str = "",
) -> str:
    if not candidate_count:
        return "blocked" if discovery_error else "no_owner_posts_observed"
    if available_posts and failed_posts:
        return "partial"
    if available_posts and reply_count:
        return "available"
    if available_posts:
        return "no_owner_replies_observed"
    return "blocked" if failed_posts or discovery_error else "no_owner_replies_observed"


def learn_profile_surface_identity(
    identity: AuthorIdentity,
    candidates: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Bind a stable owner ID when a direct profile inventory is consistent."""
    identifier_counts: Counter[str] = Counter()
    exact_username_ids: set[str] = set()
    exact_usernames: set[str] = set()

    for candidate in candidates:
        owner = candidate_owner(candidate, identity.platform)
        for identifier in owner["identifiers"]:
            identifier_counts[identifier] += 1
        matching_usernames = identity.usernames.intersection(owner["usernames"])
        if matching_usernames:
            exact_usernames.update(matching_usernames)
            exact_username_ids.update(owner["identifiers"])

    learned_ids = set(exact_username_ids)
    if not learned_ids and len(identifier_counts) == 1:
        consistent_id = next(iter(identifier_counts))
        if (
            not identity.identifiers
            or consistent_id in identity.identifiers
        ):
            learned_ids.add(consistent_id)
    identity.learn(
        usernames=exact_usernames,
        identifiers=learned_ids,
    )
    conflicting_ids = sorted(
        set(identifier_counts).difference(identity.identifiers)
    )
    return {
        "binding_basis": (
            "exact_username_on_profile_posts"
            if exact_username_ids
            else (
                "single_consistent_owner_id_on_direct_profile_surface"
                if learned_ids
                else (
                    "conflicting_owner_id_not_learned"
                    if conflicting_ids and identity.identifiers
                    else "no_stable_binding_observed"
                )
            )
        ),
        "owner_ids_observed": sorted(identifier_counts),
        "owner_id_frequencies": dict(sorted(identifier_counts.items())),
        "learned_public_identifiers": sorted(learned_ids),
        "conflicting_public_identifiers": conflicting_ids,
    }


def build_schema_analysis(
    *,
    platform: str,
    owner_replies: Iterable[dict[str, Any]],
    raw_reply_records: Iterable[dict[str, Any]] = (),
    comment_methods: Iterable[str] = (),
    posts_with_live_comment_feeds: int = 0,
) -> dict[str, Any]:
    replies = [row for row in owner_replies if isinstance(row, dict)]
    raw_records = [
        row for row in raw_reply_records if isinstance(row, dict)
    ]
    paths: set[str] = set()
    for row in raw_records or replies:
        paths.update(record_schema_paths(row))

    public_identifiers: set[str] = set()
    usernames: set[str] = set()
    for row in replies:
        author = row.get("author")
        if not isinstance(author, dict):
            continue
        public_identifiers.update(
            text(value)
            for value in author.get("public_identifiers") or []
            if text(value)
        )
        username = text(author.get("username"))
        if username:
            usernames.add(username)

    model = platform_reply_model(platform)
    capability = model["capability"]
    if replies:
        live_observation = "target_owner_reply_observed"
    elif posts_with_live_comment_feeds:
        live_observation = "post_feed_observed_without_target_owner_reply"
    else:
        live_observation = "no_live_comment_feed_observed"

    return {
        "sanitization": {
            "stores_request_field_names_only": True,
            "stores_headers_cookies_or_tokens": False,
            "stores_unrelated_commenter_text": False,
        },
        "live_observation": live_observation,
        "observed_comment_methods": sorted(
            {text(value) for value in comment_methods if text(value)}
        ),
        "observed_reply_record_fields": sorted(paths),
        "observed_target_usernames": sorted(usernames),
        "observed_target_public_identifiers": sorted(public_identifiers),
        "platform_request_model": {
            "evidence_basis": (
                "request construction used by the platform adapter plus "
                "the normalized public response contract"
            ),
            **model,
        },
        "discovery_capability": {
            "classification": capability,
            "can_query_comments_by_author": capability == "native_author_query",
            "accepted_author_filter_fields": model[
                "accepted_author_filter_fields"
            ],
            "learned_ids_can_be_used_for": (
                "native author timeline discovery"
                if capability == "native_author_query"
                else (
                    "exact author verification inside other live public "
                    "post comment pools"
                )
            ),
            "finding": (
                "This platform exposes a native author-scoped timeline."
                if capability == "native_author_query"
                else (
                    "The observed public comment request is scoped by post or "
                    "thread identifiers. The author identifier is returned in "
                    "the response but is not an accepted feed filter."
                )
            ),
        },
    }


def _walk_dicts(value: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 24:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child, depth + 1)


def _tiktok_post_candidates(
    payload: Any,
    username: str,
) -> Iterator[dict[str, Any]]:
    emitted: set[str] = set()
    for node in _walk_dicts(payload):
        values = [node]
        for key in ("itemStruct", "item", "aweme_info", "awemeInfo"):
            nested = node.get(key)
            if isinstance(nested, dict):
                values.append(nested)
        for item in values:
            author = item.get("author")
            if not isinstance(author, dict):
                continue
            post_id = text(
                item.get("id")
                or item.get("aweme_id")
                or item.get("item_id")
            )
            if not post_id or post_id in emitted:
                continue
            item_username = text(
                author.get("uniqueId")
                or author.get("unique_id")
            )
            if (
                item_username
                and username
                and item_username.casefold() != username.casefold()
            ):
                continue
            if not (
                isinstance(item.get("video"), dict)
                or "desc" in item
                or "description" in item
            ):
                continue
            emitted.add(post_id)
            yield {
                "post_id": post_id,
                "url": (
                    f"https://www.tiktok.com/@{item_username or username}"
                    f"/video/{post_id}"
                ),
                "owner_username": item_username or username,
                "caption": text(
                    item.get("desc") or item.get("description")
                ),
                "pre_discovery_seed": username,
                "search_query": username,
                "discovery_method": "tiktok_profile_public_response",
            }


class OwnerReplyProbe:
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
        max_posts: int = 5,
        max_comments_per_post: int = 200,
        discovery_timeout_seconds: float = 90,
        post_timeout_seconds: float = 180,
        trace_other_posts: bool = False,
        trace_max_posts: int = 10,
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
        self.max_posts = max(1, int(max_posts))
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
        self.trace_other_posts = bool(trace_other_posts)
        self.trace_max_posts = max(1, int(trace_max_posts))

    def _profile_url(self) -> str:
        raw = self.identity.input_value
        if raw.startswith(("http://", "https://")):
            profile_url = raw
        else:
            profile_url = canonical_profile_url(
                self.identity.platform,
                self.identity.username,
                self.identity.user_id,
            )
        if self.identity.platform in {"youtube", "facebook"}:
            profile_url = re.sub(r"/about/?$", "", profile_url)
        return profile_url

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
            options: dict[str, Any] = {"headless": self.headless}
            if self.browser_channel:
                options["channel"] = self.browser_channel
            browser = await playwright.chromium.launch(**options)
            context = await browser.new_context(locale="en-US")
            owns_context = True
        return playwright, browser, context, external, owns_context

    @staticmethod
    async def _close_browser(
        resources: tuple[Any, Any, Any, bool, bool],
    ) -> None:
        playwright, browser, context, external, owns_context = resources
        if owns_context:
            try:
                await context.close()
            except Exception:
                pass
        if not external:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await playwright.stop()
        except Exception:
            pass

    async def run(self) -> dict[str, Any]:
        if self.identity.platform == "x":
            payload = self._run_x_model()
        elif self.identity.platform == "tiktok":
            payload = await self._run_tiktok()
        else:
            payload = await self._run_profile_pool_platform()

        payload["other_post_trace"] = {
            "status": "not_requested",
            "note": (
                "Use --trace-other-posts to pass learned public identifiers "
                "into live discovery on other users' posts."
            ),
        }
        if self.trace_other_posts:
            tracer = AuthorActivityTracer(
                platform=self.identity.platform,
                target=self.identity.input_value,
                username=self.identity.username,
                user_id=self.identity.user_id,
                cdp_url=self.cdp_url,
                browser_channel=self.browser_channel,
                headless=self.headless,
                max_posts=self.trace_max_posts,
                max_comments_per_post=self.max_comments_per_post,
                discovery_timeout_seconds=self.discovery_timeout_seconds,
                post_timeout_seconds=self.post_timeout_seconds,
            )
            payload["other_post_trace"] = await tracer.run()
        return payload

    def _base_payload(
        self,
        *,
        status: str,
        profile_url: str,
        authenticated: bool,
        discovery: dict[str, Any],
        coverage: dict[str, Any],
        owner_replies: list[dict[str, Any]],
        raw_reply_records: Iterable[dict[str, Any]],
        comment_methods: Iterable[str],
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
                "start_direction": "target_profile_then_owner_replies",
                "profile_url": profile_url,
                "source": "live_public_platform_fetch",
                "local_comment_files_used": False,
                "authenticated_session_detected": authenticated,
                "retention_policy": (
                    "retain only replies authored by the target account; "
                    "do not retain unrelated commenter text"
                ),
            },
            "target_identity": self.identity.as_dict(),
            "discovery": discovery,
            "coverage": coverage,
            "owner_replies": owner_replies,
            "schema_analysis": build_schema_analysis(
                platform=self.identity.platform,
                owner_replies=owner_replies,
                raw_reply_records=raw_reply_records,
                comment_methods=comment_methods,
                posts_with_live_comment_feeds=int(
                    coverage.get("posts_with_live_comment_feeds") or 0
                ),
            ),
            "timings_seconds": {
                key: round(value, 3)
                for key, value in timings.items()
            },
            "error": error,
        }

    def _run_x_model(self) -> dict[str, Any]:
        token = (
            os.environ.get("X_BEARER_TOKEN", "").strip()
            or os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
        )
        token_file = (
            os.environ.get("X_BEARER_TOKEN_FILE", "").strip()
            or os.environ.get("TWITTER_BEARER_TOKEN_FILE", "").strip()
        )
        if not token and token_file:
            path = Path(token_file)
            if path.is_file():
                try:
                    token = path.read_text(
                        encoding="utf-8-sig"
                    ).strip()
                except OSError:
                    token = ""
        credential_error = (
            ""
            if token
            else (
                "X API credentials are missing. Set X_BEARER_TOKEN or "
                "X_BEARER_TOKEN_FILE to an official X API v2 bearer token."
            )
        )
        payload = self._base_payload(
            status=(
                "credentials_required"
                if credential_error
                else "native_author_query_available"
            ),
            profile_url=self._profile_url(),
            authenticated=False,
            discovery={
                "owner_reply_bootstrap_needed": False,
                "live_timeline_attempted": False,
                "reason": (
                    "X exposes an author-scoped post timeline when an API "
                    "bearer token is available."
                ),
            },
            coverage={
                "owner_posts_observed": 0,
                "posts_with_live_comment_feeds": 0,
                "target_owner_replies_found": 0,
                "post_results": [],
            },
            owner_replies=[],
            raw_reply_records=[],
            comment_methods=[],
            timings={"total": 0.0},
            error=credential_error,
        )
        payload["scope"]["source"] = "platform_capability_model"
        return payload

    async def _run_profile_pool_platform(self) -> dict[str, Any]:
        platform = self.identity.platform
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
            raise ValueError(f"Unsupported profile-pool platform: {platform}")

        started = time.perf_counter()
        timings: dict[str, float] = {}
        resources: tuple[Any, Any, Any, bool, bool] | None = None
        context = None
        authenticated = False
        profile_url = self._profile_url()
        discovery_error = ""
        candidates: list[dict[str, Any]] = []
        post_results: list[dict[str, Any]] = []
        owner_replies: list[dict[str, Any]] = []
        raw_reply_records: list[dict[str, Any]] = []
        comment_methods: set[str] = set()
        seen_reply_markers: set[str] = set()

        try:
            if platform in {"instagram", "facebook"}:
                resources = await self._open_browser()
                context = resources[2]
                cookies = await context.cookies()
                authenticated = bool(
                    {
                        text(cookie.get("name"))
                        for cookie in cookies
                    }.intersection(AUTH_COOKIE_NAMES)
                )

            stage = time.perf_counter()
            try:
                rows = await asyncio.wait_for(
                    scraper.search(
                        profile_url,
                        max_videos=self.max_posts,
                        context=context,
                    ),
                    timeout=self.discovery_timeout_seconds,
                )
                candidates = [
                    row for row in rows if isinstance(row, dict)
                ][: self.max_posts]
            except Exception as exc:
                discovery_error = f"{type(exc).__name__}: {exc}"
            timings["profile_post_inventory"] = time.perf_counter() - stage

            binding = learn_profile_surface_identity(
                self.identity,
                candidates,
            )

            stage = time.perf_counter()
            for candidate in candidates:
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
                            "comments_and_replies_scanned": 0,
                            "reply_records_scanned": 0,
                            "target_owner_replies": 0,
                            "complete": False,
                            "elapsed_seconds": round(
                                time.perf_counter() - post_started,
                                3,
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                combined_post = {
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
                post_binding = learn_profile_surface_identity(
                    self.identity,
                    [combined_post],
                )
                records = list(
                    iter_comments(extraction.get("comments") or [])
                )
                reply_records = [
                    record for record in records if is_reply_record(record)
                ]
                before = len(owner_replies)
                basis_counts = {
                    "stable_id": 0,
                    "exact_username": 0,
                }
                for record in reply_records:
                    basis, author = match_author(self.identity, record)
                    if not basis:
                        continue
                    normalized = normalize_comment(
                        record,
                        identity=self.identity,
                        author=author,
                        match_basis=basis,
                        source_post=combined_post,
                    )
                    normalized["is_reply"] = True
                    marker = (
                        text(normalized.get("comment_id"))
                        or "\x1f".join(
                            (
                                text(
                                    normalized.get("source_post", {}).get(
                                        "post_id"
                                    )
                                ),
                                text(normalized.get("text")),
                                text(normalized.get("created_at")),
                            )
                        )
                    )
                    if marker in seen_reply_markers:
                        continue
                    seen_reply_markers.add(marker)
                    owner_replies.append(normalized)
                    raw_reply_records.append(record)
                    basis_counts[basis] += 1

                method = text(extraction.get("comment_method"))
                if method:
                    comment_methods.add(method)
                error = text(
                    extraction.get("comment_error")
                    or extraction.get("error")
                )
                comments_seen = int(
                    extraction.get("comments_seen_in_response")
                    if extraction.get("comments_seen_in_response")
                    is not None
                    else len(records)
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
                        "comments_and_replies_scanned": comments_seen,
                        "reply_records_scanned": len(reply_records),
                        "target_owner_replies": (
                            len(owner_replies) - before
                        ),
                        "match_basis_counts": basis_counts,
                        "profile_binding": post_binding,
                        "comment_method": method,
                        "complete": (
                            extraction.get("comments_exhausted") is True
                        ),
                        "limit_reached": bool(
                            extraction.get("comment_limit_reached")
                        ),
                        "elapsed_seconds": round(
                            time.perf_counter() - post_started,
                            3,
                        ),
                        "error": error,
                    }
                )
            timings["owner_post_comment_scan"] = (
                time.perf_counter() - stage
            )
        finally:
            if resources is not None:
                await self._close_browser(resources)

        timings["total"] = time.perf_counter() - started
        available_posts = sum(
            row.get("status") == "available" for row in post_results
        )
        failed_posts = sum(
            row.get("status") == "error" for row in post_results
        )
        status = owner_reply_status(
            candidate_count=len(candidates),
            available_posts=available_posts,
            failed_posts=failed_posts,
            reply_count=len(owner_replies),
            discovery_error=discovery_error,
        )
        return self._base_payload(
            status=status,
            profile_url=profile_url,
            authenticated=authenticated,
            discovery={
                "method": "direct_public_profile_post_inventory",
                "owner_posts_observed": len(candidates),
                "post_limit": self.max_posts,
                "identity_binding": binding,
                "error": discovery_error,
            },
            coverage={
                "owner_posts_observed": len(candidates),
                "posts_with_live_comment_feeds": available_posts,
                "posts_failed": failed_posts,
                "target_owner_replies_found": len(owner_replies),
                "all_available_posts_complete": (
                    bool(available_posts)
                    and all(
                        row.get("complete")
                        for row in post_results
                        if row.get("status") == "available"
                    )
                ),
                "post_results": post_results,
            },
            owner_replies=owner_replies,
            raw_reply_records=raw_reply_records,
            comment_methods=comment_methods,
            timings=timings,
            error=discovery_error if status == "blocked" else "",
        )

    async def _run_tiktok(self) -> dict[str, Any]:
        from tiktok_scraper.username_comment_trace import (
            TikTokUsernameCommentTracer,
            post_id_from_url,
            username_from_post_url,
        )

        started = time.perf_counter()
        timings: dict[str, float] = {}
        resources = await self._open_browser()
        context = resources[2]
        page = await context.new_page()
        profile_url = self._profile_url()
        candidates_by_id: dict[str, dict[str, Any]] = {}
        response_tasks: set[asyncio.Task[Any]] = set()
        discovery_error = ""
        login_prompt = False

        async def capture_response(response: Any) -> None:
            if not any(
                marker in response.url
                for marker in (
                    "/api/post/item_list/",
                    "/api/item/detail/",
                )
            ):
                return
            try:
                payload = await response.json()
            except Exception:
                return
            for candidate in _tiktok_post_candidates(
                payload,
                self.identity.username,
            ):
                candidates_by_id.setdefault(
                    candidate["post_id"],
                    candidate,
                )

        def schedule_response(response: Any) -> None:
            task = asyncio.create_task(capture_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", schedule_response)
        cookies = await context.cookies(["https://www.tiktok.com"])
        authenticated = bool(
            {
                text(cookie.get("name"))
                for cookie in cookies
            }.intersection(AUTH_COOKIE_NAMES)
        )

        try:
            stage = time.perf_counter()
            try:
                await page.goto(
                    profile_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.discovery_timeout_seconds * 1000),
                )
                await page.wait_for_timeout(3500)
                for _ in range(4):
                    await page.mouse.wheel(0, 1600)
                    await page.wait_for_timeout(900)
            except Exception as exc:
                discovery_error = f"{type(exc).__name__}: {exc}"

            try:
                links = await page.locator(
                    'a[href*="/video/"]'
                ).evaluate_all(
                    "nodes => [...new Set(nodes.map(n => n.href).filter(Boolean))]"
                )
            except Exception:
                links = []
            for url in links:
                post_id = post_id_from_url(url)
                if not post_id:
                    continue
                owner_username = (
                    username_from_post_url(url)
                    or self.identity.username
                )
                if (
                    self.identity.username
                    and owner_username.casefold()
                    != self.identity.username.casefold()
                ):
                    continue
                candidates_by_id.setdefault(
                    post_id,
                    {
                        "post_id": post_id,
                        "url": text(url).split("?", 1)[0],
                        "owner_username": owner_username,
                        "caption": "",
                        "pre_discovery_seed": self.identity.username,
                        "search_query": self.identity.username,
                        "discovery_method": "tiktok_profile_dom",
                    },
                )
            if response_tasks:
                _, pending = await asyncio.wait(
                    response_tasks,
                    timeout=10,
                )
                for task in pending:
                    task.cancel()
            try:
                body_text = (await page.locator("body").inner_text()).casefold()
                login_prompt = (
                    "log in to tiktok" in body_text
                    or "sign up for tiktok" in body_text
                )
            except Exception:
                login_prompt = False
            timings["profile_post_inventory"] = time.perf_counter() - stage

            candidates = list(candidates_by_id.values())[: self.max_posts]
            tracer = TikTokUsernameCommentTracer(
                username=self.identity.username,
                cdp_url=self.cdp_url,
                max_posts=self.max_posts,
                max_comments_per_post=self.max_comments_per_post,
                request_timeout_seconds=min(
                    30.0,
                    self.post_timeout_seconds,
                ),
                navigation_timeout_seconds=min(
                    60.0,
                    self.post_timeout_seconds,
                ),
            )
            tracer.target_identifiers.update(self.identity.identifiers)

            stage = time.perf_counter()
            post_results: list[dict[str, Any]] = []
            owner_replies: list[dict[str, Any]] = []
            for candidate in candidates:
                post_started = time.perf_counter()
                try:
                    coverage, matches = await asyncio.wait_for(
                        tracer._scan_post(page, candidate),
                        timeout=self.post_timeout_seconds,
                    )
                except Exception as exc:
                    post_results.append(
                        {
                            "post_id": candidate["post_id"],
                            "url": candidate["url"],
                            "status": "error",
                            "comments_and_replies_scanned": 0,
                            "reply_records_scanned": 0,
                            "target_owner_replies": 0,
                            "complete": False,
                            "elapsed_seconds": round(
                                time.perf_counter() - post_started,
                                3,
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                replies = [
                    row
                    for row in matches
                    if text(row.get("parent_comment_id"))
                ]
                for reply in replies:
                    reply["is_reply"] = True
                    author = reply.get("author")
                    identifiers = (
                        set(author.get("public_identifiers") or [])
                        if isinstance(author, dict)
                        else set()
                    )
                    reply["match_basis"] = (
                        "stable_id"
                        if self.identity.identifiers.intersection(identifiers)
                        else "exact_username"
                    )
                owner_replies.extend(replies)
                error = "; ".join(coverage.get("errors") or [])
                post_results.append(
                    {
                        "post_id": candidate["post_id"],
                        "url": candidate["url"],
                        "status": (
                            "available"
                            if coverage.get("comments_scanned") or not error
                            else "error"
                        ),
                        "comments_and_replies_scanned": int(
                            coverage.get("comments_scanned") or 0
                        ),
                        "reply_records_scanned": int(
                            coverage.get("replies_scanned") or 0
                        ),
                        "target_owner_replies": len(replies),
                        "comment_method": "tiktok_comment_list_api",
                        "complete": bool(coverage.get("complete")),
                        "limit_reached": bool(
                            coverage.get("limit_reached")
                        ),
                        "elapsed_seconds": round(
                            time.perf_counter() - post_started,
                            3,
                        ),
                        "error": error,
                    }
                )
            timings["owner_post_comment_scan"] = (
                time.perf_counter() - stage
            )
            self.identity.learn(
                identifiers=tracer.target_identifiers,
            )
        finally:
            for task in list(response_tasks):
                task.cancel()
            try:
                await page.close()
            except Exception:
                pass
            await self._close_browser(resources)

        timings["total"] = time.perf_counter() - started
        available_posts = sum(
            row.get("status") == "available" for row in post_results
        )
        failed_posts = sum(
            row.get("status") == "error" for row in post_results
        )
        if not candidates and not discovery_error:
            discovery_error = (
                "TikTok profile exposed no public post links or decodable "
                "post-list response."
            )
            if login_prompt:
                discovery_error += " A login prompt was observed."
        status = owner_reply_status(
            candidate_count=len(candidates),
            available_posts=available_posts,
            failed_posts=failed_posts,
            reply_count=len(owner_replies),
            discovery_error=discovery_error,
        )
        return self._base_payload(
            status=status,
            profile_url=profile_url,
            authenticated=authenticated,
            discovery={
                "method": "direct_tiktok_profile_public_page",
                "owner_posts_observed": len(candidates),
                "post_limit": self.max_posts,
                "login_prompt_observed": login_prompt,
                "error": discovery_error,
            },
            coverage={
                "owner_posts_observed": len(candidates),
                "posts_with_live_comment_feeds": available_posts,
                "posts_failed": failed_posts,
                "target_owner_replies_found": len(owner_replies),
                "all_available_posts_complete": (
                    bool(available_posts)
                    and all(
                        row.get("complete")
                        for row in post_results
                        if row.get("status") == "available"
                    )
                ),
                "post_results": post_results,
            },
            owner_replies=owner_replies,
            raw_reply_records=owner_replies,
            comment_methods={"tiktok_comment_list_api"}
            if available_posts
            else set(),
            timings=timings,
            error=discovery_error if status == "blocked" else "",
        )
