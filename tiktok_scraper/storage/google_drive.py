"""Google Drive API archival with resumable, verified uploads.

The scraper keeps its live SQLite database and active worker directories on the
local disk. Completed artifacts are copied to an app-owned Drive hierarchy and
may be removed locally only after Drive reports the expected size and MD5.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import time
from typing import Any, Callable, Mapping

import requests


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
MIN_CHUNK_SIZE = 256 * 1024


class DriveArchiveError(RuntimeError):
    """Raised when Drive archival cannot safely complete."""


def _now() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool_text(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _path(value: Any, *, base: Path | None = None) -> Path | None:
    text = _text(value)
    if not text:
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    if base is not None and not expanded.is_absolute():
        expanded = base / expanded
    return expanded


def default_token_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "KitaCoLab" / "SocialListening" / "google_drive_token.json"


def _first(*values: Any, default: Any = "") -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return default


class DriveArchiveConfig:
    """Resolved machine and campaign settings for Drive archival."""

    def __init__(
        self,
        *,
        provider: str = "none",
        credentials_file: Path | None = None,
        token_file: Path | None = None,
        root_folder_name: str = "Social Listening Projects",
        root_folder_id: str = "",
        account_hint: str = "",
        mount_path: Path | None = None,
        archive_raw_runs: bool = True,
        archive_backlog: bool = True,
        raw_run_archive_timing: str = "immediate",
        upload_compiled: bool = True,
        upload_logs: bool = True,
        snapshot_database: bool = True,
        delete_local_after_upload: bool = False,
        required: bool = True,
        chunk_size_mb: int = 16,
        max_retries: int = 7,
    ) -> None:
        normalized_provider = provider.strip().lower().replace("_", "-") or "none"
        if normalized_provider not in {"none", "google-drive"}:
            raise DriveArchiveError("archive provider must be none or google-drive")
        normalized_timing = raw_run_archive_timing.strip().lower().replace("_", "-") or "immediate"
        if normalized_timing not in {"immediate", "final"}:
            raise DriveArchiveError("raw run archive timing must be immediate or final")
        if chunk_size_mb < 1 or chunk_size_mb > 256:
            raise DriveArchiveError("Drive chunk size must be between 1 and 256 MiB")
        self.provider = normalized_provider
        self.credentials_file = credentials_file
        self.token_file = token_file or default_token_file()
        self.root_folder_name = root_folder_name.strip() or "Social Listening Projects"
        self.root_folder_id = root_folder_id.strip()
        self.account_hint = account_hint.strip()
        self.mount_path = mount_path
        self.archive_raw_runs = archive_raw_runs
        self.archive_backlog = archive_backlog
        self.raw_run_archive_timing = normalized_timing
        self.upload_compiled = upload_compiled
        self.upload_logs = upload_logs
        self.snapshot_database = snapshot_database
        self.delete_local_after_upload = delete_local_after_upload
        self.required = required
        self.chunk_size_bytes = chunk_size_mb * 1024 * 1024
        self.max_retries = max(1, max_retries)

    @property
    def enabled(self) -> bool:
        return self.provider == "google-drive"

    @classmethod
    def from_sources(
        cls,
        args: Mapping[str, Any],
        storage: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> "DriveArchiveConfig":
        storage = storage or {}
        env = environ or os.environ
        base = cwd or Path.cwd()

        def setting(arg_name: str, env_name: str, storage_name: str, default: Any = "") -> Any:
            return _first(args.get(arg_name), env.get(env_name), storage.get(storage_name), default=default)

        provider = setting("archive_provider", "SCRAPER_ARCHIVE_PROVIDER", "provider", "none")
        credentials = _path(
            setting(
                "drive_credentials",
                "GOOGLE_DRIVE_CLIENT_SECRET_FILE",
                "credentials_file",
                str(base / ".google_drive" / "client_secret.json"),
            ),
            base=base,
        )
        token = _path(
            setting("drive_token", "GOOGLE_DRIVE_TOKEN_FILE", "token_file", str(default_token_file())),
            base=base,
        )
        mount = _path(
            setting("drive_mount_path", "GOOGLE_DRIVE_MOUNT_PATH", "mount_path", ""),
            base=base,
        )

        bool_settings = {
            "archive_raw_runs": ("drive_archive_raw_runs", "GOOGLE_DRIVE_ARCHIVE_RAW_RUNS", True),
            "archive_backlog": ("drive_archive_backlog", "GOOGLE_DRIVE_ARCHIVE_BACKLOG", True),
            "upload_compiled": ("drive_upload_compiled", "GOOGLE_DRIVE_UPLOAD_COMPILED", True),
            "upload_logs": ("drive_upload_logs", "GOOGLE_DRIVE_UPLOAD_LOGS", True),
            "snapshot_database": ("drive_snapshot_database", "GOOGLE_DRIVE_SNAPSHOT_DATABASE", True),
            "delete_local_after_upload": (
                "drive_delete_local_after_upload",
                "GOOGLE_DRIVE_DELETE_LOCAL_AFTER_UPLOAD",
                False,
            ),
            "required": ("drive_required", "GOOGLE_DRIVE_REQUIRED", True),
        }
        resolved_bools: dict[str, bool] = {}
        for storage_name, (arg_name, env_name, default) in bool_settings.items():
            resolved_bools[storage_name] = _bool_text(
                _first(args.get(arg_name), env.get(env_name), storage.get(storage_name), default=default),
                default,
            )

        return cls(
            provider=_text(provider),
            credentials_file=credentials,
            token_file=token,
            root_folder_name=_text(
                setting(
                    "drive_root_folder_name",
                    "GOOGLE_DRIVE_ROOT_FOLDER_NAME",
                    "root_folder_name",
                    "Social Listening Projects",
                )
            ),
            root_folder_id=_text(
                setting("drive_root_folder_id", "GOOGLE_DRIVE_ROOT_FOLDER_ID", "root_folder_id", "")
            ),
            account_hint=_text(
                setting("drive_account_hint", "GOOGLE_DRIVE_ACCOUNT_HINT", "account_hint", "")
            ),
            mount_path=mount,
            chunk_size_mb=int(
                setting("drive_chunk_size_mb", "GOOGLE_DRIVE_CHUNK_SIZE_MB", "chunk_size_mb", 16)
            ),
            max_retries=int(setting("drive_max_retries", "GOOGLE_DRIVE_MAX_RETRIES", "max_retries", 7)),
            raw_run_archive_timing=_text(
                setting(
                    "drive_raw_run_archive_timing",
                    "GOOGLE_DRIVE_RAW_RUN_ARCHIVE_TIMING",
                    "raw_run_archive_timing",
                    "immediate",
                )
            ),
            **resolved_bools,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "account_hint": self.account_hint,
            "mount_path": str(self.mount_path) if self.mount_path else "",
            "root_folder_name": self.root_folder_name,
            "root_folder_id": self.root_folder_id,
            "credentials_file": str(self.credentials_file) if self.credentials_file else "",
            "token_file": str(self.token_file),
            "archive_raw_runs": self.archive_raw_runs,
            "archive_backlog": self.archive_backlog,
            "raw_run_archive_timing": self.raw_run_archive_timing,
            "upload_compiled": self.upload_compiled,
            "upload_logs": self.upload_logs,
            "snapshot_database": self.snapshot_database,
            "delete_local_after_upload": self.delete_local_after_upload,
            "required": self.required,
            "chunk_size_mb": self.chunk_size_bytes // (1024 * 1024),
        }


def ensure_archive_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS archive_uploads (
            artifact_key TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            source_run_id TEXT DEFAULT '',
            logical_path TEXT NOT NULL,
            local_path TEXT NOT NULL,
            remote_name TEXT NOT NULL,
            remote_parent_id TEXT DEFAULT '',
            remote_file_id TEXT DEFAULT '',
            size_bytes INTEGER NOT NULL,
            md5 TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            remote_size_bytes INTEGER DEFAULT 0,
            remote_md5 TEXT DEFAULT '',
            status TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            session_uri TEXT DEFAULT '',
            uploaded_bytes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            verified_at TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_archive_uploads_project_status
            ON archive_uploads(project, status, artifact_type);
        CREATE INDEX IF NOT EXISTS idx_archive_uploads_source_run
            ON archive_uploads(project, source_run_id, status);
        """
    )
    conn.commit()


def file_fingerprint(path: Path) -> dict[str, Any]:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            md5.update(chunk)
            sha256.update(chunk)
    return {"size_bytes": size, "md5": md5.hexdigest(), "sha256": sha256.hexdigest()}


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _resume_offset(range_header: str, total_size: int) -> int:
    match = re.search(r"(?:bytes=)?(\d+)-(\d+)$", range_header.strip())
    if not match:
        return 0
    offset = int(match.group(2)) + 1
    if offset < 0 or offset > total_size:
        raise DriveArchiveError(f"Drive returned an invalid resumable offset: {offset}/{total_size}")
    return offset


class DriveRestClient:
    """Small Drive v3 client using OAuth and persistent resumable upload URLs."""

    def __init__(
        self,
        config: DriveArchiveConfig,
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.session = session
        self.sleep = sleep
        self.account: dict[str, Any] = {}

    def authenticate(self, *, interactive: bool = True) -> dict[str, Any]:
        if self.config.mount_path and not self.config.mount_path.exists():
            raise DriveArchiveError(
                f"Configured Google Drive mount is unavailable: {self.config.mount_path}"
            )
        if self.session is None:
            try:
                from google.auth.transport.requests import AuthorizedSession, Request
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ImportError as exc:
                raise DriveArchiveError(
                    "Google Drive dependencies are missing. Run: pip install -r requirements.txt"
                ) from exc

            def authorize() -> Any:
                if not interactive:
                    raise DriveArchiveError("Google Drive OAuth authorization is required")
                if not self.config.credentials_file or not self.config.credentials_file.exists():
                    raise DriveArchiveError(
                        "Google Drive OAuth client file is missing. Create a Desktop app OAuth client, "
                        f"then place its JSON at {self.config.credentials_file}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.config.credentials_file), [DRIVE_SCOPE]
                )
                return flow.run_local_server(
                    host="localhost",
                    port=0,
                    open_browser=True,
                    authorization_prompt_message=(
                        "Authorize the Google Drive account selected for scraper archives."
                    ),
                    success_message="Google Drive authorization completed. You can close this tab.",
                    prompt="select_account consent",
                )

            credentials = None
            loaded_token = False
            if self.config.token_file.exists():
                try:
                    credentials = Credentials.from_authorized_user_file(
                        str(self.config.token_file), [DRIVE_SCOPE]
                    )
                    loaded_token = True
                except (ValueError, OSError) as exc:
                    raise DriveArchiveError(
                        f"Cannot read Google Drive token: {self.config.token_file}"
                    ) from exc
            if credentials and credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                except Exception:
                    credentials = None
            if not credentials or not credentials.valid:
                credentials = authorize()
                loaded_token = False
            self.session = AuthorizedSession(credentials)
            account = self.get_about()
            try:
                self._verify_account(account)
            except DriveArchiveError:
                if not loaded_token or not interactive:
                    raise
                credentials = authorize()
                self.session = AuthorizedSession(credentials)
                account = self.get_about()
                self._verify_account(account)
            self.config.token_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config.token_file.with_name(
                f"{self.config.token_file.name}.{os.getpid()}.tmp"
            )
            temporary.write_text(credentials.to_json(), encoding="utf-8")
            os.replace(temporary, self.config.token_file)
            self.account = account
            return account

        account = self.get_about()
        self._verify_account(account)
        self.account = account
        return account

    def _verify_account(self, about: Mapping[str, Any]) -> None:
        hint = self.config.account_hint.casefold()
        if not hint:
            return
        user = about.get("user") if isinstance(about, Mapping) else {}
        user = user if isinstance(user, Mapping) else {}
        email = _text(user.get("emailAddress"))
        display_name = _text(user.get("displayName"))
        haystack = f"{email}\n{display_name}".casefold()
        matches = hint == email.casefold() if "@" in hint else hint in haystack
        if not matches:
            raise DriveArchiveError(
                "Authenticated Google Drive account does not match the configured account hint "
                f"({self.config.account_hint}). Received: {email or display_name or 'unknown account'}"
            )

    def _request(
        self,
        method: str,
        url: str,
        *,
        expected: set[int] | None = None,
        timeout: float = 120,
        **kwargs: Any,
    ) -> Any:
        if self.session is None:
            raise DriveArchiveError("Google Drive client is not authenticated")
        expected = expected or {200}
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 >= self.config.max_retries:
                    break
                self.sleep(self._retry_delay(attempt, None))
                continue
            if response.status_code in expected:
                return response
            if response.status_code not in RETRYABLE_HTTP_STATUSES or attempt + 1 >= self.config.max_retries:
                detail = _text(getattr(response, "text", ""))[:500]
                raise DriveArchiveError(
                    f"Drive API {method} {url} failed with HTTP {response.status_code}: {detail}"
                )
            self.sleep(self._retry_delay(attempt, response))
        raise DriveArchiveError(f"Drive API request failed after retries: {last_error}")

    @staticmethod
    def _retry_delay(attempt: int, response: Any | None) -> float:
        retry_after = _text(getattr(response, "headers", {}).get("Retry-After")) if response else ""
        if retry_after.isdigit():
            return min(120.0, float(retry_after))
        return min(120.0, (2 ** attempt) + random.random())

    def get_about(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{DRIVE_API}/about",
            params={"fields": "user(displayName,emailAddress),storageQuota"},
        )
        return response.json()

    def list_files(self, query: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = {
                "q": query,
                "spaces": "drive",
                "pageSize": 100,
                "orderBy": "createdTime",
                "fields": (
                    "nextPageToken,files(id,name,mimeType,parents,size,md5Checksum,"
                    "createdTime,trashed,appProperties)"
                ),
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._request("GET", f"{DRIVE_API}/files", params=params)
            payload = response.json()
            files.extend(payload.get("files") or [])
            page_token = _text(payload.get("nextPageToken"))
            if not page_token:
                return files

    def get_file(self, file_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={
                "fields": "id,name,mimeType,parents,size,md5Checksum,trashed,appProperties"
            },
        )
        return response.json()

    def ensure_folder(
        self,
        name: str,
        parent_id: str,
        *,
        app_properties: Mapping[str, str] | None = None,
    ) -> str:
        escaped_name = _drive_query_literal(name)
        escaped_parent = _drive_query_literal(parent_id)
        query = (
            f"name = '{escaped_name}' and '{escaped_parent}' in parents and "
            f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
        )
        matches = self.list_files(query)
        wanted = dict(app_properties or {})
        for item in matches:
            existing = item.get("appProperties") or {}
            if not wanted or all(existing.get(key) == value for key, value in wanted.items()):
                return _text(item.get("id"))
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
            "parents": [parent_id],
        }
        if wanted:
            metadata["appProperties"] = wanted
        response = self._request(
            "POST",
            f"{DRIVE_API}/files",
            expected={200, 201},
            params={"fields": "id,name,parents,appProperties"},
            json=metadata,
        )
        folder_id = _text(response.json().get("id"))
        if not folder_id:
            raise DriveArchiveError(f"Drive did not return an id for folder: {name}")
        return folder_id

    def ensure_project_folders(self, project_slug: str) -> dict[str, str]:
        if self.config.root_folder_id:
            root_id = self.config.root_folder_id
            root = self.get_file(root_id)
            if root.get("mimeType") != FOLDER_MIME_TYPE or root.get("trashed"):
                raise DriveArchiveError("Configured Drive root_folder_id is not an active folder")
        else:
            root_id = self.ensure_folder(
                self.config.root_folder_name,
                "root",
                app_properties={"slArchiveRoot": "v1"},
            )
        project_id = self.ensure_folder(
            project_slug,
            root_id,
            app_properties={"slProject": project_slug[:120]},
        )
        folders = {"root": root_id, "project": project_id}
        for name in ("raw_runs", "compiled", "logs", "state", "manifests"):
            folders[name] = self.ensure_folder(
                name,
                project_id,
                app_properties={"slProjectSection": name},
            )
        return folders

    def _find_remote_file(
        self,
        parent_id: str,
        remote_name: str,
        artifact_key: str,
    ) -> dict[str, Any] | None:
        query = (
            f"name = '{_drive_query_literal(remote_name)}' and "
            f"'{_drive_query_literal(parent_id)}' in parents and trashed = false and "
            f"mimeType != '{FOLDER_MIME_TYPE}'"
        )
        matches = self.list_files(query)
        for item in matches:
            if (item.get("appProperties") or {}).get("slArtifactKey") == artifact_key:
                return item
        return matches[0] if len(matches) == 1 else None

    def _start_resumable_upload(
        self,
        *,
        parent_id: str,
        remote_name: str,
        mime_type: str,
        size_bytes: int,
        app_properties: Mapping[str, str],
        remote_file_id: str,
    ) -> str:
        metadata: dict[str, Any] = {
            "name": remote_name,
            "appProperties": dict(app_properties),
        }
        if not remote_file_id:
            metadata["parents"] = [parent_id]
        if remote_file_id:
            method = "PATCH"
            url = f"{DRIVE_UPLOAD_API}/files/{remote_file_id}"
        else:
            method = "POST"
            url = f"{DRIVE_UPLOAD_API}/files"
        response = self._request(
            method,
            url,
            expected={200},
            params={
                "uploadType": "resumable",
                "fields": "id,name,size,md5Checksum,parents,appProperties",
            },
            headers={
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size_bytes),
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=metadata,
        )
        session_uri = _text(response.headers.get("Location"))
        if not session_uri:
            raise DriveArchiveError("Drive did not return a resumable upload URL")
        return session_uri

    def _query_resumable_upload(self, session_uri: str, total_size: int) -> tuple[int, dict[str, Any] | None]:
        response = self._request(
            "PUT",
            session_uri,
            expected={200, 201, 308, 404, 410},
            headers={"Content-Length": "0", "Content-Range": f"bytes */{total_size}"},
            data=b"",
        )
        if response.status_code in {404, 410}:
            return -1, None
        if response.status_code in {200, 201}:
            return total_size, response.json()
        return _resume_offset(_text(response.headers.get("Range")), total_size), None

    def _upload_zero_byte_file(
        self,
        *,
        parent_id: str,
        remote_name: str,
        mime_type: str,
        app_properties: Mapping[str, str],
        remote_file_id: str,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"name": remote_name, "appProperties": dict(app_properties)}
        if not remote_file_id:
            metadata["parents"] = [parent_id]
            response = self._request(
                "POST",
                f"{DRIVE_API}/files",
                expected={200, 201},
                params={"fields": "id,name,size,md5Checksum,parents,appProperties"},
                json=metadata,
            )
            return response.json()
        self._request(
            "PATCH",
            f"{DRIVE_API}/files/{remote_file_id}",
            params={"fields": "id"},
            json=metadata,
        )
        response = self._request(
            "PATCH",
            f"{DRIVE_UPLOAD_API}/files/{remote_file_id}",
            params={"uploadType": "media", "fields": "id,name,size,md5Checksum,parents,appProperties"},
            headers={"Content-Type": mime_type, "Content-Length": "0"},
            data=b"",
        )
        return response.json()

    def upload_file(
        self,
        local_path: Path,
        *,
        parent_id: str,
        remote_name: str,
        artifact_key: str,
        artifact_type: str,
        project_slug: str,
        fingerprint: Mapping[str, Any],
        remote_file_id: str = "",
        session_uri: str = "",
        uploaded_bytes: int = 0,
        progress: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        size_bytes = int(fingerprint["size_bytes"])
        expected_md5 = _text(fingerprint["md5"])
        app_properties = {
            "slArtifactKey": artifact_key,
            "slArtifactType": artifact_type[:120],
            "slProject": project_slug[:120],
        }
        remote = None
        if remote_file_id:
            try:
                remote = self.get_file(remote_file_id)
            except DriveArchiveError:
                remote_file_id = ""
                session_uri = ""
        if remote is None and not remote_file_id:
            remote = self._find_remote_file(parent_id, remote_name, artifact_key)
            remote_file_id = _text((remote or {}).get("id"))
        if remote and self._matches_fingerprint(remote, size_bytes, expected_md5):
            return remote

        mime_type = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
        if size_bytes == 0:
            result = self._upload_zero_byte_file(
                parent_id=parent_id,
                remote_name=remote_name,
                mime_type=mime_type,
                app_properties=app_properties,
                remote_file_id=remote_file_id,
            )
            return self._verify_uploaded_file(_text(result.get("id") or remote_file_id), size_bytes, expected_md5)

        offset = max(0, min(int(uploaded_bytes), size_bytes))
        if session_uri:
            offset, finished = self._query_resumable_upload(session_uri, size_bytes)
            if finished:
                file_id = _text(finished.get("id") or remote_file_id)
                return self._verify_uploaded_file(file_id, size_bytes, expected_md5)
            if offset < 0:
                session_uri = ""
                offset = 0
        if not session_uri:
            session_uri = self._start_resumable_upload(
                parent_id=parent_id,
                remote_name=remote_name,
                mime_type=mime_type,
                size_bytes=size_bytes,
                app_properties=app_properties,
                remote_file_id=remote_file_id,
            )
            offset = 0
            if progress:
                progress(session_uri, offset)

        final_payload: dict[str, Any] | None = None
        with local_path.open("rb") as handle:
            while offset < size_bytes:
                handle.seek(offset)
                chunk = handle.read(min(self.config.chunk_size_bytes, size_bytes - offset))
                if not chunk:
                    raise DriveArchiveError(f"Unexpected end of local file during upload: {local_path}")
                end = offset + len(chunk) - 1
                response = self._request(
                    "PUT",
                    session_uri,
                    expected={200, 201, 308, 404, 410},
                    timeout=300,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Type": mime_type,
                        "Content-Range": f"bytes {offset}-{end}/{size_bytes}",
                    },
                    data=chunk,
                )
                if response.status_code in {404, 410}:
                    session_uri = self._start_resumable_upload(
                        parent_id=parent_id,
                        remote_name=remote_name,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        app_properties=app_properties,
                        remote_file_id=remote_file_id,
                    )
                    offset = 0
                    if progress:
                        progress(session_uri, offset)
                    continue
                if response.status_code == 308:
                    offset = _resume_offset(_text(response.headers.get("Range")), size_bytes)
                    if progress:
                        progress(session_uri, offset)
                    continue
                final_payload = response.json()
                offset = size_bytes
                if progress:
                    progress(session_uri, offset)

        file_id = _text((final_payload or {}).get("id") or remote_file_id)
        return self._verify_uploaded_file(file_id, size_bytes, expected_md5)

    @staticmethod
    def _matches_fingerprint(remote: Mapping[str, Any], size_bytes: int, md5: str) -> bool:
        return (
            not remote.get("trashed")
            and int(remote.get("size") or -1) == size_bytes
            and _text(remote.get("md5Checksum")).casefold() == md5.casefold()
        )

    def _verify_uploaded_file(self, file_id: str, size_bytes: int, md5: str) -> dict[str, Any]:
        if not file_id:
            raise DriveArchiveError("Drive upload completed without a file id")
        remote = self.get_file(file_id)
        if not self._matches_fingerprint(remote, size_bytes, md5):
            raise DriveArchiveError(
                "Drive verification failed for "
                f"{remote.get('name') or file_id}: local size/md5={size_bytes}/{md5}, "
                f"remote size/md5={remote.get('size')}/{remote.get('md5Checksum')}"
            )
        return remote


class GoogleDriveArchiveManager:
    """Coordinates project artifacts, the Drive client, and the SQLite ledger."""

    def __init__(
        self,
        config: DriveArchiveConfig,
        conn: sqlite3.Connection,
        project: str,
        project_slug: str,
        paths: Mapping[str, Path],
        *,
        client: DriveRestClient | Any | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.project = project
        self.project_slug = project_slug
        self.paths = dict(paths)
        self.client = client or DriveRestClient(config)
        self.folders: dict[str, str] = {}
        self.folder_cache: dict[tuple[str, str], str] = {}
        self.account: dict[str, Any] = {}
        ensure_archive_schema(conn)

    def initialize(self, *, interactive: bool = True) -> dict[str, Any]:
        self.account = self.client.authenticate(interactive=interactive)
        self.folders = self.client.ensure_project_folders(self.project_slug)
        user = self.account.get("user") or {}
        return {
            **self.config.public_dict(),
            "account": {
                "display_name": _text(user.get("displayName")),
                "email": _text(user.get("emailAddress")),
            },
            "remote_project_folder_id": self.folders.get("project", ""),
        }

    def _artifact_key(self, logical_path: str) -> str:
        payload = f"{self.project}\x1f{logical_path}".encode("utf-8", "replace")
        return hashlib.sha256(payload).hexdigest()

    def _ledger_row(self, artifact_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM archive_uploads WHERE artifact_key = ?", (artifact_key,)
        ).fetchone()

    def _prepare_ledger(
        self,
        *,
        artifact_key: str,
        artifact_type: str,
        source_run_id: str,
        logical_path: str,
        local_path: Path,
        parent_id: str,
        remote_name: str,
        fingerprint: Mapping[str, Any],
        metadata: Mapping[str, Any] | None,
    ) -> sqlite3.Row:
        existing = self._ledger_row(artifact_key)
        same_content = bool(
            existing
            and existing["sha256"] == fingerprint["sha256"]
            and int(existing["size_bytes"]) == int(fingerprint["size_bytes"])
        )
        timestamp = _now()
        if existing:
            status = existing["status"] if same_content else "pending"
            session_uri = existing["session_uri"] if same_content else ""
            uploaded_bytes = int(existing["uploaded_bytes"]) if same_content else 0
            self.conn.execute(
                """
                UPDATE archive_uploads SET artifact_type = ?, source_run_id = ?,
                    logical_path = ?, local_path = ?, remote_name = ?, remote_parent_id = ?,
                    size_bytes = ?, md5 = ?, sha256 = ?, status = ?, session_uri = ?,
                    uploaded_bytes = ?, updated_at = ?, last_error = '', metadata_json = ?
                WHERE artifact_key = ?
                """,
                (
                    artifact_type,
                    source_run_id,
                    logical_path,
                    str(local_path),
                    remote_name,
                    parent_id,
                    int(fingerprint["size_bytes"]),
                    fingerprint["md5"],
                    fingerprint["sha256"],
                    status,
                    session_uri,
                    uploaded_bytes,
                    timestamp,
                    json.dumps(dict(metadata or {}), ensure_ascii=False),
                    artifact_key,
                ),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO archive_uploads (
                    artifact_key, project, artifact_type, source_run_id, logical_path,
                    local_path, remote_name, remote_parent_id, size_bytes, md5, sha256,
                    status, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    artifact_key,
                    self.project,
                    artifact_type,
                    source_run_id,
                    logical_path,
                    str(local_path),
                    remote_name,
                    parent_id,
                    int(fingerprint["size_bytes"]),
                    fingerprint["md5"],
                    fingerprint["sha256"],
                    timestamp,
                    timestamp,
                    json.dumps(dict(metadata or {}), ensure_ascii=False),
                ),
            )
        self.conn.commit()
        row = self._ledger_row(artifact_key)
        if row is None:
            raise DriveArchiveError(f"Failed to create upload ledger entry: {logical_path}")
        return row

    def _upload_artifact(
        self,
        local_path: Path,
        *,
        parent_id: str,
        logical_path: str,
        artifact_type: str,
        source_run_id: str = "",
        remote_name: str = "",
        metadata: Mapping[str, Any] | None = None,
        fingerprint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not local_path.is_file():
            raise DriveArchiveError(f"Archive file does not exist: {local_path}")
        remote_name = remote_name or local_path.name
        artifact_key = self._artifact_key(logical_path)
        fingerprint = dict(fingerprint or file_fingerprint(local_path))
        row = self._prepare_ledger(
            artifact_key=artifact_key,
            artifact_type=artifact_type,
            source_run_id=source_run_id,
            logical_path=logical_path,
            local_path=local_path,
            parent_id=parent_id,
            remote_name=remote_name,
            fingerprint=fingerprint,
            metadata=metadata,
        )
        if row["status"] == "verified":
            try:
                remote = self.client.get_file(row["remote_file_id"])
            except Exception as exc:
                remote = None
                remote_error = str(exc)
            else:
                remote_error = ""
            if remote and DriveRestClient._matches_fingerprint(
                remote, int(fingerprint["size_bytes"]), _text(fingerprint["md5"])
            ):
                return {
                    "artifact_key": artifact_key,
                    "status": "already_verified",
                    "remote_file_id": row["remote_file_id"],
                    **fingerprint,
                }
            self.conn.execute(
                "UPDATE archive_uploads SET status = 'pending', updated_at = ?, last_error = ? "
                "WHERE artifact_key = ?",
                (_now(), f"Remote re-verification failed: {remote_error}"[:2000], artifact_key),
            )
            self.conn.commit()
            row = self._ledger_row(artifact_key)
            if row is None:
                raise DriveArchiveError(f"Upload ledger entry disappeared: {logical_path}")

        self.conn.execute(
            "UPDATE archive_uploads SET status = 'uploading', attempts = attempts + 1, updated_at = ? "
            "WHERE artifact_key = ?",
            (_now(), artifact_key),
        )
        self.conn.commit()

        def progress(session_uri: str, uploaded_bytes: int) -> None:
            self.conn.execute(
                """
                UPDATE archive_uploads SET session_uri = ?, uploaded_bytes = ?,
                    status = 'uploading', updated_at = ? WHERE artifact_key = ?
                """,
                (session_uri, uploaded_bytes, _now(), artifact_key),
            )
            self.conn.commit()

        try:
            remote = self.client.upload_file(
                local_path,
                parent_id=parent_id,
                remote_name=remote_name,
                artifact_key=artifact_key,
                artifact_type=artifact_type,
                project_slug=self.project_slug,
                fingerprint=fingerprint,
                remote_file_id=_text(row["remote_file_id"]),
                session_uri=_text(row["session_uri"]),
                uploaded_bytes=int(row["uploaded_bytes"] or 0),
                progress=progress,
            )
        except Exception as exc:
            self.conn.execute(
                "UPDATE archive_uploads SET status = 'failed', updated_at = ?, last_error = ? "
                "WHERE artifact_key = ?",
                (_now(), str(exc)[:2000], artifact_key),
            )
            self.conn.commit()
            raise

        verified_at = _now()
        self.conn.execute(
            """
            UPDATE archive_uploads SET status = 'verified', remote_file_id = ?,
                remote_size_bytes = ?, remote_md5 = ?, session_uri = '', uploaded_bytes = ?,
                verified_at = ?, updated_at = ?, last_error = '' WHERE artifact_key = ?
            """,
            (
                _text(remote.get("id")),
                int(remote.get("size") or 0),
                _text(remote.get("md5Checksum")),
                int(fingerprint["size_bytes"]),
                verified_at,
                verified_at,
                artifact_key,
            ),
        )
        self.conn.commit()
        return {
            "artifact_key": artifact_key,
            "status": "verified",
            "remote_file_id": _text(remote.get("id")),
            **fingerprint,
        }

    def _ensure_child_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key not in self.folder_cache:
            self.folder_cache[key] = self.client.ensure_folder(name, parent_id)
        return self.folder_cache[key]

    def _parent_for_relative(self, base_folder_id: str, relative_parent: Path) -> str:
        parent_id = base_folder_id
        for part in relative_parent.parts:
            if part not in {"", "."}:
                parent_id = self._ensure_child_folder(parent_id, part)
        return parent_id

    def _manifest_is_verified(self, logical_path: str) -> sqlite3.Row | None:
        row = self._ledger_row(self._artifact_key(logical_path))
        if not row or row["status"] != "verified" or not row["remote_file_id"]:
            return None
        try:
            remote = self.client.get_file(row["remote_file_id"])
        except Exception:
            return None
        if DriveRestClient._matches_fingerprint(remote, int(row["size_bytes"]), row["md5"]):
            return row
        return None

    def archive_raw_run(
        self,
        session_path: Path,
        *,
        run_id: str,
        platform: str,
        run_metadata: Mapping[str, Any] | None = None,
        allow_delete: bool = True,
    ) -> dict[str, Any]:
        logical_manifest = f"raw_runs/{run_id}/_manifest.json"
        verified_manifest = self._manifest_is_verified(logical_manifest)
        if verified_manifest:
            deleted = False
            if allow_delete and self.config.delete_local_after_upload and session_path.exists():
                self._delete_raw_run(session_path)
                deleted = True
            return {
                "status": "already_verified",
                "run_id": run_id,
                "remote_manifest_id": verified_manifest["remote_file_id"],
                "local_deleted": deleted,
            }
        if not session_path.is_dir():
            raise DriveArchiveError(f"Raw run is missing before Drive verification: {session_path}")

        run_folder_id = self.client.ensure_folder(
            run_id,
            self.folders["raw_runs"],
            app_properties={"slRunId": run_id[:120], "slPlatform": platform[:120]},
        )
        files = sorted(path for path in session_path.rglob("*") if path.is_file())
        if not files:
            return {"status": "empty", "run_id": run_id, "files": 0, "local_deleted": False}

        manifest_entries = []
        uploaded = 0
        skipped = 0
        total_bytes = 0
        for local_file in files:
            relative = local_file.relative_to(session_path)
            parent_id = self._parent_for_relative(run_folder_id, relative.parent)
            logical_path = f"raw_runs/{run_id}/{relative.as_posix()}"
            fingerprint = file_fingerprint(local_file)
            result = self._upload_artifact(
                local_file,
                parent_id=parent_id,
                logical_path=logical_path,
                artifact_type="raw_run_file",
                source_run_id=run_id,
                metadata={"platform": platform, "relative_path": relative.as_posix()},
                fingerprint=fingerprint,
            )
            total_bytes += int(fingerprint["size_bytes"])
            uploaded += result["status"] == "verified"
            skipped += result["status"] == "already_verified"
            manifest_entries.append(
                {
                    "path": relative.as_posix(),
                    **fingerprint,
                    "remote_file_id": result["remote_file_id"],
                }
            )

        manifest = {
            "schema_version": 1,
            "project": self.project,
            "project_slug": self.project_slug,
            "run_id": run_id,
            "platform": platform,
            "archived_at": _now(),
            "source_session": str(session_path),
            "run_metadata": dict(run_metadata or {}),
            "file_count": len(manifest_entries),
            "total_bytes": total_bytes,
            "files": manifest_entries,
        }
        manifest_dir = self.paths["state"] / "drive_manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{run_id}.json"
        self._atomic_json(manifest_path, manifest)
        manifest_result = self._upload_artifact(
            manifest_path,
            parent_id=run_folder_id,
            logical_path=logical_manifest,
            artifact_type="raw_run_manifest",
            source_run_id=run_id,
            remote_name="_manifest.json",
            metadata={"platform": platform, "file_count": len(manifest_entries)},
        )
        deleted = False
        if allow_delete and self.config.delete_local_after_upload:
            self._delete_raw_run(session_path)
            deleted = True
        return {
            "status": "verified",
            "run_id": run_id,
            "files": len(manifest_entries),
            "bytes": total_bytes,
            "uploaded": uploaded,
            "already_verified": skipped,
            "remote_manifest_id": manifest_result["remote_file_id"],
            "local_deleted": deleted,
        }

    def archive_successful_backlog(self) -> dict[str, Any]:
        results = []
        rows = self.conn.execute(
            """
            SELECT run_id, platform, session_path, started_at, finished_at, source_key,
                   source_kind, source_value, source_role
            FROM runs WHERE project = ? AND status = 'success' AND session_path != ''
            ORDER BY started_at
            """,
            (self.project,),
        ).fetchall()
        for row in rows:
            session_path = Path(row["session_path"])
            logical_manifest = f"raw_runs/{row['run_id']}/_manifest.json"
            if not session_path.exists() and not self._manifest_is_verified(logical_manifest):
                results.append({"run_id": row["run_id"], "status": "missing_local_unverified"})
                continue
            results.append(
                self.archive_raw_run(
                    session_path,
                    run_id=row["run_id"],
                    platform=row["platform"],
                    run_metadata=dict(row),
                    allow_delete=True,
                )
            )
        return {
            "runs_considered": len(rows),
            "results": results,
            "verified": sum(result.get("status") in {"verified", "already_verified"} for result in results),
        }

    def archive_outputs(self) -> dict[str, Any]:
        roots: list[tuple[str, Path, str]] = []
        if self.config.upload_compiled:
            roots.append(("compiled", self.paths["compiled"], "compiled_output"))
        if self.config.upload_logs:
            roots.append(("logs", self.paths["logs"], "pipeline_log"))
        results = []
        for section, local_root, artifact_type in roots:
            if not local_root.exists():
                continue
            for local_file in sorted(path for path in local_root.rglob("*") if path.is_file()):
                relative = local_file.relative_to(local_root)
                parent_id = self._parent_for_relative(self.folders[section], relative.parent)
                results.append(
                    self._upload_artifact(
                        local_file,
                        parent_id=parent_id,
                        logical_path=f"{section}/{relative.as_posix()}",
                        artifact_type=artifact_type,
                        metadata={"section": section, "relative_path": relative.as_posix()},
                    )
                )
        manifest = {
            "schema_version": 1,
            "project": self.project,
            "generated_at": _now(),
            "artifact_count": len(results),
            "artifacts": results,
        }
        manifest_path = self.paths["state"] / "drive_manifests" / "project_outputs.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(manifest_path, manifest)
        manifest_result = self._upload_artifact(
            manifest_path,
            parent_id=self.folders["manifests"],
            logical_path="manifests/project_outputs.json",
            artifact_type="project_output_manifest",
        )
        return {
            "status": "verified",
            "artifact_count": len(results),
            "manifest_file_id": manifest_result["remote_file_id"],
        }

    def archive_database_snapshot(self, database_path: Path) -> dict[str, Any]:
        snapshot_dir = self.paths["state"] / "drive_snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / "scrape_state_latest.sqlite"
        temporary = snapshot_path.with_name(f"{snapshot_path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        self.conn.commit()
        page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        estimated_size = max(
            database_path.stat().st_size if database_path.exists() else 0,
            page_count * page_size,
        )
        free_bytes = shutil.disk_usage(snapshot_dir).free
        required_bytes = estimated_size + (128 * 1024 * 1024)
        if free_bytes < required_bytes:
            raise DriveArchiveError(
                "Not enough local space for a consistent SQLite snapshot: "
                f"need about {required_bytes} bytes, have {free_bytes} bytes"
            )
        destination = sqlite3.connect(temporary)
        try:
            self.conn.backup(destination, pages=4096, sleep=0.01)
            destination.commit()
        finally:
            destination.close()
        redacted = sqlite3.connect(temporary)
        try:
            redacted.execute("UPDATE archive_uploads SET session_uri = ''")
            redacted.commit()
        finally:
            redacted.close()
        os.replace(temporary, snapshot_path)
        result = self._upload_artifact(
            snapshot_path,
            parent_id=self.folders["state"],
            logical_path="state/scrape_state_latest.sqlite",
            artifact_type="sqlite_snapshot",
            metadata={"source_database": str(database_path), "snapshot_at": _now()},
        )
        if result["status"] in {"verified", "already_verified"}:
            snapshot_path.unlink(missing_ok=True)
        return result

    def status_summary(self) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS artifact_count, COALESCE(SUM(size_bytes), 0) AS size_bytes
            FROM archive_uploads WHERE project = ? GROUP BY status ORDER BY status
            """,
            (self.project,),
        ).fetchall()
        return {
            "provider": self.config.provider,
            "remote_project_folder_id": self.folders.get("project", ""),
            "statuses": [dict(row) for row in rows],
        }

    def _delete_raw_run(self, session_path: Path) -> None:
        raw_root = self.paths["raw_runs"].resolve()
        resolved = session_path.resolve()
        try:
            relative = resolved.relative_to(raw_root)
        except ValueError as exc:
            raise DriveArchiveError(f"Refusing to delete raw run outside {raw_root}: {resolved}") from exc
        if not relative.parts or resolved == raw_root:
            raise DriveArchiveError(f"Refusing to delete raw run root: {resolved}")
        shutil.rmtree(resolved)

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
