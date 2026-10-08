"""Official setup diagnostics use only injected HTTP, never live accounts."""
import json
import time

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_official import build_adapter, official_check, setup_report


USER_TOKEN = "fixture-user-secret"
APP_TOKEN = "fixture-app-secret"
NEW_TOKEN = "fixture-refreshed-secret"
ENV = {"THREADS_ACCESS_TOKEN": USER_TOKEN, "THREADS_APP_ACCESS_TOKEN": APP_TOKEN}
IDENTITY = {"id": "88", "username": "operator"}


class Session:
    def __init__(self, responses=()):
        self.responses, self.calls, self.closed = list(responses), [], False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body = value if isinstance(value, tuple) else (200, value)

        class Response:
            status_code = status

            def json(self):
                return body
        return Response()

    def close(self):
        self.closed = True


def debug(**changes):
    data = {"is_valid": True, "user_id": "88", "expires_at": int(time.time()) + 3600,
            "data_access_expires_at": 0,
            "scopes": ["threads_basic", "threads_read_replies", "threads_keyword_search"]}
    data.update(changes)
    return {"data": data}


def test_setup_is_offline_and_exposes_only_env_presence():
    report = setup_report("creator", environ=ENV)
    assert report["environment"] == {"THREADS_ACCESS_TOKEN": True, "THREADS_APP_ACCESS_TOKEN": True}
    assert report["required_collection_scopes"] == ["threads_basic", "threads_read_replies", "threads_profile_discovery"]
    assert all(value not in json.dumps(report) for value in ENV.values())
    assert setup_report(environ={})["environment"]["THREADS_ACCESS_TOKEN"] is False


def test_environment_token_construction_does_not_call_network():
    session = Session()
    adapter = build_adapter(environ=ENV, session=session)
    assert not session.calls and not session.closed
    assert adapter.token_lifecycle["expiry_known"] is False
    assert adapter.http.headers["Authorization"] == "Bearer " + USER_TOKEN


def test_refresh_is_explicit_memory_only_and_uses_fixed_unversioned_host():
    environment = dict(ENV)
    session = Session([{"access_token": NEW_TOKEN, "expires_in": 5184000}])
    adapter = build_adapter(True, environment, session)
    assert environment == ENV
    assert adapter.http.headers["Authorization"] == "Bearer " + NEW_TOKEN
    assert adapter.token_lifecycle["status"] == "refreshed_in_memory"
    assert adapter.token_lifecycle["expires_in"] == 5184000
    assert NEW_TOKEN not in json.dumps(adapter.token_lifecycle)
    method, url, kwargs = session.calls[0]
    assert method == "GET" and url == "https://graph.threads.net/refresh_access_token"
    assert kwargs["params"] == {"grant_type": "th_refresh_token"}
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 30
    assert kwargs["headers"]["Authorization"] == "Bearer " + USER_TOKEN


@pytest.mark.parametrize("payload", [{}, {"access_token": "", "expires_in": 30},
    {"access_token": NEW_TOKEN, "expires_in": 0}, {"access_token": NEW_TOKEN, "expires_in": True},
    {"access_token": NEW_TOKEN + "\n", "expires_in": 30}])
def test_refresh_rejects_invalid_response_and_closes(payload):
    session = Session([payload])
    with pytest.raises(AdapterError, match="invalid_token_refresh_response"):
        build_adapter(True, ENV, session)
    assert session.closed


@pytest.mark.parametrize("status,code", [(302, "api_request_failed"), (401, "permission_denied"),
    (403, "permission_denied"), (429, "rate_limited"), (500, "api_request_failed")])
def test_refresh_denial_has_no_retry_or_raw_error(status, code):
    session = Session([(status, {"error": USER_TOKEN + APP_TOKEN})])
    report = official_check("operator", refresh_token=True, environ=ENV, session=session)
    assert report["code"] == code and report["status"] == "blocked"
    assert len(session.calls) == 1 and session.closed
    assert USER_TOKEN not in json.dumps(report) and APP_TOKEN not in json.dumps(report)


@pytest.mark.parametrize("graph_code,code", [(190, "access_token_invalid_or_expired"),
    (10, "permission_denied"), (4, "rate_limited")])
def test_auth_graph_errors_use_closed_codes(graph_code, code):
    session = Session([(400, {"error": {"code": graph_code, "message": USER_TOKEN + APP_TOKEN}})])
    report = official_check("operator", refresh_token=True, environ=ENV, session=session)
    assert report["code"] == code
    assert len(session.calls) == 1 and session.closed
    assert USER_TOKEN not in json.dumps(report) and APP_TOKEN not in json.dumps(report)


def test_missing_token_does_not_call_network():
    session = Session()
    result = official_check("operator", environ={}, session=session)
    assert result["code"] == "runtime_access_token_required"
    assert not session.calls and session.closed


def test_identity_without_debugger_does_not_claim_permissions():
    session = Session([IDENTITY])
    report = official_check("@operator", environ={"THREADS_ACCESS_TOKEN": USER_TOKEN}, session=session)
    assert report["status"] == "identity_checked"
    assert report["permissions"]["status"] == "unknown"
    assert report["readiness"] == "not_established_by_diagnostics"
    assert len(session.calls) == 1 and session.closed


def test_wrong_authorized_account_blocks_before_debug_or_probe():
    session = Session([{"id": "99", "username": "different_operator", "secret": USER_TOKEN}])
    report = official_check("operator", source="topic", target="pottery", probe=True, environ=ENV, session=session)
    assert report["code"] == "account_mismatch"
    assert len(session.calls) == 1 and session.closed
    assert "different_operator" not in json.dumps(report) and USER_TOKEN not in json.dumps(report)


def test_debugger_uses_app_bearer_filters_scopes_and_does_not_claim_app_review():
    session = Session([IDENTITY, debug(scopes=["threads_basic", "threads_read_replies", APP_TOKEN])])
    report = official_check("operator", environ=ENV, session=session)
    assert report["status"] == "identity_checked"
    assert report["permissions"]["granted_scopes"] == ["threads_basic", "threads_read_replies"]
    assert report["permissions"]["app_review_access"] == "unknown"
    method, url, kwargs = session.calls[1]
    assert method == "GET" and url == "https://graph.threads.net/debug_token"
    assert kwargs["params"] == {"input_token": USER_TOKEN}
    assert kwargs["headers"]["Authorization"] == "Bearer " + APP_TOKEN
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 30
    assert APP_TOKEN not in json.dumps(report) and USER_TOKEN not in json.dumps(report)


@pytest.mark.parametrize("changes,code", [({"is_valid": False}, "access_token_invalid"),
    ({"expires_at": 1}, "access_token_expired"), ({"data_access_expires_at": 1}, "data_access_expired"),
    ({"user_id": "99"}, "token_user_mismatch"), ({"expires_at": "tomorrow"}, "invalid_token_debug_response"),
    ({"scopes": ["threads_basic"]}, "missing_required_scopes")])
def test_debugger_blocks_expired_mismatched_or_insufficient_tokens(changes, code):
    session = Session([IDENTITY, debug(**changes)])
    report = official_check("operator", source="topic", target="pottery", probe=True, environ=ENV, session=session)
    assert report["status"] == "blocked" and report["code"] == code
    assert len(session.calls) == 2 and session.closed


@pytest.mark.parametrize("source,target,path,params", [
    ("own", None, "/me/threads", {}), ("creator", "maker", "/profile_posts", {"username": "maker"}),
    ("topic", "hand built pots", "/keyword_search", {"q": "hand built pots", "search_type": "RECENT", "search_mode": "KEYWORD"}),
    ("post", "123", "/123", {}),
])
def test_probe_uses_one_source_read_no_paging_or_hydration(source, target, path, params):
    row = {"id": "123", "username": "operator" if source == "own" else "maker"}
    payload = row if source == "post" else {"data": [row], "paging": {"next": "https://evil.invalid/"}}
    session = Session([IDENTITY, payload])
    report = official_check("operator", source=source, target=target, probe=True,
                            environ={"THREADS_ACCESS_TOKEN": USER_TOKEN}, session=session)
    assert report["status"] == "identity_checked"
    assert report["source_probe"]["result_count"] == 1
    assert report["source_probe"]["pagination"] == "not_tested"
    assert report["source_probe"]["public_access"] == ("unknown" if source == "own" else "different_account_result_observed")
    assert len(session.calls) == 2 and session.closed
    method, url, kwargs = session.calls[1]
    assert method == "GET" and url == "https://graph.threads.net/v1.0" + path
    assert kwargs["params"] == {"fields": "id,username", **({} if source == "post" else {"limit": 1}), **params}
    assert "maker" not in json.dumps(report) and "123" not in json.dumps(report)


def test_empty_probe_is_inconclusive_and_does_not_imply_public_permission():
    session = Session([IDENTITY, {"data": []}])
    report = official_check("operator", source="topic", target="pottery", probe=True,
                            environ={"THREADS_ACCESS_TOKEN": USER_TOKEN}, session=session)
    assert report["source_probe"]["coverage"] == "zero_results_inconclusive"
    assert report["source_probe"]["public_access"] == "unknown"


@pytest.mark.parametrize("source,target,payload,code", [
    ("creator", "maker", {"data": [{"id": "123", "username": "someone_else"}]}, "creator_mismatch"),
    ("own", None, {"data": [{"id": "123", "username": "someone_else"}]}, "creator_mismatch"),
    ("post", "123", {"id": "456", "username": "maker"}, "post_mismatch"),
])
def test_source_probe_cannot_validate_wrong_owner_or_post(source, target, payload, code):
    session = Session([IDENTITY, payload])
    report = official_check("operator", source=source, target=target, probe=True,
                            environ={"THREADS_ACCESS_TOKEN": USER_TOKEN}, session=session)
    assert report["status"] == "blocked" and report["code"] == code
    assert len(session.calls) == 2 and session.closed


@pytest.mark.parametrize("arguments", [
    {"source": "unknown"}, {"account": "https://threads.com/@operator"},
    {"source": "own", "target": "extra"}, {"source": "creator", "probe": True},
    {"source": "creator", "target": "https://evil.invalid"},
    {"source": "post", "target": "../me"}, {"source": "topic", "target": "unsafe\nquery"},
])
def test_invalid_scope_never_reaches_network(arguments):
    session = Session()
    arguments = {"account": "operator", **arguments}
    result = official_check(**arguments, environ=ENV, session=session)
    assert result["status"] == "blocked" and "code" in result
    assert not session.calls and session.closed


def test_transport_failure_and_malicious_content_never_leak_secrets():
    session = Session([RuntimeError(USER_TOKEN + APP_TOKEN)])
    result = official_check("operator", environ=ENV, session=session)
    assert result["code"] == "transport_or_response_failure"
    assert all(secret not in json.dumps(result) for secret in ENV.values())
    assert session.closed


def test_refreshed_token_is_used_by_identity_and_debug_without_being_output():
    session = Session([{"access_token": NEW_TOKEN, "expires_in": 5184000}, IDENTITY, debug()])
    report = official_check("operator", refresh_token=True, environ=ENV, session=session)
    assert report["status"] == "identity_checked"
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer " + NEW_TOKEN
    assert session.calls[2][2]["params"]["input_token"] == NEW_TOKEN
    assert all(secret not in json.dumps(report) for secret in (USER_TOKEN, APP_TOKEN, NEW_TOKEN))
