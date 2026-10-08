"""Synthetic checks for durable transport selection and live-resource cleanup."""
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import engage_social as cli
from social_engage.adapters import AdapterError
from social_engage.state import State, StateError


class FakeAdapter:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.closed = 0
        self.calls = []

    def identity(self, expected=None):
        self.calls.append(("identity", expected))
        if self.failure == "identity":
            raise AdapterError("account_mismatch")
        return {"id": "88", "username": expected or "operator"}

    def discover(self, scope):
        self.calls.append(("discover", scope))
        return ["123"]

    def collect(self, post_id, scope, actor):
        self.calls.append(("collect", post_id))
        if self.failure == "collect":
            raise AdapterError("permission_denied")
        return {"id": post_id, "platform": scope["platform"], "text": "Synthetic post", "comments": []}

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def no_real_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real network or browser adapter is forbidden")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr(cli, "ThreadsAdapter", forbidden)
    monkeypatch.setattr(cli, "LinkedInAdapter", forbidden)
    monkeypatch.setitem(sys.modules, "social_engage.threads_browser", SimpleNamespace(ThreadsBrowserAdapter=forbidden))
    monkeypatch.setitem(sys.modules, "social_engage.linkedin_browser", SimpleNamespace(LinkedInBrowserAdapter=forbidden))


def collect_args(database, *extra):
    return cli.parser().parse_args(["--database", str(database), "collect", "--source", "own",
                                   "--account", "operator", "--posts", "1", *extra])


def test_browser_exact_url_freezes_post_id_and_owner():
    scope = cli.scope_from_args(cli.parser().parse_args(["collect", "--source", "post", "--target",
        "https://www.threads.net/@Creator/post/B7?discard=tracking", "--account", "operator", "--posts", "1"]))
    assert scope["target"] == "123" and scope["target_owner"] == "creator"
    assert scope["transport"] == "browser"


def browser_factory(monkeypatch, adapter, opened):
    def create(runtime_dir=None):
        opened.append(runtime_dir)
        return adapter
    monkeypatch.setitem(sys.modules, "social_engage.threads_browser", SimpleNamespace(ThreadsBrowserAdapter=create))


def test_new_threads_run_freezes_transport_and_absolute_runtime_across_resume(tmp_path, monkeypatch):
    database = tmp_path / "run.sqlite3"
    monkeypatch.chdir(tmp_path)
    adapter, opened = FakeAdapter(failure="collect"), []
    browser_factory(monkeypatch, adapter, opened)
    result = cli.execute(collect_args(database, "--browser-runtime-dir", "browser"))
    assert result["status"] == "collection_incomplete"
    with_database = State(database, "threads")
    original = with_database.load(result["run_id"])
    with_database.close()
    assert original["scope"]["transport"] == "browser"
    assert original["scope"]["browser_runtime_dir"] == str((tmp_path / "browser").resolve())
    assert original["scope"]["api_version"] is None
    adapter.failure = None
    adapter.calls.clear()
    next_dir = tmp_path / "different"
    next_dir.mkdir()
    monkeypatch.chdir(next_dir)
    resumed = cli.execute(cli.parser().parse_args(["--database", str(database), "resume", "--run-id", result["run_id"]]))
    assert resumed["status"] == "collection_complete"
    assert [call[0] for call in adapter.calls] == ["identity", "collect"]
    assert opened == [str(tmp_path / "browser")] * 2
    assert adapter.closed == 2
    with_database = State(database, "threads")
    final = with_database.load(result["run_id"])
    with_database.close()
    assert final["scope"] == original["scope"]
    assert final["scope_hash"] == original["scope_hash"]


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_official_and_legacy_runs_keep_official_adapter(tmp_path, monkeypatch, legacy):
    scope = cli.scope_from_args(collect_args(tmp_path / "run.db", "--transport", "official"))
    assert scope["transport"] == "official" and scope["api_version"] == "v1.0"
    if legacy:
        scope.pop("transport")
    adapter = FakeAdapter()
    tokens = []
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.setattr(cli, "ThreadsAdapter", lambda token: tokens.append(token) or adapter)
    database = State(tmp_path / "run.db", "threads")
    run_id = database.create(scope)
    before = database.load(run_id)["scope_hash"]
    database.close()
    result = cli.execute(cli.parser().parse_args(["--database", str(tmp_path / "run.db"), "resume", "--run-id", run_id]))
    assert result["status"] == "collection_complete"
    assert tokens == ["synthetic-token"] and adapter.closed == 1
    database = State(tmp_path / "run.db", "threads")
    assert database.load(run_id)["scope_hash"] == before
    database.close()


def test_linkedin_explicit_official_scope_stays_organization_bound(tmp_path):
    args = cli.parser().parse_args(["--platform", "linkedin", "collect", "--source", "organization",
                                   "--account", "urn:li:organization:123", "--posts", "1", "--transport", "official"])
    scope = cli.scope_from_args(args)
    assert scope["transport"] == "official" and scope["api_version"] == "202608"


def test_linkedin_browser_organization_rejected_before_database_or_adapter(monkeypatch):
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("database opened"))
    argv = ["--platform", "linkedin", "collect", "--transport", "browser", "--account", "urn:li:organization:123",
            "--source", "organization", "--posts", "1"]
    with pytest.raises(StateError, match="unsupported_source_for_platform"):
        cli.execute(cli.parser().parse_args(argv))


@pytest.mark.parametrize("argv, error", [
    (["check-access", "--transport", "official"], "exact_account_required"),
    (["--platform", "linkedin", "check-access", "--transport", "official"], "exact_account_required"),
    (["check-access", "--transport", "official", "--account", "operator", "--browser-runtime-dir", "."], "browser_runtime_dir_requires"),
    (["check-access", "--api-version", "v1.0"], "browser_transport_has_no_official_api_version"),
])
def test_invalid_access_check_fails_without_state_or_live_access(monkeypatch, argv, error):
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("database opened"))
    with pytest.raises(StateError, match=error):
        cli.execute(cli.parser().parse_args(argv))


def test_access_check_auto_resolves_and_projects_identity_without_database(tmp_path, monkeypatch):
    database = tmp_path / "must-not-exist.db"
    adapter, opened = FakeAdapter(), []
    def identity(expected=None):
        adapter.calls.append(("identity", expected))
        return {"id": "88", "username": "operator", "unexpected_private_field": "omit this"}
    adapter.identity = identity
    browser_factory(monkeypatch, adapter, opened)
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("database opened"))
    monkeypatch.setattr(cli, "operator_lock", lambda *args: pytest.fail("database lock opened"))
    result = cli.execute(cli.parser().parse_args(["--database", str(database), "check-access"]))
    assert result == {"status": "identity_checked", "identity_check_only": True, "platform": "threads",
                      "transport": "browser", "actor": {"id": "88", "username": "operator"}}
    assert adapter.calls == [("identity", None)] and adapter.closed == 1
    assert opened == [None] and not database.exists()


def test_access_check_failure_closes_adapter_without_creating_database(tmp_path, monkeypatch):
    adapter, opened = FakeAdapter(failure="identity"), []
    browser_factory(monkeypatch, adapter, opened)
    database = tmp_path / "must-not-exist.db"
    with pytest.raises(AdapterError, match="account_mismatch"):
        cli.execute(cli.parser().parse_args(["--database", str(database), "check-access", "--account", "other"]))
    assert adapter.calls == [("identity", "other")] and adapter.closed == 1
    assert not database.exists()


@pytest.mark.parametrize("platform, account, version", [
    ("threads", "@Operator", "v1.0"), ("linkedin", "urn:li:organization:123", "202608"),
])
def test_official_access_check_keeps_exact_account_binding(monkeypatch, platform, account, version):
    adapter = FakeAdapter()
    opened = []
    monkeypatch.setattr(cli, "make_adapter", lambda scope: opened.append(scope) or adapter)
    result = cli.execute(cli.parser().parse_args(["--platform", platform, "check-access", "--transport", "official",
                                                "--account", account, "--api-version", version]))
    assert opened[0]["transport"] == "official" and opened[0]["api_version"] == version
    expected = "operator" if platform == "threads" else account
    assert adapter.calls == [("identity", expected)] and adapter.closed == 1
    assert result["actor"]["username"] == expected


def test_capabilities_is_offline_and_honest_about_transport(monkeypatch):
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("database opened"))
    monkeypatch.setattr(cli, "make_adapter", lambda *args: pytest.fail("adapter opened"))
    result = cli.execute(cli.parser().parse_args(["capabilities"]))
    assert result["default_transport"] == "browser"
    assert result["transports"] == ["browser", "official"]
    assert result["live_validation"] == "not_claimed_by_capabilities"
    assert cli.capabilities("linkedin")["default_transport"] == "browser"
    assert cli.capabilities("linkedin")["sources_by_transport"]["official"] == ["organization", "post"]


def test_linkedin_browser_identity_without_oauth_or_database(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setitem(sys.modules, "social_engage.linkedin_browser", SimpleNamespace(LinkedInBrowserAdapter=lambda runtime_dir=None: adapter))
    monkeypatch.setattr(cli, "State", lambda *args: pytest.fail("database opened"))
    monkeypatch.setattr(cli, "operator_lock", lambda *args: pytest.fail("lock opened"))
    result = cli.execute(cli.parser().parse_args(["--platform", "linkedin", "check-access"]))
    assert result["platform"] == "linkedin" and result["transport"] == "browser"
    assert adapter.calls == [("identity", None)] and adapter.closed == 1


def test_linkedin_browser_scope_and_resume_keep_actor_inventory_transport(tmp_path, monkeypatch):
    adapter = FakeAdapter(failure="collect")
    adapter.discover = lambda scope: ["urn:li:activity:123"]
    monkeypatch.setitem(sys.modules, "social_engage.linkedin_browser", SimpleNamespace(LinkedInBrowserAdapter=lambda runtime_dir=None: adapter))
    database = tmp_path / "linkedin.db"
    args = cli.parser().parse_args(["--platform", "linkedin", "--database", str(database), "collect", "--source", "creator",
                                  "--target", "https://www.linkedin.com/in/Creator-Test/", "--account", "Operator", "--posts", "1"])
    first = cli.execute(args)
    assert first["status"] == "collection_incomplete"
    state = State(database, "linkedin")
    original = state.load(first["run_id"])
    assert original["scope"]["transport"] == "browser" and original["scope"]["api_version"] is None
    assert original["scope"]["target"] == "creator-test" and original["scope"]["account"] == "operator"
    state.close()
    adapter.failure = None
    adapter.calls.clear()
    adapter.discover = lambda scope: pytest.fail("resume rediscovered")
    final = cli.execute(cli.parser().parse_args(["--platform", "linkedin", "--database", str(database), "resume", "--run-id", first["run_id"]]))
    assert final["status"] == "collection_complete"
    assert [c[0] for c in adapter.calls] == ["identity", "collect"]
    state = State(database, "linkedin")
    doc = state.load(first["run_id"])
    assert doc["scope"] == original["scope"] and doc["inventory"] == original["inventory"]
    state.close()


def test_legacy_linkedin_resume_remains_official(tmp_path, monkeypatch):
    args = cli.parser().parse_args(["--platform", "linkedin", "collect", "--transport", "official", "--source", "organization",
                                  "--account", "urn:li:organization:123", "--posts", "1"])
    scope = cli.scope_from_args(args)
    scope.pop("transport")
    database = tmp_path / "legacy.db"
    state = State(database,"linkedin")
    run_id = state.create(scope)
    state.close()
    adapter = FakeAdapter()
    monkeypatch.setattr(cli,"LinkedInAdapter",lambda *a,**kw: adapter)
    result = cli.execute(cli.parser().parse_args(["--platform","linkedin","--database",str(database),"resume","--run-id",run_id]))
    assert result["status"] == "collection_complete"


def test_complete_resume_and_status_do_not_instantiate_browser(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    browser_factory(monkeypatch, adapter, [])
    database = tmp_path / "run.db"
    result = cli.execute(collect_args(database))
    monkeypatch.setattr(cli, "make_adapter", lambda *args: pytest.fail("offline command opened adapter"))
    for command in ("resume", "status"):
        offline = cli.execute(cli.parser().parse_args(["--database", str(database), command, "--run-id", result["run_id"]]))
        assert offline["status"] == "collection_complete"
    assert adapter.closed == 1


@pytest.mark.parametrize("command", ["collect", "resume", "refresh", "publish"])
@pytest.mark.parametrize("failure", [False, True])
def test_every_live_command_closes_adapter_on_success_or_failure(tmp_path, monkeypatch, command, failure):
    scope = cli.scope_from_args(collect_args(tmp_path / "run.db"))
    document = {"scope": scope, "actor": {"id": "88", "username": "operator"},
                "collection_verified": False, "inventory": ["123"], "posts": {"123": {}}}
    adapter = FakeAdapter(failure="collect" if failure and command == "refresh" else None)
    closed = []
    state = SimpleNamespace(
        create=lambda scope: "run", load=lambda run: document, ready=lambda *args: (document, {}),
        status=lambda run: {"status": "checked"}, refresh=lambda *args: None,
        close=lambda: closed.append(True), db=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None)))
    monkeypatch.setattr(cli, "State", lambda *args: state)
    monkeypatch.setattr(cli, "operator_lock", lambda *args: nullcontext())
    monkeypatch.setattr(cli, "make_adapter", lambda *args: adapter)
    def operation(*args):
        if failure:
            raise AdapterError("synthetic_live_failure")
        return {"status": "checked"}
    monkeypatch.setattr(cli, "collect", operation)
    monkeypatch.setattr(cli, "publish", operation)
    args = collect_args(tmp_path / "run.db") if command == "collect" else cli.parser().parse_args(
        [command, "--run-id", "run", *(["--post-id", "123"] if command in {"refresh", "publish"} else [])])
    if failure:
        with pytest.raises(AdapterError):
            cli.execute(args)
    else:
        assert cli.execute(args)["status"] == "checked"
    assert adapter.closed == 1 and closed == [True]


def test_official_http_session_is_closed_without_adapter_close_method(monkeypatch):
    closed = []
    adapter = SimpleNamespace(http=SimpleNamespace(session=SimpleNamespace(close=lambda: closed.append(True))))
    monkeypatch.setattr(cli, "make_adapter", lambda *args: adapter)
    with pytest.raises(AdapterError):
        with cli.live_adapter({"platform": "threads", "transport": "official"}):
            raise AdapterError("failed")
    assert closed == [True]
