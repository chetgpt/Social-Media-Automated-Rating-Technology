"""Bounded official Threads setup and diagnostics, without stored credentials.

These helpers never collect durable evidence, publish, or open a browser. Token
refresh is explicit and process-local; it is not OAuth onboarding or a daemon.
"""
from __future__ import annotations

import os
import time

from .adapters import AdapterError, HTTP, ThreadsAdapter, numeric, username


SOURCES = frozenset({"own", "creator", "topic", "post"})
KNOWN_SCOPES = frozenset({
    "threads_basic", "threads_read_replies", "threads_manage_insights",
    "threads_keyword_search", "threads_profile_discovery",
    "threads_content_publish", "threads_manage_replies", "threads_manage_mentions",
})
SAFE_ERRORS = frozenset({
    "invalid_source", "invalid_username", "invalid_target", "target_required",
    "invalid_numeric_api_id", "runtime_access_token_required", "invalid_app_access_token",
    "permission_denied", "rate_limited", "api_request_failed", "invalid_api_response",
    "transport_or_response_failure", "account_mismatch", "invalid_token_refresh_response",
    "invalid_token_debug_response", "access_token_invalid", "access_token_expired",
    "data_access_expired", "token_user_mismatch", "missing_required_scopes", "access_token_invalid_or_expired",
    "invalid_probe_response", "creator_mismatch", "post_mismatch",
})


def required_scopes(source):
    if source not in SOURCES:
        raise AdapterError("invalid_source")
    scopes = ["threads_basic", "threads_read_replies"]
    if source == "topic":
        scopes.append("threads_keyword_search")
    elif source == "creator":
        scopes.append("threads_profile_discovery")
    return scopes


def setup_report(source="own", *, environ=None):
    """Offline report: environment presence only, never credential values."""
    scopes = required_scopes(source)
    environment = os.environ if environ is None else environ
    return {
        "status": "setup_required", "transport": "official", "source": source,
        "environment": {name: bool(environment.get(name)) for name in
                        ("THREADS_ACCESS_TOKEN", "THREADS_APP_ACCESS_TOKEN")},
        "required_collection_scopes": scopes,
        "optional_collection_scopes": ["threads_manage_insights"],
        "permissions_note": "Granted scopes do not prove public search/profile App Review access.",
        "cost_note": "No fee guarantee is made; verify Meta terms before activation. Hosting and AI costs are separate.",
        "token_storage": "Process environment only; tokens are never printed or persisted by this helper.",
        "token_refresh": "Explicit refresh only, retained in memory for this process; no automatic renewal.",
        "links": {
            "setup": "https://developers.facebook.com/docs/threads/get-started/",
            "tokens": "https://developers.facebook.com/docs/threads/get-started/long-lived-tokens/",
            "official_collection": "https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api",
            "sample_app": "https://github.com/fbsamples/threads_api",
        },
    }


def _valid_token(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and not any(ord(char) < 32 for char in value))


def _auth_request(session, path, bearer, params):
    """Read-only auth endpoints, separate from the adapter's publication guard."""
    if path not in {"/refresh_access_token", "/debug_token"}:
        raise AdapterError("invalid_api_response")
    # Reuse fixed status/Graph-code handling, timeout and redirect protection.
    # Only GET is exposed here, so the publication write guard remains intact.
    return HTTP(bearer, "https://graph.threads.net", session=session).request("GET", path, params=params)


def _close(session):
    try:
        session.close()
    except Exception:
        pass


def build_adapter(refresh_token=False, environ=None, session=None):
    """Build a caller-owned adapter; the caller closes its HTTP session.

    An explicitly requested refresh returns only safe lifecycle metadata. The
    replacement credential exists solely inside the adapter, never in os.environ.
    """
    environment = os.environ if environ is None else environ
    adapter = ThreadsAdapter(environment.get("THREADS_ACCESS_TOKEN", ""), session=session)
    adapter.token_lifecycle = {"status": "environment_token", "expiry_known": False,
                               "persisted": False, "automatic_renewal": False}
    if not refresh_token:
        return adapter
    try:
        payload = _auth_request(adapter.http.session, "/refresh_access_token", adapter.http._token,
                                {"grant_type": "th_refresh_token"})
        token, seconds = payload.get("access_token"), payload.get("expires_in")
        if not _valid_token(token) or type(seconds) is not int or seconds <= 0:
            raise AdapterError("invalid_token_refresh_response")
        adapter = ThreadsAdapter(token, session=adapter.http.session)
        adapter.token_lifecycle = {"status": "refreshed_in_memory", "expiry_known": True,
            "expires_in": seconds, "expires_at": int(time.time()) + seconds,
            "persisted": False, "automatic_renewal": False}
        return adapter
    except Exception:
        _close(adapter.http.session)
        raise


def _validate_scope(account, source, target, probe):
    required_scopes(source)
    account = username(account)
    if source == "own":
        if target not in (None, ""):
            raise AdapterError("invalid_target")
        return account, None
    if target in (None, ""):
        if probe:
            raise AdapterError("target_required")
        return account, None
    if source == "creator":
        target = username(target)
    elif source == "post":
        target = numeric(target)
    elif (not isinstance(target, str) or target != target.strip() or len(target) > 500
          or any(ord(char) < 32 for char in target)):
        raise AdapterError("invalid_target")
    return account, target


def _debug_token(adapter, app_token, actor, source):
    if not _valid_token(app_token):
        raise AdapterError("invalid_app_access_token")
    payload = _auth_request(adapter.http.session, "/debug_token", app_token,
                            {"input_token": adapter.http._token})
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AdapterError("invalid_token_debug_response")
    if data.get("is_valid") is not True:
        raise AdapterError("access_token_invalid")
    expiries = {}
    for field, error in (("expires_at", "access_token_expired"),
                         ("data_access_expires_at", "data_access_expired")):
        expiry = data.get(field)
        if type(expiry) is not int or expiry < 0:
            raise AdapterError("invalid_token_debug_response")
        if expiry and expiry <= time.time():
            raise AdapterError(error)
        expiries[field] = expiry or None
    if numeric(data.get("user_id")) != actor["id"]:
        raise AdapterError("token_user_mismatch")
    scopes = data.get("scopes")
    if not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes):
        raise AdapterError("invalid_token_debug_response")
    granted = sorted(set(scopes) & KNOWN_SCOPES)
    return {"status": "checked", "granted_scopes": granted,
        "required_collection_scopes": required_scopes(source),
        "missing_scopes": sorted(set(required_scopes(source)) - set(granted)),
        "app_review_access": "unknown", **expiries}


def _probe(adapter, actor, source, target):
    params = {"fields": "id,username", "limit": 1}
    if source == "post":
        path, params = "/" + target, {"fields": "id,username"}
    elif source == "own":
        path = "/me/threads"
    elif source == "creator":
        path, params["username"] = "/profile_posts", target
    else:
        path = "/keyword_search"
        params.update(q=target, search_type="RECENT", search_mode="KEYWORD")
    payload = adapter.http.request("GET", path, params=params)
    rows = [payload] if source == "post" else payload.get("data")
    if not isinstance(rows, list) or len(rows) > 1:
        raise AdapterError("invalid_probe_response")
    different_author = False
    for row in rows:
        if not isinstance(row, dict):
            raise AdapterError("invalid_probe_response")
        post_id, author = numeric(row.get("id")), username(row.get("username"))
        if source == "post" and post_id != target:
            raise AdapterError("post_mismatch")
        if (source == "own" and author != actor["username"]) or (source == "creator" and author != target):
            raise AdapterError("creator_mismatch")
        different_author = author != actor["username"]
    return {"status": "endpoint_accepted", "result_count": len(rows),
        "coverage": "one_result_only" if rows else "zero_results_inconclusive",
        "public_access": "different_account_result_observed" if different_author else "unknown",
        "app_review_access": "unknown", "reply_access": "not_tested", "pagination": "not_tested"}


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def official_check(account, source="own", target=None, probe=False, refresh_token=False,
                   environ=None, session=None):
    """Identity, optional token diagnostics and at most one source request.

    Errors are fixed codes. No raw content, URLs, permissions payloads or secrets
    are returned, and the diagnostic session is always closed.
    """
    environment = os.environ if environ is None else environ
    secrets = [environment.get("THREADS_ACCESS_TOKEN"), environment.get("THREADS_APP_ACCESS_TOKEN")]
    result = {"status": "blocked", "transport": "official",
        "permissions": {"status": "unknown", "reason": "app_access_token_not_configured",
                        "app_review_access": "unknown"},
        "source_probe": {"status": "not_requested"}, "collection_performed": False}
    adapter = None
    try:
        account, target = _validate_scope(account, source, target, probe)
        result["source"] = source
        adapter = build_adapter(refresh_token=refresh_token, environ=environment, session=session)
        secrets.append(adapter.http._token)
        result["token_lifecycle"] = adapter.token_lifecycle
        actor = adapter.identity(account)
        result["actor"] = actor
        app_token = environment.get("THREADS_APP_ACCESS_TOKEN")
        if app_token:
            result["permissions"] = _debug_token(adapter, app_token, actor, source)
            if result["permissions"]["missing_scopes"]:
                raise AdapterError("missing_required_scopes")
        if probe:
            result["source_probe"] = _probe(adapter, actor, source, target)
        result["status"] = "identity_checked"
        result["readiness"] = "not_established_by_diagnostics"
    except AdapterError as exc:
        result["code"] = str(exc) if str(exc) in SAFE_ERRORS else "diagnostic_failed"
    except Exception:
        result["code"] = "diagnostic_failed"
    finally:
        if adapter is not None:
            _close(adapter.http.session)
        elif session is not None:
            _close(session)
    return _redact(result, secrets)
