#!/usr/bin/env python
"""Official-API LinkedIn Page collection for LISTEN and ENGAGE.

This is deliberately separate from the TikTok workflow and master registry.
It can inventory an administered LinkedIn organization Page, hydrate a frozen
new-post selection, and store retention-aware evidence.  It cannot discover
public topics or arbitrary members, run AI, export member data, draft, approve,
or publish.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tiktok_scraper.linkedin_api import (
    DEFAULT_API_VERSION,
    LinkedInAPIClient,
    LinkedInAPIError,
    LinkedInForbiddenError,
    LinkedInRateLimitedError,
    LinkedInUnavailableError,
    LinkedInValidationError,
    validate_organization_urn,
    validate_post_urn,
)
from tiktok_scraper.linkedin_state import (
    COMMENT_RETENTION_HOURS,
    ORGANIZATION_POST_RETENTION_DAYS,
    InvalidLinkedInStateInput,
    LinkedInCollectionState,
    LinkedInStateError,
)


CLI_SCHEMA_VERSION = "linkedin-workflow-cli-v1"
DEFAULT_DATABASE = Path("comments_data") / "linkedin" / "linkedin_collection.sqlite3"
ACCESS_TOKEN_ENV = "LINKEDIN_ACCESS_TOKEN"
DEFAULT_COMMENTS_PER_POST = 100
MAX_COMMENTS_PER_POST = 1_000
_API_VERSION_RE = re.compile(r"20[0-9]{2}(?:0[1-9]|1[0-2])")


class LinkedInWorkflowError(RuntimeError):
    """Safe command error that never contains an OAuth token or raw payload."""


def _positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise argparse.ArgumentTypeError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"{name} must be a positive integer"
        ) from exc
    if result < 1 or (maximum is not None and result > maximum):
        suffix = f" no greater than {maximum}" if maximum is not None else ""
        raise argparse.ArgumentTypeError(
            f"{name} must be a positive integer{suffix}"
        )
    return result


def _posts(value: str) -> int:
    return _positive_int(value, "posts")


def _comments(value: str) -> int:
    return _positive_int(
        value,
        "comments-per-post",
        maximum=MAX_COMMENTS_PER_POST,
    )


def _timeout(value: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 300") from exc
    if not 1 <= result <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 300")
    return result


def _api_version(value: str) -> str:
    normalized = str(value or "").strip()
    if _API_VERSION_RE.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError("api-version must use YYYYMM")
    return normalized


def _emit(value: Mapping[str, Any], *, stream: Any = None) -> None:
    destination = sys.stdout if stream is None else stream
    print(json.dumps(dict(value), ensure_ascii=True, sort_keys=True), file=destination)


def capabilities(api_version: str = DEFAULT_API_VERSION) -> dict[str, Any]:
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "platform": "linkedin",
        "api_authority": "linkedin_official_api",
        "api_version": api_version,
        "workflows": {
            "listen": "collection_only",
            "engage": "collection_only",
        },
        "sources": {
            "organization": "administered_organization_page",
            "post": "exact_authorized_organization_post_urn",
        },
        "collected": [
            "organization_post_fields",
            "official_asset_urns",
            "aggregate_social_metadata",
            "accessible_comments_and_replies",
        ],
        "unsupported": [
            "topic_or_hashtag_discovery",
            "arbitrary_member_collection",
            "browser_scraping",
            "cross_platform_export",
            "external_ai_processing",
            "drafting",
            "approval",
            "publication",
        ],
        "access_token_environment": ACCESS_TOKEN_ENV,
        "access_requirements": {
            "product": "linkedin_community_management",
            "oauth": "three_legged",
            "organization_post_read_scope": "r_organization_social",
            "organization_comment_read_scope": "r_organization_social_feed",
            "page_role_required": True,
        },
        "retention": {
            "member_comment_hours": COMMENT_RETENTION_HOURS,
            "organization_post_days": ORGANIZATION_POST_RETENTION_DAYS,
            "persistent_after_purge": "resource_urns_and_deletion_tombstones_only",
        },
        "publication_enabled": False,
        "external_ai_enabled": False,
    }


def _client(api_version: str, timeout: float) -> LinkedInAPIClient:
    token = os.environ.get(ACCESS_TOKEN_ENV, "")
    if not token:
        raise LinkedInWorkflowError(
            f"{ACCESS_TOKEN_ENV} is required for LinkedIn collection"
        )
    return LinkedInAPIClient(token, api_version=api_version, timeout=timeout)


def _api_error_code(error: LinkedInAPIError) -> str:
    operation = str(getattr(error, "operation", "request") or "request")
    operation = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in operation
    )[:60]
    kind = str(getattr(error, "kind", "unavailable") or "unavailable")
    return f"linkedin_api_{kind}_{operation}"[:120]


def _terminalize_observation(
    observation: Mapping[str, Any],
    *,
    api_version: str,
    comments_per_post: int,
) -> dict[str, Any]:
    """Close the bounded-comment outcome without retaining provider payloads."""

    normalized = dict(observation)
    comments = normalized.get("comments")
    metrics = normalized.get("metrics")
    collection = normalized.get("collection")
    if not isinstance(comments, list):
        raise LinkedInWorkflowError("LinkedIn adapter returned invalid comments")
    if not isinstance(metrics, Mapping) or not isinstance(collection, Mapping):
        raise LinkedInWorkflowError("LinkedIn adapter returned invalid evidence")
    if len(comments) > comments_per_post:
        raise LinkedInWorkflowError("LinkedIn adapter exceeded comments-per-post")

    declared_total = metrics.get("comments")
    if isinstance(declared_total, bool):
        declared_total = None
    elif declared_total is not None:
        try:
            declared_total = max(0, int(declared_total))
        except (TypeError, ValueError):
            declared_total = None
    if declared_total is not None:
        comments_status = "complete" if len(comments) >= declared_total else "truncated"
    else:
        comments_status = (
            "truncated" if len(comments) >= comments_per_post else "complete"
        )

    closed_collection = dict(collection)
    closed_collection.update(
        {
            "authority": "linkedin_official_api",
            "api_version": api_version,
            "post_status": "available",
            "social_metadata_status": "available",
            "comments_status": comments_status,
            "comments_limit": comments_per_post,
            "terminal": True,
        }
    )
    normalized["collection"] = closed_collection
    return normalized


def _freeze_inventory(
    state: LinkedInCollectionState,
    client: LinkedInAPIClient,
    run: Mapping[str, Any],
) -> tuple[list[str], int]:
    if str(run["source_mode"]) == "post":
        inventory = [str(run["exact_post_urn"])]
        known_excluded = 0
    else:
        discovered = client.list_author_post_urns(str(run["organization_urn"]))
        known = state.known_post_urns(str(run["organization_urn"]))
        inventory = [post_urn for post_urn in discovered if post_urn not in known]
        known_excluded = len(discovered) - len(inventory)
    state.freeze_inventory(str(run["run_id"]), inventory)
    return inventory, known_excluded


def collect_run(
    state: LinkedInCollectionState,
    client: LinkedInAPIClient,
    run_id: str,
) -> dict[str, Any]:
    """Advance one immutable run until exact count or terminal inventory."""

    run = state.get_run(run_id)
    if run["status"] == "collection_complete":
        return state.status(run_id)
    if run["status"] == "collection_incomplete":
        return state.status(run_id)

    known_excluded = 0
    if bool(run["inventory_frozen"]):
        inventory = state.inventory(run_id)
    else:
        inventory, known_excluded = _freeze_inventory(state, client, run)

    rows = {row["post_urn"]: row for row in state.post_rows(run_id)}
    failures: list[str] = []
    organization_urn = str(run["organization_urn"])
    requested_count = int(run["requested_count"])
    comments_per_post = int(run["comments_per_post"])

    for ordinal, post_urn in enumerate(inventory, start=1):
        status = state.status(run_id)
        if status["evidence_ready"] >= requested_count:
            break
        existing = rows.get(post_urn)
        if existing is not None and bool(existing["evidence_ready"]):
            continue

        if post_urn in state.known_post_urns(organization_urn):
            state.record_failure(
                run_id,
                ordinal=ordinal,
                post_urn=post_urn,
                error_code="known_post_excluded",
            )
            failures.append("known_post_excluded")
            continue

        try:
            raw_observation = client.collect_organization_post(
                post_urn,
                organization_urn,
                comments_limit=comments_per_post,
            )
            observation = _terminalize_observation(
                raw_observation,
                api_version=str(run["api_version"]),
                comments_per_post=comments_per_post,
            )
            state.checkpoint_post(
                run_id,
                ordinal=ordinal,
                observation=observation,
            )
        except (LinkedInForbiddenError, LinkedInRateLimitedError) as exc:
            error_code = _api_error_code(exc)
            state.record_failure(
                run_id,
                ordinal=ordinal,
                post_urn=post_urn,
                error_code=error_code,
            )
            raise
        except LinkedInUnavailableError as exc:
            error_code = _api_error_code(exc)
            state.record_failure(
                run_id,
                ordinal=ordinal,
                post_urn=post_urn,
                error_code=error_code,
            )
            failures.append(error_code)
        except (InvalidLinkedInStateInput, LinkedInWorkflowError):
            state.record_failure(
                run_id,
                ordinal=ordinal,
                post_urn=post_urn,
                error_code="invalid_adapter_observation",
            )
            raise LinkedInWorkflowError(
                "LinkedIn adapter observation failed the state contract"
            ) from None

    final_status = state.status(run_id)
    reasons: list[str] = list(dict.fromkeys(failures))
    if final_status["evidence_ready"] < requested_count:
        reasons.append("authorized_organization_inventory_exhausted")
        if known_excluded:
            reasons.append("known_posts_excluded")
    return state.finalize(run_id, reasons=reasons)


def command_capabilities(args: argparse.Namespace) -> dict[str, Any]:
    return capabilities(args.api_version)


def command_collect(args: argparse.Namespace) -> dict[str, Any]:
    organization_urn = validate_organization_urn(args.organization_urn)
    post_urn = validate_post_urn(args.post_urn) if args.post_urn else ""
    if args.source == "post":
        if not post_urn or args.posts != 1:
            raise LinkedInWorkflowError(
                "post source requires --post-urn and --posts 1"
            )
    elif post_urn:
        raise LinkedInWorkflowError("--post-urn is valid only with --source post")

    run_id = args.run_id or f"linkedin-{args.workflow}-{uuid.uuid4().hex}"
    args.run_id = run_id
    client = _client(args.api_version, args.timeout)
    with LinkedInCollectionState(args.database) as state:
        state.create_run(
            run_id,
            project=args.project,
            workflow=args.workflow,
            source_mode=args.source,
            organization_urn=organization_urn,
            exact_post_urn=post_urn,
            requested_count=args.posts,
            api_version=args.api_version,
            comments_per_post=args.comments_per_post,
        )
        return collect_run(state, client, run_id)


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    with LinkedInCollectionState(args.database) as state:
        run = state.get_run(args.run_id)
        if run["status"] == "collection_complete":
            return state.status(args.run_id)
        state.resume_incomplete(args.run_id)
        client = _client(str(run["api_version"]), args.timeout)
        return collect_run(state, client, args.run_id)


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    with LinkedInCollectionState(args.database) as state:
        return state.status(args.run_id)


def command_purge_expired(args: argparse.Namespace) -> dict[str, Any]:
    with LinkedInCollectionState(args.database) as state:
        result = state.purge_expired()
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "platform": "linkedin",
        "status": "purged",
        **result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Official LinkedIn organization-Page collection for LISTEN and ENGAGE"
    )
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE),
        help="Isolated retention-aware LinkedIn SQLite database",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capability_parser = subparsers.add_parser(
        "capabilities", help="Show the closed LinkedIn capability contract"
    )
    capability_parser.add_argument(
        "--api-version", type=_api_version, default=DEFAULT_API_VERSION
    )
    capability_parser.set_defaults(handler=command_capabilities)

    collect_parser = subparsers.add_parser(
        "collect", help="Create and execute an exact organization collection run"
    )
    collect_parser.add_argument("--workflow", choices=("listen", "engage"), required=True)
    collect_parser.add_argument("--source", choices=("organization", "post"), required=True)
    collect_parser.add_argument("--organization-urn", required=True)
    collect_parser.add_argument("--post-urn")
    collect_parser.add_argument("--posts", type=_posts, required=True)
    collect_parser.add_argument(
        "--comments-per-post",
        type=_comments,
        default=DEFAULT_COMMENTS_PER_POST,
    )
    collect_parser.add_argument(
        "--api-version", type=_api_version, default=DEFAULT_API_VERSION
    )
    collect_parser.add_argument("--project", default="linkedin")
    collect_parser.add_argument("--run-id")
    collect_parser.add_argument("--timeout", type=_timeout, default=30.0)
    collect_parser.set_defaults(handler=command_collect)

    resume_parser = subparsers.add_parser(
        "resume", help="Resume only the saved immutable run selection"
    )
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--timeout", type=_timeout, default=30.0)
    resume_parser.set_defaults(handler=command_resume)

    status_parser = subparsers.add_parser(
        "status", help="Read status and enforce expiration without network access"
    )
    status_parser.add_argument("--run-id", required=True)
    status_parser.set_defaults(handler=command_status)

    purge_parser = subparsers.add_parser(
        "purge-expired", help="Purge expired payloads and retain URN tombstones"
    )
    purge_parser.set_defaults(handler=command_purge_expired)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (
        LinkedInWorkflowError,
        LinkedInValidationError,
        LinkedInAPIError,
        LinkedInStateError,
        OSError,
    ) as exc:
        error_result: dict[str, Any] = {
            "schema_version": CLI_SCHEMA_VERSION,
            "platform": "linkedin",
            "status": "error",
            "error": str(exc),
        }
        run_id = str(getattr(args, "run_id", "") or "")
        if run_id:
            error_result["run_id"] = run_id
        if isinstance(exc, LinkedInAPIError):
            error_result["error_kind"] = exc.kind
            error_result["operation"] = exc.operation
            if exc.retry_after_seconds is not None:
                error_result["retry_after_seconds"] = exc.retry_after_seconds
        _emit(error_result, stream=sys.stderr)
        return 1
    _emit(result)
    if args.command in {"collect", "resume"} and result.get("status") == "collection_incomplete":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
