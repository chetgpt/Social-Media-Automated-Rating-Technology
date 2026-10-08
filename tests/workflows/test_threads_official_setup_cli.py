"""Official setup and token maintenance must not drift into browser/state access."""
import json

import pytest

import engage_social as cli
from social_engage.state import State, StateError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("requests.sessions.Session.request",
                        lambda *a, **k: pytest.fail("unexpected live HTTP"))


def test_setup_and_diagnostics_never_open_workflow_database(monkeypatch, tmp_path):
    from social_engage import threads_official
    monkeypatch.setattr(cli, "State", lambda *a: pytest.fail("database opened"))
    monkeypatch.setattr(cli, "operator_lock", lambda *a: pytest.fail("database lock opened"))
    monkeypatch.setattr(cli, "make_adapter", lambda *a, **k: pytest.fail("generic adapter opened"))
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "synthetic-secret-do-not-output")
    result = cli.execute(cli.parser().parse_args(["official-setup", "--source", "topic"]))
    assert "synthetic-secret" not in json.dumps(result)
    calls = []
    monkeypatch.setattr(threads_official, "official_check",
                        lambda *a, **k: calls.append((a, k)) or {"status": "identity_checked"})
    result = cli.execute(cli.parser().parse_args(["--database", str(tmp_path / "unused.db"),
        "official-check", "--account", "operator", "--source", "topic", "--target", "pottery", "--probe"]))
    assert result["status"] == "identity_checked"
    assert calls == [(("operator",), {"source": "topic", "target": "pottery", "probe": True, "refresh_token": False})]
    assert not (tmp_path / "unused.db").exists()


@pytest.mark.parametrize("command", ["official-setup", "official-check"])
def test_threads_setup_commands_reject_linkedin_before_any_access(monkeypatch, command):
    monkeypatch.setattr(cli, "State", lambda *a: pytest.fail("database opened"))
    args = ["--platform", "linkedin", command]
    if command == "official-check":
        args += ["--account", "operator"]
    with pytest.raises(StateError, match="official_setup_requires_threads"):
        cli.execute(cli.parser().parse_args(args))


@pytest.mark.parametrize("args", [
    ["check-access", "--refresh-token"],
    ["--platform", "linkedin", "check-access", "--transport", "official", "--account", "urn:li:organization:123", "--refresh-token"],
    ["collect", "--source", "own", "--account", "operator", "--posts", "1", "--refresh-token"],
])
def test_refresh_does_not_create_database_or_open_browser(monkeypatch, args):
    monkeypatch.setattr(cli, "State", lambda *a: pytest.fail("database opened"))
    monkeypatch.setattr(cli, "make_adapter", lambda *a, **k: pytest.fail("adapter opened"))
    with pytest.raises(StateError, match="token_refresh_requires_threads_official_transport"):
        cli.execute(cli.parser().parse_args(args))


class Adapter:
    def __init__(self):
        self.closed = False
        self.token_lifecycle = {"status": "refreshed_in_memory", "expires_in": 5184000}

    def identity(self, expected):
        return {"id": "88", "username": expected}

    def discover(self, scope):
        return ["123"]

    def collect(self, pid, scope, actor):
        return {"id": pid, "platform": "threads", "text": "Fixture", "comments": []}

    def close(self):
        self.closed = True


def test_official_refresh_does_not_change_frozen_scope_or_parent_token(monkeypatch, tmp_path):
    from social_engage import threads_official
    adapter, calls = Adapter(), []
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "original-fixture-token")
    monkeypatch.setattr(threads_official, "build_adapter",
                        lambda **k: calls.append(k) or adapter)
    path = tmp_path / "state.db"
    argv = ["--database", str(path), "collect", "--transport", "official", "--source", "own",
            "--account", "operator", "--posts", "1", "--workflow", "listen", "--refresh-token"]
    result = cli.execute(cli.parser().parse_args(argv))
    assert result["status"] == "collection_complete"
    assert calls == [{"refresh_token": True}] and adapter.closed
    import os
    assert os.environ["THREADS_ACCESS_TOKEN"] == "original-fixture-token"
    state = State(path, "threads")
    try:
        doc = state.load(result["run_id"])
        assert doc["scope"]["transport"] == "official"
        serialized = json.dumps(doc)
        assert "refresh_token" not in serialized and "original-fixture-token" not in serialized
        scope_hash = doc["scope_hash"]
    finally:
        state.close()
    # A completed resume performs no refresh or HTTP and preserves the same scope.
    resumed = cli.execute(cli.parser().parse_args(["--database", str(path), "resume", "--run-id", result["run_id"], "--refresh-token"]))
    assert resumed["status"] == "collection_complete" and len(calls) == 1
    state = State(path, "threads")
    try:
        assert state.load(result["run_id"])["scope_hash"] == scope_hash
    finally:
        state.close()


def test_refresh_identity_report_contains_only_sanitized_lifecycle(monkeypatch):
    from social_engage import threads_official
    adapter = Adapter()
    monkeypatch.setattr(threads_official, "build_adapter", lambda **k: adapter)
    result = cli.execute(cli.parser().parse_args(["check-access", "--transport", "official", "--account", "operator", "--refresh-token"]))
    assert result["identity_check_only"]
    assert result["token_lifecycle"] == adapter.token_lifecycle and adapter.closed


def test_blocked_diagnostic_has_nonzero_exit_without_raw_error(monkeypatch, capsys):
    from social_engage import threads_official
    monkeypatch.setattr(threads_official, "official_check", lambda *a, **k: {"status": "blocked", "reason": "missing_required_scopes"})
    assert cli.main(["official-check", "--account", "operator"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "missing_required_scopes"
