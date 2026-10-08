"""No real tokens or Windows vault entries are used by this test module."""
import ctypes

import pytest

from social_engage import threads_credentials as vault


class MemoryBackend:
    def __init__(self):
        self.values = {}
        self.calls = []

    def write(self, target, account, blob):
        self.calls.append(("write", target))
        self.values[target] = (account, blob)

    def read(self, target):
        self.calls.append(("read", target))
        if target not in self.values:
            raise vault.CredentialError("credential_not_found")
        return self.values[target]

    def delete(self, target):
        self.calls.append(("delete", target))
        return self.values.pop(target, None) is not None


def test_exact_account_app_binding_and_normalization():
    backend = MemoryBackend()
    vault.save_token("123", "@My.Account", "synthetic-token", backend=backend)
    assert vault.load_token("123", "my.account", backend=backend) == "synthetic-token"
    for app, account in [("124", "my.account"), ("123", "another")]:
        with pytest.raises(vault.CredentialError, match="^credential_not_found$"):
            vault.load_token(app, account, backend=backend)
    assert vault.delete_token("124", "my.account", backend=backend) is False
    assert vault.delete_token("123", "my.account", backend=backend) is True
    assert backend.calls[0] == ("write", "KC-Lab:Threads:123:my.account")


@pytest.mark.parametrize("app,account", [("", "valid"), ("123/path", "valid"),
    ("123", "bad account"), ("123", "account:other"), (123, "valid")])
@pytest.mark.parametrize("operation", ["save", "load", "delete"])
def test_invalid_binding_never_opens_vault(monkeypatch, app, account, operation):
    monkeypatch.setattr(vault, "WindowsCredentialBackend", lambda: pytest.fail("vault opened"))
    with pytest.raises(vault.CredentialError, match="^invalid_credential_binding$"):
        if operation == "save":
            vault.save_token(app, account, "synthetic-token")
        else:
            getattr(vault, operation + "_token")(app, account)


@pytest.mark.parametrize("token", [None, "", " leading", "two words", "tab\there", "line\nbreak",
    "null\x00", "delete\x7f", "x" * 1281, "\ud800"])
def test_invalid_token_never_writes_or_echoes(monkeypatch, token):
    monkeypatch.setattr(vault, "WindowsCredentialBackend", lambda: pytest.fail("vault opened"))
    with pytest.raises(vault.CredentialError) as caught:
        vault.save_token("123", "account", token)
    assert str(caught.value) == "invalid_credential_token"


@pytest.mark.parametrize("account,blob", [("different", b"x\x00"), ("account", b"x"),
    ("account", b""), ("account", b"x" * 2562), ("account", "not-bytes"),
    ("account", "has space".encode("utf-16-le"))])
def test_corrupt_or_mismatched_vault_record_is_closed(account, blob):
    backend = MemoryBackend()
    backend.values[vault.credential_target("123", "account")] = account, blob
    with pytest.raises(vault.CredentialError, match="^invalid_stored_credential$"):
        vault.load_token("123", "account", backend=backend)


def test_unsupported_system_has_no_plaintext_fallback(monkeypatch):
    monkeypatch.setattr(vault.os, "name", "posix")
    with pytest.raises(vault.CredentialError, match="^windows_credential_manager_required$"):
        vault.WindowsCredentialBackend()


class FakeCall:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def test_native_binding_wipes_temporary_blob_and_frees_read_buffer(monkeypatch):
    target, token = "KC-Lab:Threads:123:account", "synthetic-native-token"
    stored_blob = (ctypes.c_ubyte * len(token.encode("utf-16-le"))).from_buffer_copy(token.encode("utf-16-le"))
    native_record = vault._Credential(Type=1, TargetName=target, UserName="account",
        CredentialBlobSize=len(stored_blob), CredentialBlob=stored_blob)
    writes, freed, deletes = [], [], []

    def write(pointer, flags):
        record = ctypes.cast(pointer, ctypes.POINTER(vault._Credential)).contents
        assert record.Type == 1 and record.Persist == 2 and flags == 0
        assert record.TargetName == target and record.UserName == "account"
        assert ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize) == token.encode("utf-16-le")
        writes.append(record.CredentialBlob)
        return True

    def read(name, kind, flags, output):
        assert (name, kind, flags) == (target, 1, 0)
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(vault._Credential)))[0] = ctypes.pointer(native_record)
        return True

    def free(pointer):
        assert bytes(stored_blob) == bytes(len(stored_blob))
        freed.append(True)

    class API:
        CredWriteW = FakeCall(write)
        CredReadW = FakeCall(read)
        CredDeleteW = FakeCall(lambda *args: deletes.append(args) or True)
        CredFree = FakeCall(free)

    monkeypatch.setattr(vault.os, "name", "nt")
    monkeypatch.setattr(vault.ctypes, "WinDLL", lambda *a, **k: API(), raising=False)
    backend = vault.WindowsCredentialBackend()
    backend.write(target, "account", token.encode("utf-16-le"))
    assert backend.read(target) == ("account", token.encode("utf-16-le"))
    assert freed == [True]
    assert backend.delete(target) is True
    assert deletes == [(target, 1, 0)]


def test_native_failures_do_not_include_os_error_text(monkeypatch):
    class API:
        CredWriteW = FakeCall(lambda *a: False)
        CredReadW = FakeCall(lambda *a: False)
        CredDeleteW = FakeCall(lambda *a: False)
        CredFree = FakeCall(lambda *a: pytest.fail("must not free failed read"))

    monkeypatch.setattr(vault.os, "name", "nt")
    monkeypatch.setattr(vault.ctypes, "WinDLL", lambda *a, **k: API(), raising=False)
    monkeypatch.setattr(vault.ctypes, "get_last_error", lambda: 1168, raising=False)
    backend = vault.WindowsCredentialBackend()
    with pytest.raises(vault.CredentialError, match="^credential_write_failed$"):
        backend.write("target", "account", b"x\x00")
    with pytest.raises(vault.CredentialError, match="^credential_not_found$"):
        backend.read("target")
    assert backend.delete("target") is False
