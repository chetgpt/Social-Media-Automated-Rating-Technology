"""Verify and save one Threads user token without CLI or file-based secrets.

The optional loopback form is a temporary local input surface, not an OAuth
callback. It never opens a browser, publishes content, or creates workflow data.
"""
from __future__ import annotations

import argparse
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import socket
import sys
import threading
import time
from urllib.parse import parse_qs
import warnings

from social_engage.adapters import numeric, username
from social_engage.threads_credentials import credential_target
from social_engage.threads_official import official_check


MAX_BODY = 8192
MAX_SECONDS = 600


class IntakeError(RuntimeError):
    """A closed code only; never include a credential or request details."""


def _valid_token(value):
    return (isinstance(value, str) and 0 < len(value) <= 1280
            and value == value.strip() and all(32 < ord(char) < 127 for char in value))


def _redact(value, secret_values):
    if isinstance(value, str):
        for secret in secret_values:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_redact(key, secret_values): _redact(item, secret_values)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret_values) for item in value]
    return value


class TokenInputServer(HTTPServer):
    """One bounded password submission, accepted only from its own origin."""

    allow_reuse_address = False

    def __init__(self, timeout=MAX_SECONDS):
        if type(timeout) not in (int, float) or not 0 < timeout <= MAX_SECONDS:
            raise IntakeError("invalid_input_timeout")
        self.deadline = time.monotonic() + timeout
        self._token = None
        self._consumed = False
        self._connection_timer = None
        self.path = "/" + secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _TokenHandler)
        self.host = "127.0.0.1:" + str(self.server_port)
        self.origin = "http://" + self.host
        self.url = self.origin + self.path
        self.timeout = min(0.25, timeout)

    def get_request(self):
        connection, address = super().get_request()
        # A partial local request cannot hold the intake beyond its deadline.
        remaining = max(0.001, self.deadline - time.monotonic())
        connection.settimeout(min(5.0, remaining))
        # Socket timeouts alone restart on each byte; enforce total lifetime as
        # well so a slowly streamed header/body cannot extend the input window.
        self._connection_timer = threading.Timer(remaining, self._expire_connection, (connection,))
        self._connection_timer.daemon = True
        self._connection_timer.start()
        return connection, address

    @staticmethod
    def _expire_connection(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def shutdown_request(self, request):
        if self._connection_timer is not None:
            self._connection_timer.cancel()
            self._connection_timer = None
        super().shutdown_request(request)

    def server_close(self):
        if self._connection_timer is not None:
            self._connection_timer.cancel()
            self._connection_timer = None
        super().server_close()

    def handle_error(self, request, client_address):
        # BaseServer's traceback can contain request-derived or secret data.
        pass

    def take_token(self):
        try:
            while self._token is None and time.monotonic() < self.deadline:
                self.handle_request()
            if self._token is None:
                raise IntakeError("token_input_expired")
            token, self._token = self._token, None
            return token
        finally:
            self.server_close()


class _TokenHandler(BaseHTTPRequestHandler):
    server_version = "LocalTokenInput"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _reply(self, status, body):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(payload)
        except (OSError, socket.timeout):
            pass

    def send_error(self, code, message=None, explain=None):
        self._reply(code, "Request rejected.")

    def _allowed(self):
        server = self.server
        if server._consumed or time.monotonic() >= server.deadline:
            self._reply(410, "Input session ended.")
            return False
        if (self.client_address[0] != "127.0.0.1"
                or self.headers.get_all("Host") != [server.host]
                or self.path != server.path):
            self._reply(403, "Request rejected (local route).")
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        self._reply(200, '<!doctype html><html lang="en"><meta charset="utf-8">'
                    '<title>Connect Threads</title><h1>Connect Threads</h1>'
                    '<p>The token stays on this computer. It will be checked against '
                    'the intended Threads account before saving in Windows Credential Manager.</p>'
                    '<form method="post" autocomplete="off">'
                    '<label for="token">Threads user access token</label>'
                    '<input id="token" name="token" type="password" autocomplete="off" '
                    'required maxlength="1280">'
                    '<button type="submit">Verify and save</button></form></html>')

    def do_POST(self):
        if not self._allowed():
            return
        origins = self.headers.get_all("Origin") or []
        navigation_state = ("same_origin_navigation" if
                            (self.headers.get_all("Sec-Fetch-Site") == ["same-origin"] and
                             self.headers.get_all("Sec-Fetch-Mode") == ["navigate"])
                            else "other")
        # The embedded browser legitimately uses an opaque Origin for local
        # navigations. Sec-Fetch headers are browser-controlled, so an exact
        # same-origin navigation remains a valid anti-cross-site signal.
        same_origin_form = (origins == [self.server.origin] or
                            navigation_state == "same_origin_navigation")
        content_types = self.headers.get_all("Content-Type") or []
        form_content_type = (len(content_types) == 1 and
                             content_types[0].split(";", 1)[0].strip().lower()
                             == "application/x-www-form-urlencoded")
        if (not same_origin_form
                or not form_content_type
                or self.headers.get("Transfer-Encoding") is not None):
            self._reply(403, "Request rejected.")
            return
        lengths = self.headers.get_all("Content-Length") or []
        if (len(lengths) != 1 or not lengths[0].isdigit()
                or not 0 < int(lengths[0]) <= MAX_BODY):
            self._reply(413, "Request rejected.")
            return
        try:
            size = int(lengths[0])
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError
            form = parse_qs(body.decode("ascii"), keep_blank_values=True,
                            strict_parsing=True, encoding="utf-8", errors="strict", max_num_fields=1)
            values = form.get("token", [])
            if set(form) != {"token"} or len(values) != 1 or not _valid_token(values[0]):
                raise ValueError
        except (ValueError, UnicodeError, OSError):
            self._reply(400, "Request rejected.")
            return
        if time.monotonic() >= self.server.deadline:
            self._reply(410, "Input session ended.")
            return
        self.server._token = values[0]
        self.server._consumed = True
        self._reply(200, "Token received. Return to the task for verification results. You can close this tab.")


def hidden_token():
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise IntakeError("hidden_terminal_required_use_browser_input")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Threads user access token (hidden): ")
        except (getpass.GetPassWarning, EOFError):
            raise IntakeError("hidden_terminal_required_use_browser_input") from None


def connect_token(app_id, account, token, *, environ=None, check=None, save=None):
    """Only a verified exact-account token reaches the local credential store."""
    environment = os.environ if environ is None else environ
    secret_values = [token, environment.get("THREADS_APP_ACCESS_TOKEN")]
    try:
        app_id, account = numeric(app_id), username(account)
        if not _valid_token(token):
            raise IntakeError("invalid_token_input")
        diagnostics = (check or official_check)(account, source="own", probe=True, environ={
            "THREADS_ACCESS_TOKEN": token,
            "THREADS_APP_ACCESS_TOKEN": environment.get("THREADS_APP_ACCESS_TOKEN", ""),
        })
        if (diagnostics.get("status") != "identity_checked"
                or diagnostics.get("actor", {}).get("username") != account
                or diagnostics.get("source_probe", {}).get("status") != "endpoint_accepted"):
            return _redact({"status": "blocked", "code": "token_not_verified",
                            "saved": False, "diagnostics": diagnostics}, secret_values)
        if save is None:
            from social_engage.threads_credentials import save_token
            save = save_token
        save(app_id, account, token)
        return _redact({"status": "connected", "saved": True, "account": account,
                        "app_id": app_id, "app_binding": "selected_vault_namespace_only",
                        "credential": {"target": credential_target(app_id, account),
                                       "storage": "Windows Credential Manager"},
                        "diagnostics": diagnostics,
                        "publication_performed": False}, secret_values)
    except Exception:
        # Provider exceptions, ctypes failures, and callbacks must not leak data.
        return {"status": "blocked", "code": "connection_failed", "saved": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--browser-input", action="store_true")
    args = parser.parse_args(argv)
    try:
        app_id, account = numeric(args.app_id), username(args.account)
        if args.browser_input:
            with TokenInputServer() as intake:
                print(json.dumps({"status": "awaiting_token", "url": intake.url,
                                  "expires_in": MAX_SECONDS, "account": account}), flush=True)
                token = intake.take_token()
        else:
            token = hidden_token()
        result = connect_token(app_id, account, token)
        token = None
    except IntakeError as exc:
        result = {"status": "blocked", "code": str(exc), "saved": False}
    except KeyboardInterrupt:
        result = {"status": "blocked", "code": "input_cancelled", "saved": False}
    except Exception:
        result = {"status": "blocked", "code": "connection_failed", "saved": False}
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["status"] == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
