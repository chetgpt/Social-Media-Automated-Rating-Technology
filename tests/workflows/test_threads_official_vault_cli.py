"""Credential loading cannot change accounts, platform, or saved transports."""
import json
import os

import pytest

import threads_official_run as launcher
from social_engage.threads_credentials import CredentialError


PREFIX = ["--app-id", "123", "--account", "@Account", "--"]


@pytest.fixture(autouse=True)
def no_real_access(monkeypatch):
    monkeypatch.setattr(launcher, "load_token", lambda *a: pytest.fail("unexpected vault access"))
    monkeypatch.setattr(launcher.cli, "execute", lambda *a: pytest.fail("unexpected workflow access"))


@pytest.mark.parametrize("command", [
    ["official-check"], ["check-access"],
    ["collect", "--source", "own", "--posts", "1", "--workflow", "listen"],
])
def test_injects_verified_binding_and_restores_environment(monkeypatch, capsys, command):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "previous-user-token")
    monkeypatch.setenv("THREADS_APP_ACCESS_TOKEN", "unrelated-app-token")
    calls = []
    monkeypatch.setattr(launcher, "load_token", lambda *a: calls.append(a) or "synthetic-vault-token")

    def execute(args):
        assert args.platform == "threads" and args.account == "account"
        if args.command != "official-check":
            assert args.transport == "official"
        assert os.environ["THREADS_ACCESS_TOKEN"] == "synthetic-vault-token"
        assert "THREADS_APP_ACCESS_TOKEN" not in os.environ
        return {"status": "identity_checked"}

    monkeypatch.setattr(launcher.cli, "execute", execute)
    assert launcher.main(PREFIX + command) == 0
    assert calls == [("123", "account")]
    assert os.environ["THREADS_ACCESS_TOKEN"] == "previous-user-token"
    assert os.environ["THREADS_APP_ACCESS_TOKEN"] == "unrelated-app-token"
    assert "token" not in capsys.readouterr().out


@pytest.mark.parametrize("arguments,reason", [
    (["check-access", "--account", "other"], "credential_account_mismatch"),
    (["check-access", "--acc", "other"], "credential_account_mismatch"),
    (["check-access", "--transport", "browser"], "vault_launcher_requires_official_transport"),
    (["check-access", "--transp", "browser"], "vault_launcher_requires_official_transport"),
    (["check-access", "--browser-runtime-dir", "somewhere"], "vault_launcher_rejects_browser_runtime"),
    (["--platform", "linkedin", "check-access"], "unsupported_vault_launcher_command"),
    (["resume", "--run-id", "saved"], "unsupported_vault_launcher_command"),
    (["refresh", "--run-id", "saved", "--post-id", "123"], "unsupported_vault_launcher_command"),
    (["publish", "--run-id", "saved", "--post-id", "123"], "unsupported_vault_launcher_command"),
    (["status", "--run-id", "saved"], "unsupported_vault_launcher_command"),
])
def test_incompatible_routes_fail_before_vault_or_database(capsys, arguments, reason):
    assert launcher.main(PREFIX + arguments) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "blocked", "reason": reason}


def test_invalid_collect_fails_before_vault_or_database(capsys, tmp_path):
    database = tmp_path / "must-not-exist.db"
    assert launcher.main(PREFIX + ["--database", str(database), "collect", "--source", "creator", "--posts", "1"]) == 2
    assert not database.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


def test_existing_account_is_normalized_without_scope_replacement():
    binding, args = launcher.prepare(PREFIX + ["official-check", "--account", "@ACCOUNT", "--source", "topic",
                                              "--target", "exact topic", "--probe"])
    assert binding.account == args.account == "account"
    assert args.target == "exact topic" and args.probe


def test_clears_new_environment_on_failure_and_does_not_echo(monkeypatch, capsys):
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("THREADS_APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(launcher, "load_token", lambda *a: "synthetic-vault-token")

    def fail(args):
        raise RuntimeError("synthetic-vault-token")

    monkeypatch.setattr(launcher.cli, "execute", fail)
    assert launcher.main(PREFIX + ["official-check"]) == 2
    assert "THREADS_ACCESS_TOKEN" not in os.environ and "THREADS_APP_ACCESS_TOKEN" not in os.environ
    assert json.loads(capsys.readouterr().out) == {"status": "blocked", "reason": "vault_launcher_failed"}


def test_missing_vault_entry_never_runs_workflow(monkeypatch, capsys):
    def missing(*args):
        raise CredentialError("credential_not_found")

    monkeypatch.setattr(launcher, "load_token", missing)
    assert launcher.main(PREFIX + ["check-access"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "credential_not_found"
