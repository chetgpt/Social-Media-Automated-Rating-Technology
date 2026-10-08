"""Load an exact Windows-vault token for a new official Threads operation.

Usage: threads_official_run.py --app-id ID --account HANDLE -- COMMAND [OPTIONS]
Supported commands: official-check, check-access, collect. Optional --database
PATH goes before COMMAND, just as in threads_workflow.py. This launcher never
resumes, refreshes or publishes a saved run; use the canonical runner for those
operations, preserving its stored transport. Refresh-token stays process-local.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys

import engage_social as cli
from social_engage.adapters import AdapterError, username
from social_engage.threads_credentials import CredentialError, credential_target, load_token


SUPPORTED_COMMANDS = frozenset({"official-check", "check-access", "collect"})


def prepare(argv):
    """Validate effective CLI scope before reading any credential or database."""
    header = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    header.add_argument("--app-id", required=True)
    header.add_argument("--account", required=True)
    if "--" not in argv:
        header.parse_args(argv)  # Preserve the ordinary --help behavior.
        raise CredentialError("workflow_separator_required")
    boundary = argv.index("--")
    binding = header.parse_args(argv[:boundary])
    credential_target(binding.app_id, binding.account)
    binding.account = username(binding.account)
    workflow = cli.parser()
    # Reuse canonical validation. Account can come from this launcher's binding;
    # an explicit workflow account is checked below, never silently overwritten.
    for action in workflow._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name in SUPPORTED_COMMANDS:
                for option in action.choices[name]._actions:
                    if option.dest == "account":
                        option.required = False
    args = workflow.parse_args(argv[boundary + 1:])
    if args.platform != "threads" or args.command not in SUPPORTED_COMMANDS:
        raise CredentialError("unsupported_vault_launcher_command")
    if args.account is not None and username(args.account) != binding.account:
        raise CredentialError("credential_account_mismatch")
    args.account = binding.account
    if args.command in {"collect", "check-access"}:
        if args.transport not in {None, "official"}:
            raise CredentialError("vault_launcher_requires_official_transport")
        args.transport = "official"
        if args.browser_runtime_dir is not None:
            raise CredentialError("vault_launcher_rejects_browser_runtime")
    if args.command == "collect":
        cli.scope_from_args(args)
    return binding, args


@contextmanager
def token_environment(token):
    """Inject only for this invocation and restore prior env on every exit.

    An unrelated inherited app-debugger credential is excluded because this
    launcher stores only user tokens. Use the canonical runner for debugging
    with an explicitly supplied app access token. This is a single-process CLI,
    not a thread-safe service credential context.
    """
    names = ("THREADS_ACCESS_TOKEN", "THREADS_APP_ACCESS_TOKEN")
    previous = {name: os.environ.get(name) for name in names}
    try:
        os.environ["THREADS_ACCESS_TOKEN"] = token
        os.environ.pop("THREADS_APP_ACCESS_TOKEN", None)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main(argv=None):
    try:
        binding, args = prepare(list(sys.argv[1:] if argv is None else argv))
        token = load_token(binding.app_id, binding.account)
        try:
            with token_environment(token):
                result = cli.execute(args)
        finally:
            token = None
    except (CredentialError, AdapterError, cli.StateError) as exc:
        result = {"status": "blocked", "reason": str(exc)}
    except Exception:
        result = {"status": "blocked", "reason": "vault_launcher_failed"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if result.get("status") in {"blocked", "collection_incomplete", "evidence_expired"} else 0


if __name__ == "__main__":
    sys.exit(main())
