"""Local-only intake tests; synthetic tokens, no real vault or Threads API."""
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlencode

import pytest

import threads_official_connect as connect


TOKEN = "synthetic-threads-secret"
APP_TOKEN = "synthetic-app-secret"


def diagnostic(account="operator", status="identity_checked", probe="endpoint_accepted"):
    return {"status": status, "actor": {"username": account, "id": "42"},
            "source_probe": {"status": probe}}


def exchange(server, method="GET", path=None, headers=None, body=None):
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        client.request(method, server.path if path is None else path, body=body,
                       headers={"Host": server.host, **(headers or {})})
        response = client.getresponse()
        return response.status, dict(response.headers), response.read().decode()
    finally:
        client.close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def post(server, **changes):
    headers = {"Origin": server.origin, "Content-Type": "application/x-www-form-urlencoded"}
    headers.update(changes.pop("headers", {}))
    return exchange(server, method="POST", body=changes.pop("body", urlencode({"token": TOKEN})),
                    headers=headers, **changes)


def rejected(operation, status):
    try:
        assert operation()[0] == status
    except (OSError, http.client.HTTPException):
        # Windows may reset a connection rejected before its body is consumed.
        pass


def test_form_is_local_ephemeral_and_has_no_token_or_remote_resources(capsys):
    with connect.TokenInputServer() as server:
        status, headers, body = exchange(server)
        assert server.server_address[0] == "127.0.0.1"
        assert len(server.path) >= 40 and "?" not in server.url
        assert status == 200 and 'type="password"' in body
        assert TOKEN not in body and "http" not in body
        assert headers["Cache-Control"].startswith("no-store")
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert not any(key.startswith("Access-Control-") for key in headers)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("changes,status", [
    ({"headers": {"Host": "attacker.invalid"}}, 403),
    ({"headers": {"Origin": "https://attacker.invalid"}}, 403),
    ({"headers": {"Origin": "null"}}, 403),
    ({"headers": {"Content-Type": "text/plain"}}, 403),
    ({"headers": {"Transfer-Encoding": "chunked"}}, 403),
    ({"headers": {"Content-Length": "9000"}}, 413),
    ({"path": "/wrong"}, 403),
    ({"body": "token="}, 400),
    ({"body": "token=a&token=b"}, 400),
    ({"body": "token=a&extra=b"}, 400),
    ({"body": "token=%0Atest"}, 400),
])
def test_rejected_requests_never_consume_or_replace_token(changes, status):
    with connect.TokenInputServer() as server:
        rejected(lambda: post(server, **changes), status)
        assert server._token is None and server._consumed is False
        assert post(server)[0] == 200
        assert server._token == TOKEN


def test_query_and_missing_origin_rejected():
    with connect.TokenInputServer() as server:
        rejected(lambda: post(server, path=server.path + "?token=" + TOKEN), 403)
        rejected(lambda: exchange(server, method="POST", body="token=" + TOKEN,
                 headers={"Content-Type": "application/x-www-form-urlencoded"}), 403)
        assert server._token is None


def test_standard_browser_form_content_type_with_charset_is_accepted():
    with connect.TokenInputServer() as server:
        assert post(server, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        })[0] == 200
        assert server._token == TOKEN


def test_same_origin_browser_navigation_without_origin_is_accepted():
    with connect.TokenInputServer() as server:
        status, _, _ = exchange(server, method="POST", body=urlencode({"token": TOKEN}), headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
        })
        assert status == 200 and server._token == TOKEN


def test_same_origin_embedded_navigation_with_opaque_origin_is_accepted():
    with connect.TokenInputServer() as server:
        status, _, _ = exchange(server, method="POST", body=urlencode({"token": TOKEN}), headers={
            "Origin": "null",
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
        })
        assert status == 200 and server._token == TOKEN


def test_submission_is_one_shot_and_take_clears_memory(capsys):
    with connect.TokenInputServer() as server:
        assert post(server)[0] == 200
        rejected(lambda: post(server, body="token=replacement"), 410)
        assert exchange(server)[0] == 410
        assert server.take_token() == TOKEN
        assert server._token is None
    assert capsys.readouterr() == ("", "")


def test_expired_form_rejects_and_take_fails_closed():
    with connect.TokenInputServer() as server:
        server.deadline = time.monotonic() - 1
        rejected(lambda: post(server), 410)
        assert server._token is None
        with pytest.raises(connect.IntakeError, match="token_input_expired"):
            server.take_token()


def test_partial_request_cannot_extend_absolute_deadline():
    with connect.TokenInputServer(timeout=0.15) as server:
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        with socket.create_connection(server.server_address, timeout=1) as client:
            client.sendall(b"GET ")
            thread.join(timeout=2)
        assert not thread.is_alive() and server._token is None


@pytest.mark.parametrize("timeout", [0, -1, 601, True, None])
def test_deadline_is_bounded(timeout):
    with pytest.raises(connect.IntakeError, match="invalid_input_timeout"):
        connect.TokenInputServer(timeout)


@pytest.mark.parametrize("report", [diagnostic("wrong_actor"),
    diagnostic(status="blocked"), diagnostic(probe="not_requested"), {}])
def test_identity_or_probe_failure_never_saves(report):
    saves = []
    result = connect.connect_token("123", "operator", TOKEN, environ={},
        check=lambda *args, **kwargs: report, save=lambda *args: saves.append(args))
    assert result["status"] == "blocked" and result["saved"] is False
    assert not saves


def test_verified_account_then_save_and_safe_metadata():
    events = []

    def check(account, **kwargs):
        events.append("check")
        assert account == "operator" and kwargs["source"] == "own" and kwargs["probe"] is True
        assert kwargs["environ"] == {"THREADS_ACCESS_TOKEN": TOKEN, "THREADS_APP_ACCESS_TOKEN": APP_TOKEN}
        return diagnostic()

    def save(*args):
        assert args == ("123", "operator", TOKEN)
        events.append("save")

    environment = {"THREADS_ACCESS_TOKEN": "old-secret", "THREADS_APP_ACCESS_TOKEN": APP_TOKEN}
    result = connect.connect_token("123", "@Operator", TOKEN, environ=environment, check=check, save=save)
    assert events == ["check", "save"]
    assert result["status"] == "connected" and result["saved"] is True
    assert "123" in result["credential"]["target"] and "operator" in result["credential"]["target"]
    assert result["app_binding"] == "selected_vault_namespace_only"
    assert environment["THREADS_ACCESS_TOKEN"] == "old-secret"
    assert all(secret not in json.dumps(result) for secret in (TOKEN, APP_TOKEN))


def test_untrusted_error_payload_cannot_echo_any_secret():
    result = connect.connect_token("123", "operator", TOKEN, environ={"THREADS_APP_ACCESS_TOKEN": APP_TOKEN},
        check=lambda *args, **kwargs: {"status": "blocked", TOKEN: {"message": TOKEN + APP_TOKEN}},
        save=lambda *args: pytest.fail("save must not run"))
    assert TOKEN not in json.dumps(result) and APP_TOKEN not in json.dumps(result)


@pytest.mark.parametrize("stage", ["check", "save"])
def test_secret_bearing_exception_is_closed(stage):
    def fail(*args, **kwargs):
        raise RuntimeError(TOKEN + APP_TOKEN)

    result = connect.connect_token("123", "operator", TOKEN, environ={},
        check=fail if stage == "check" else lambda *args, **kwargs: diagnostic(),
        save=fail)
    assert result == {"status": "blocked", "code": "connection_failed", "saved": False}


def test_non_tty_refuses_getpass_fallback(monkeypatch):
    monkeypatch.setattr(connect.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(connect.getpass, "getpass", lambda *args: pytest.fail("must not read"))
    with pytest.raises(connect.IntakeError, match="hidden_terminal_required_use_browser_input"):
        connect.hidden_token()


@pytest.mark.parametrize("token", ["", " padded ", "abc\n", "a" * 1281])
def test_invalid_token_never_reaches_network_or_vault(token):
    result = connect.connect_token("123", "operator", token, environ={},
        check=lambda *args, **kwargs: pytest.fail("network must not run"),
        save=lambda *args: pytest.fail("save must not run"))
    assert result["status"] == "blocked" and result["saved"] is False


def test_main_prints_only_sanitized_result(monkeypatch, capsys):
    monkeypatch.setattr(connect, "hidden_token", lambda: TOKEN)
    monkeypatch.setattr(connect, "official_check", lambda *args, **kwargs: diagnostic("wrong_actor"))
    assert connect.main(["--app-id", "123", "--account", "operator"]) == 1
    out, err = capsys.readouterr()
    assert TOKEN not in out + err and json.loads(out)["saved"] is False
