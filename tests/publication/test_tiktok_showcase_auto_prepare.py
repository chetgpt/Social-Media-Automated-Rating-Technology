from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

import tiktok_publication_adapter as publication_adapter
import tiktok_showcase_auto_prepare as auto_prepare


def _valid_config(tmp_path: Path) -> tuple[Path, dict]:
    capture_root = tmp_path / "captures"
    public_directory = tmp_path / "public"
    capture_root.mkdir()
    public_directory.mkdir()
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"test executable placeholder")
    payload = {
        "schema_version": auto_prepare.CONFIG_SCHEMA_VERSION,
        "enabled": True,
        "public_media_verified": True,
        "capture_root": str(capture_root.resolve()),
        "public_directory": str(public_directory.resolve()),
        "public_base_url": "https://media.example.test/tiktok/showcases/",
        "ffmpeg_path": str(ffmpeg.resolve()),
    }
    config_path = tmp_path / "showcase-auto-prepare.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path, payload


def test_config_is_explicit_secret_free_and_uses_verified_absolute_paths(
    tmp_path,
):
    config_path, payload = _valid_config(tmp_path)

    config = auto_prepare.load_showcase_auto_prepare_config(config_path)

    assert config.enabled is True
    assert config.capture_root == Path(payload["capture_root"])
    assert config.public_directory == Path(payload["public_directory"])
    assert config.public_base_url == payload["public_base_url"]
    assert config.ffmpeg_path == Path(payload["ffmpeg_path"])


def test_config_rejects_secret_fields_without_echoing_the_secret(tmp_path):
    config_path, payload = _valid_config(tmp_path)
    secret = "must-never-be-reported"
    payload["access_token"] = secret
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        auto_prepare.ShowcaseAutoPrepareError,
        match="must not contain secret fields",
    ) as captured:
        auto_prepare.load_showcase_auto_prepare_config(config_path)

    assert secret not in str(captured.value)


def test_disabled_config_performs_no_media_work(tmp_path, monkeypatch):
    config_path = tmp_path / "disabled.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": auto_prepare.CONFIG_SCHEMA_VERSION,
                "enabled": False,
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    monkeypatch.setattr(
        auto_prepare,
        "connect_database",
        lambda _path: conn,
    )
    monkeypatch.setattr(
        auto_prepare,
        "get_showcase",
        lambda *_args, **_kwargs: {"showcase_id": "showcase-1"},
    )
    monkeypatch.setattr(
        auto_prepare,
        "stage_comment_screenshot",
        lambda *_args, **_kwargs: pytest.fail("media work must stay disabled"),
    )

    result = auto_prepare.auto_prepare_showcase(
        tmp_path / "workflow.sqlite",
        showcase_id="showcase-1",
        config_path=config_path,
    )

    assert result["attempted"] is False
    assert result["status"] == "disabled"
    assert result["showcase"]["showcase_id"] == "showcase-1"


def test_prepare_binds_only_the_deterministic_api_ready_media(
    tmp_path,
    monkeypatch,
):
    config_path, _ = _valid_config(tmp_path)
    config = auto_prepare.load_showcase_auto_prepare_config(config_path)
    source = config.capture_root / "exact-comment.png"
    source.write_bytes(b"raw capture")
    staged_path = config.public_directory / ("a" * 64 + ".jpg")
    calls = []

    monkeypatch.setattr(
        auto_prepare,
        "get_showcase",
        lambda _conn, _showcase_id: {
            "showcase_id": "showcase-1",
            "media_path": str(source),
            "media_sha256": "b" * 64,
        },
    )

    def fake_stage(source_path, **kwargs):
        calls.append(("stage", source_path, kwargs))
        return {
            "media_path": str(staged_path),
            "media_sha256": "a" * 64,
            "media_bytes": 1234,
            "mime_type": "image/jpeg",
            "media_url": (
                "https://media.example.test/tiktok/showcases/" + "a" * 64 + ".jpg"
            ),
        }

    def fake_bind(_conn, **kwargs):
        calls.append(("bind", kwargs))
        return {"showcase_id": "showcase-1", "status": "publish_media_ready"}

    monkeypatch.setattr(auto_prepare, "stage_comment_screenshot", fake_stage)
    monkeypatch.setattr(auto_prepare, "bind_publish_media", fake_bind)

    result = auto_prepare._prepare(
        object(),
        showcase_id="showcase-1",
        config=config,
    )

    assert result["status"] == "publish_media_ready"
    assert calls[0][0] == "stage"
    assert calls[0][2]["expected_source_hash"] == "b" * 64
    assert calls[0][2]["allowed_source_root"] == config.capture_root
    assert calls[1][0] == "bind"
    assert calls[1][1]["media_sha256"] == "a" * 64
    assert calls[1][1]["public_media_url"].endswith("a" * 64 + ".jpg")


def test_preparation_failure_is_durable_but_confirmed_receipt_is_unchanged(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "workflow.sqlite"
    receipt_payload = json.dumps(
        {
            "submitted_text": "already confirmed",
            "exact_comment_screenshot": {"sha256": "a" * 64},
        },
        sort_keys=True,
    )
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            response_json TEXT NOT NULL
        );
        CREATE TABLE tiktok_comment_showcase_jobs (
            showcase_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            error TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO publication_receipts VALUES (?, 'published', ?)",
        ("receipt-1", receipt_payload),
    )
    conn.execute(
        "INSERT INTO tiktok_comment_showcase_jobs VALUES "
        "('showcase-1', 'captured', '', 'before')"
    )
    conn.commit()
    conn.close()

    def bare_connect(path):
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(auto_prepare, "connect_database", bare_connect)
    monkeypatch.setattr(
        auto_prepare,
        "get_showcase",
        lambda connection, showcase_id: dict(
            connection.execute(
                "SELECT * FROM tiktok_comment_showcase_jobs "
                "WHERE showcase_id=?",
                (showcase_id,),
            ).fetchone()
        ),
    )
    invalid_config = tmp_path / "unsafe.json"
    secret = "must-never-be-stored"
    invalid_config.write_text(
        json.dumps(
            {
                "schema_version": auto_prepare.CONFIG_SCHEMA_VERSION,
                "enabled": True,
                "access_token": secret,
            }
        ),
        encoding="utf-8",
    )

    result = auto_prepare.auto_prepare_showcase(
        database,
        showcase_id="showcase-1",
        config_path=invalid_config,
    )

    assert result["status"] == "preparation_failed"
    assert result["error_persisted"] is True
    assert secret not in result["error"]
    conn = sqlite3.connect(database)
    stored_receipt = conn.execute(
        "SELECT status, response_json FROM publication_receipts"
    ).fetchone()
    job = conn.execute(
        "SELECT status, error FROM tiktok_comment_showcase_jobs"
    ).fetchone()
    conn.close()
    assert stored_receipt == ("published", receipt_payload)
    assert job[0] == "captured"
    assert "secret fields" in job[1]
    assert secret not in job[1]


def test_connection_failure_is_not_reported_as_a_persisted_error(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "disabled.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": auto_prepare.CONFIG_SCHEMA_VERSION,
                "enabled": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        auto_prepare,
        "connect_database",
        lambda _path: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    result = auto_prepare.auto_prepare_showcase(
        tmp_path / "workflow.sqlite",
        showcase_id="showcase-1",
        config_path=config_path,
    )

    assert result["status"] == "preparation_failed"
    assert result["error_persisted"] is False


def test_missing_opt_in_config_never_opens_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auto_prepare,
        "DEFAULT_CONFIG_PATH",
        tmp_path / "missing-showcase-config.json",
    )
    monkeypatch.setattr(
        auto_prepare,
        "connect_database",
        lambda _path: pytest.fail("database must not open without opt-in"),
    )

    result = auto_prepare.auto_prepare_showcase(
        "unused.sqlite",
        showcase_id="showcase-1",
        config_path="",
    )

    assert result == {
        "attempted": False,
        "status": "not_configured",
        "showcase_id": "showcase-1",
        "error": "",
    }


def test_workspace_default_config_is_discovered_without_a_cli_flag(
    tmp_path,
    monkeypatch,
):
    default_config = tmp_path / "showcase_auto_prepare.json"
    default_config.write_text(
        json.dumps(
            {
                "schema_version": auto_prepare.CONFIG_SCHEMA_VERSION,
                "enabled": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(auto_prepare, "DEFAULT_CONFIG_PATH", default_config)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    monkeypatch.setattr(auto_prepare, "connect_database", lambda _path: conn)
    monkeypatch.setattr(
        auto_prepare,
        "get_showcase",
        lambda *_args, **_kwargs: {"showcase_id": "showcase-1"},
    )

    result = auto_prepare.auto_prepare_showcase(
        "unused.sqlite",
        showcase_id="showcase-1",
        config_path="",
    )

    assert result["status"] == "disabled"
    assert result["showcase"]["showcase_id"] == "showcase-1"


def test_publication_adapter_invokes_preparation_after_enqueue(monkeypatch):
    configured_path = Path("showcase-auto-prepare.json")
    captured = {}

    def fake_auto_prepare(database, *, showcase_id, config_path):
        captured.update(
            database=database,
            showcase_id=showcase_id,
            config_path=config_path,
        )
        return {
            "attempted": True,
            "status": "publish_media_ready",
            "showcase_id": showcase_id,
            "media_sha256": "a" * 64,
            "error": "",
            "showcase": {
                "showcase_id": showcase_id,
                "status": "publish_media_ready",
            },
        }

    monkeypatch.setattr(auto_prepare, "auto_prepare_showcase", fake_auto_prepare)

    showcase, preparation, error = (
        publication_adapter.auto_prepare_enqueued_comment_showcase(
            Path("workflow.sqlite"),
            showcase={"showcase_id": "showcase-1", "status": "captured"},
            config_path=configured_path,
        )
    )

    assert captured == {
        "database": Path("workflow.sqlite"),
        "showcase_id": "showcase-1",
        "config_path": configured_path,
    }
    assert showcase["status"] == "publish_media_ready"
    assert preparation["status"] == "publish_media_ready"
    assert "showcase" not in preparation
    assert error == ""
