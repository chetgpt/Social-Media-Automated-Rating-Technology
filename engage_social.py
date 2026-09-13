"""Threads-first, official-API LISTEN/AUDIT/ENGAGE. AI stages are offline.

Collection/drafting is not authorization. Only an exact presented, explicitly
approved response in a live run may reach the guarded publication command.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from social_engage.adapters import AdapterError, LinkedInAdapter, ThreadsAdapter, numeric, username
from social_engage.state import State, StateError, operator_lock, require, text


def capabilities(platform):
    return {"schema": "social-engage-capabilities-v1", "platform": platform, "authority": "official_api",
            "workflows": ["listen", "music-audit", "audit", "engage"],
            "sources": ["own", "creator", "topic", "post"] if platform == "threads" else ["organization", "post"],
            "stages": ["collect", "packet", "store-analysis", "store-draft", "store-review", "request-live", "show-response", "authorize", "publish"],
            "maintenance": ["status", "resume", "refresh", "purge-expired", "purge-post"],
            "publishes": "text_reply_to_collected_post" if platform == "threads" else "organization_comment_on_own_page_post",
            "default_mode": "shadow", "ai_engine": "interactive_built_in_assistant", "external_ai_api": False,
            "token_environment": "THREADS_ACCESS_TOKEN" if platform == "threads" else "LINKEDIN_ACCESS_TOKEN",
            "required_scopes": (["threads_basic", "threads_content_publish", "threads_read_replies", "threads_manage_replies",
                                  "threads_manage_insights (own insights)", "threads_keyword_search (topic)", "threads_profile_discovery (creator)"]
                                 if platform == "threads" else ["r_organization_admin", "r_organization_social", "r_organization_social_feed", "w_organization_social_feed"]),
            "unsupported": ["audio_download", "sonic_audit", "audio_archive", "acoustic_identification", "lyrics_verification",
                            "platform_music_identity", "transcript_extraction", "automatic_outreach", "direct_messages", "showcase_media_posts"],
            "setup_required": "approved app, runtime OAuth token, scopes and exact account; API acceptance is checked at runtime",
            "live_validation": "not_claimed_by_capabilities"}


def positive(value):
    try:
        result = int(value)
        if result > 0:
            return result
    except (TypeError, ValueError):
        pass
    raise argparse.ArgumentTypeError("a positive finite integer is required")


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return int(parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        pass
    raise argparse.ArgumentTypeError("use an ISO timestamp with explicit timezone")


def scope_from_args(args):
    sources = capabilities(args.platform)["sources"]
    require(args.source in sources, "unsupported_source_for_platform")
    require(args.comments <= 500, "comment_cap_maximum_500")
    require(args.posts <= 10000, "post_count_maximum_10000")
    require(args.mode != "live" or args.workflow == "engage", "live_requires_engage")
    if args.platform == "threads":
        account = username(args.account)
        if args.source == "post":
            target = numeric(args.target)
        elif args.source == "creator":
            target = username(args.target)
        elif args.source == "own":
            require(args.target is None, "own_source_does_not_accept_target")
            target = account
        else:
            target = " ".join(text(args.target, "topic", 500).split())
    else:
        from tiktok_scraper.linkedin_api import validate_organization_urn, validate_post_urn
        account = validate_organization_urn(args.account)
        target = validate_post_urn(args.target) if args.source == "post" else account
        require(args.source != "organization" or args.target in (None, account), "organization_target_mismatch")
        require(not args.since and not args.until, "linkedin_publication_window_unsupported")
    require(args.source != "post" or args.posts == 1, "exact_post_requires_one")
    require((args.since is None) == (args.until is None), "both_publication_bounds_required")
    if args.since is not None:
        require(1688540400 <= args.since < args.until <= time.time(), "invalid_publication_window")
    candidate_limit = args.candidate_limit or max(100, args.posts * 3)
    require(args.posts <= candidate_limit <= 10000, "invalid_candidate_limit")
    return {"platform": args.platform, "workflow": args.workflow, "mode": args.mode, "source": args.source,
            "target": target, "account": account, "posts": args.posts, "comments": args.comments,
            "candidate_limit": candidate_limit, "since": args.since, "until": args.until,
            "api_version": "v1.0" if args.platform == "threads" else args.api_version}


def make_adapter(scope):
    if scope["platform"] == "threads":
        return ThreadsAdapter(os.environ.get("THREADS_ACCESS_TOKEN", ""))
    return LinkedInAdapter(os.environ.get("LINKEDIN_ACCESS_TOKEN", ""), version=scope["api_version"])


def collect(state, run_id, adapter):
    doc = state.load(run_id)
    if doc["collection_verified"]:
        return state.status(run_id)
    scope = doc["scope"]
    try:
        actor = adapter.identity(scope["account"])
        if not doc["frozen"]:
            candidates = adapter.discover(scope)
            known = state.known()
            selected = list(dict.fromkeys(pid for pid in candidates if pid not in known))[:scope["posts"]]
            state.freeze(run_id, selected, actor)
            doc = state.load(run_id)
        else:
            require(actor == doc["actor"], "resume_account_mismatch")
        for post_id in doc["inventory"]:
            if post_id in doc["posts"]:
                continue
            try:
                evidence = adapter.collect(post_id, scope, actor)
                state.checkpoint(run_id, evidence)
            except (AdapterError, StateError) as exc:
                state.failure(run_id, post_id, str(exc))
            except Exception:
                # Existing LinkedIn reader errors must not leak response content.
                state.failure(run_id, post_id, "adapter_collection_failed")
    except (AdapterError, StateError) as exc:
        state.failure(run_id, "collection", str(exc))
    except Exception:
        state.failure(run_id, "collection", "adapter_unavailable")
    return state.finalize(run_id)


def publish(state, run_id, post_id, adapter):
    doc, p = state.ready(run_id, post_id)
    adapter.preflight(p["evidence"], doc["scope"], doc["actor"])
    # Freshness and authorization checked again after all network preflight.
    state.reserve(run_id, post_id)
    try:
        receipt = adapter.publish(p["evidence"], doc["actor"], p["draft"]["text"], lambda: state.guard(run_id, post_id))
        state.receipt(run_id, post_id, receipt)
    except BaseException:
        state.receipt(run_id, post_id)
        raise StateError("publication_uncertain_do_not_retry") from None
    return state.status(run_id)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--platform", choices=("threads", "linkedin"), default="threads")
    p.add_argument("--database", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    c = sub.add_parser("collect")
    c.add_argument("--workflow", choices=("listen", "music-audit", "audit", "engage"), default="engage")
    c.add_argument("--mode", choices=("shadow", "live"), default="shadow")
    c.add_argument("--source", choices=("own", "creator", "topic", "post", "organization"), required=True)
    c.add_argument("--target")
    c.add_argument("--account", required=True)
    c.add_argument("--posts", type=positive, required=True)
    c.add_argument("--comments", type=positive, default=100)
    c.add_argument("--candidate-limit", type=positive)
    c.add_argument("--since", type=timestamp)
    c.add_argument("--until", type=timestamp)
    c.add_argument("--api-version", default="202608")
    for name in ("resume", "status", "report"):
        sub.add_parser(name).add_argument("--run-id", required=True)
    for name in ("packet", "store-analysis", "store-draft", "store-review", "show-response", "request-live", "authorize", "publish", "refresh"):
        c = sub.add_parser(name)
        c.add_argument("--run-id", required=True)
        c.add_argument("--post-id", required=True)
        if name.startswith("store-"):
            c.add_argument("--file", type=Path, required=True)
        if name in {"show-response", "authorize"}:
            c.add_argument("--operator", default="workspace-operator")
        if name == "authorize":
            c.add_argument("--presentation-hash", required=True)
            c.add_argument("--approval-token", required=True)
            c.add_argument("--approved", action="store_true", required=True, help="Record only the user's explicit approval of the shown text")
    sub.add_parser("purge-expired")
    sub.add_parser("purge-post").add_argument("--post-id", required=True)
    return p


def execute(args):
    if args.command == "capabilities":
        return capabilities(args.platform)
    scope = scope_from_args(args) if args.command == "collect" else None
    database = args.database or Path("comments_data") / args.platform / "social_engage.sqlite3"
    # Live and offline mutations serialize on the same local database lock.
    with operator_lock(database):
        state = State(database, args.platform)
        try:
            command = args.command
            if command == "collect":
                adapter = make_adapter(scope)  # Missing token doesn't create an empty run.
                run_id = state.create(scope)
                return collect(state, run_id, adapter)
            if command == "resume":
                doc = state.load(args.run_id)
                if doc["collection_verified"]:
                    return state.status(args.run_id)
                return collect(state, args.run_id, make_adapter(doc["scope"]))
            if command == "refresh":
                doc = state.load(args.run_id)
                require(args.post_id in doc["posts"] and args.post_id in doc["inventory"], "refresh_requires_known_run_post")
                require(not doc["posts"][args.post_id].get("deletion_requested"), "deleted_post_cannot_be_refreshed")
                require(state.db.execute("SELECT 1 FROM attempts WHERE actor=? AND target=?", (doc["actor"]["id"], args.post_id)).fetchone() is None, "published_or_uncertain_post_cannot_refresh")
                adapter = make_adapter(doc["scope"])
                require(adapter.identity(doc["scope"]["account"]) == doc["actor"], "refresh_account_mismatch")
                state.refresh(args.run_id, adapter.collect(args.post_id, doc["scope"], doc["actor"]))
                return state.status(args.run_id)
            if command == "status":
                return state.status(args.run_id)
            if command == "packet":
                return state.packet(args.run_id, args.post_id)
            if command.startswith("store-"):
                require(args.file.stat().st_size <= 1048576, "input_file_too_large")
                data = json.loads(args.file.read_text(encoding="utf-8-sig"))
                require(isinstance(data, dict), "stage_input_must_be_object")
                getattr(state, command.removeprefix("store-"))(args.run_id, args.post_id, data)
                return state.status(args.run_id)
            if command == "show-response":
                return state.present(args.run_id, args.post_id, args.operator)
            if command == "request-live":
                state.request_live(args.run_id, args.post_id)
                return state.status(args.run_id)
            if command == "authorize":
                state.authorize(args.run_id, args.post_id, args.presentation_hash, args.approval_token, args.operator)
                return state.status(args.run_id)
            if command == "publish":
                doc, _ = state.ready(args.run_id, args.post_id)  # Fail offline before even reading a token.
                return publish(state, args.run_id, args.post_id, make_adapter(doc["scope"]))
            if command == "report":
                doc = state.load(args.run_id)
                require(doc["scope"]["workflow"] in {"audit", "engage"} and doc["status"] == "collection_complete", "audit_collection_required")
                require(all("analysis" in post for post in doc["posts"].values()), "all_posts_must_be_analyzed")
                return {"schema": "social-engage-audit-v1", "run_id": args.run_id, "provisional": True,
                        "scope": doc["scope"], "posts": [{"id": key, "evidence_hash": post["evidence_hash"], "analysis": post["analysis"]} for key, post in doc["posts"].items()]}
            if command == "purge-expired":
                return state.purge_expired()
            if command == "purge-post":
                return state.purge_expired(args.post_id)
            raise StateError("unsupported_command")
        finally:
            state.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (StateError, AdapterError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print(json.dumps({"status": "blocked", "reason": "invalid_input_or_local_state"}))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "platform_operation_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if result.get("status") in {"collection_incomplete", "evidence_expired"} else 0


if __name__ == "__main__":
    sys.exit(main())
