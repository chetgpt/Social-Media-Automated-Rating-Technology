import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

import publish_pending
import tiktok_publication_adapter as publication_adapter
from tiktok_master_database import (
    attach_master_database,
    comment_target_guard,
    mark_submit_intent,
    register_publication_claim,
    register_publication_outcome,
)
from tiktok_publication_adapter import (
    capture_targets_content,
    deterministic_public_rating,
    prepare_final_publication_text,
    tiktok_video_id_from_url,
)


def _create_queue(database, rows):
    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            target_url TEXT NOT NULL,
            mode TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            engage_run_id TEXT NOT NULL,
            engage_post_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO publication_queue (
            publication_id, platform, status, target_url, mode,
            draft_text, decision_json, engage_run_id, engage_post_id, created_at
        ) VALUES (
            ?, 'tiktok', 'approved', ?, 'live', ?, '{}',
            'engage-run', 'engage-post', ?
        )
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _approved_publication(text=None):
    final_text = text or (
        "AI review 8.4/10: the explanation is useful; adding the source "
        "would make it easier for viewers to verify."
    )
    return {
        "analysis_score": 84,
        "draft_text": final_text,
        "draft_hash": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        "decision_json": json.dumps(
            {
                "publish": True,
                "score": 84,
                "policy_violations": [],
                "public_rating": {
                    "enabled": True,
                    "required": True,
                    "scale": 10,
                    "increment": 0.1,
                    "value": 8.4,
                    "formatted": "8.4/10",
                },
            }
        ),
    }


def test_legacy_unknown_account_comment_blocks_post_for_every_account(tmp_path):
    source_database = tmp_path / "legacy-source.sqlite"
    master_database = tmp_path / "master.sqlite"
    conn = sqlite3.connect(source_database, isolation_level=None)
    try:
        schema = attach_master_database(conn, master_database)
        conn.execute("BEGIN IMMEDIATE")
        attempt_id = register_publication_claim(
            conn,
            schema,
            account="legacy_unknown",
            post_id="7400000000000000001",
            publication_id="legacy-publication",
            source_path=source_database,
            target_url=(
                "https://www.tiktok.com/@creator/video/"
                "7400000000000000001"
            ),
            text_hash="legacy-text-hash",
            local_attempt_number=1,
        )
        mark_submit_intent(conn, schema, attempt_id)
        register_publication_outcome(
            conn,
            schema,
            attempt_id=attempt_id,
            outcome="confirmed",
            receipt_id="legacy-receipt",
            text_hash="legacy-text-hash",
        )
        conn.commit()

        for account in ("creator", "different-creator"):
            guard = comment_target_guard(
                conn,
                schema,
                account=account,
                post_id="7400000000000000001",
            )
            assert guard["blocked"] is True
            assert guard["account_key"] == "*"
            assert guard["state"] == "confirmed"
    finally:
        conn.close()


def _create_engage_master_binding_database(
    database: Path,
    *,
    master_database: str = "",
) -> None:
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            """
            CREATE TABLE engage_tiktok_runs (
                run_id TEXT PRIMARY KEY,
                master_database TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO engage_tiktok_runs (
                run_id, master_database, updated_at
            ) VALUES ('engage-run', ?, '')
            """,
            (master_database,),
        )
        conn.commit()
    finally:
        conn.close()


def test_blank_engage_master_path_is_bound_once_atomically(tmp_path):
    database = tmp_path / "project.sqlite"
    selected = tmp_path / "master-one.sqlite"
    replacement = tmp_path / "master-two.sqlite"
    _create_engage_master_binding_database(database)
    publication = {"engage_run_id": "engage-run"}

    bound = publication_adapter.bind_engage_master_database(
        database,
        publication,
        selected,
    )

    assert bound == selected.resolve()
    conn = sqlite3.connect(database)
    try:
        stored = conn.execute(
            """
            SELECT master_database
            FROM engage_tiktok_runs
            WHERE run_id='engage-run'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == str(selected.resolve())

    with pytest.raises(RuntimeError, match="does not match"):
        publication_adapter.bind_engage_master_database(
            database,
            publication,
            replacement,
        )
    conn = sqlite3.connect(database)
    try:
        unchanged = conn.execute(
            """
            SELECT master_database
            FROM engage_tiktok_runs
            WHERE run_id='engage-run'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert unchanged == str(selected.resolve())


def test_missing_master_path_uses_workspace_default(tmp_path, monkeypatch):
    database = tmp_path / "project.sqlite"
    selected = tmp_path / "workspace-master.sqlite"
    _create_engage_master_binding_database(database)
    monkeypatch.setattr(
        publication_adapter,
        "DEFAULT_MASTER_DATABASE",
        selected,
    )

    bound = publication_adapter.bind_engage_master_database(
        database,
        {"engage_run_id": "engage-run"},
        None,
    )

    assert bound == selected.resolve()
    assert bound != database.resolve()
    conn = sqlite3.connect(database)
    try:
        stored = conn.execute(
            """
            SELECT master_database
            FROM engage_tiktok_runs
            WHERE run_id='engage-run'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == str(selected.resolve())


def test_unmigrated_engage_run_gets_master_binding_column(tmp_path):
    database = tmp_path / "legacy-project.sqlite"
    selected = tmp_path / "master.sqlite"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            """
            CREATE TABLE engage_tiktok_runs (
                run_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO engage_tiktok_runs (run_id)
            VALUES ('legacy-run')
            """
        )
        conn.commit()
    finally:
        conn.close()

    publication = {"engage_run_id": "legacy-run"}
    bound = publication_adapter.bind_engage_master_database(
        database,
        publication,
        selected,
    )
    assert bound == selected.resolve()
    conn = sqlite3.connect(database)
    try:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(engage_tiktok_runs)"
            ).fetchall()
        }
        stored = conn.execute(
            """
            SELECT master_database
            FROM engage_tiktok_runs
            WHERE run_id='legacy-run'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert "master_database" in columns
    assert stored == str(selected.resolve())


def test_matching_engage_master_path_is_accepted_without_rebinding(tmp_path):
    database = tmp_path / "project.sqlite"
    selected = (tmp_path / "master.sqlite").resolve()
    _create_engage_master_binding_database(
        database,
        master_database=str(selected),
    )
    publication = {"engage_run_id": "engage-run"}

    bound = publication_adapter.bind_engage_master_database(
        database,
        publication,
        selected,
    )

    assert bound == selected
    assert publication["_engage_master_database"] == str(selected)


def test_claim_rejects_master_mismatch_before_attach(tmp_path, monkeypatch):
    database = tmp_path / "project.sqlite"
    stored = (tmp_path / "stored-master.sqlite").resolve()
    selected = tmp_path / "different-master.sqlite"
    _create_engage_master_binding_database(
        database,
        master_database=str(stored),
    )
    attach_called = False

    def unexpected_attach(*args, **kwargs):
        nonlocal attach_called
        attach_called = True
        raise AssertionError("master database must not be attached")

    monkeypatch.setattr(
        publication_adapter,
        "attach_master_database",
        unexpected_attach,
    )

    with pytest.raises(RuntimeError, match="does not match"):
        publication_adapter.claim_publication(
            database,
            {"engage_run_id": "engage-run"},
            daily_limit=0,
            max_attempts=3,
            master_database=selected,
            observed_account="creator",
        )

    assert attach_called is False
    assert not selected.exists()


def test_adapter_rejects_master_mismatch_before_browser_preflight(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "project.sqlite"
    stored = (tmp_path / "stored-master.sqlite").resolve()
    selected = tmp_path / "different-master.sqlite"
    _create_engage_master_binding_database(
        database,
        master_database=str(stored),
    )
    publication = {
        "publication_id": "publication-1",
        "engage_run_id": "engage-run",
    }
    preflight_created = False

    class UnexpectedPreflight:
        def __init__(self, **kwargs):
            nonlocal preflight_created
            preflight_created = True
            raise AssertionError("browser preflight must not start")

    monkeypatch.setattr(
        publication_adapter,
        "load_approved_publication",
        lambda *_args: publication,
    )
    monkeypatch.setattr(
        publication_adapter,
        "SocialBrowserPreflight",
        UnexpectedPreflight,
    )
    args = SimpleNamespace(
        database=str(database),
        publication_id="publication-1",
        master_database=str(selected),
        daily_limit=0,
        max_attempts=3,
    )

    with pytest.raises(RuntimeError, match="does not match"):
        asyncio.run(publication_adapter.run(args))

    assert preflight_created is False
    assert not selected.exists()


def test_publish_pending_requires_an_explicit_selection():
    with pytest.raises(SystemExit):
        publish_pending.parse_args([])

    args = publish_pending.parse_args(
        [
            "--database",
            "engage.sqlite",
            "--publication-id",
            "publication-1",
        ]
    )
    assert args.database == "engage.sqlite"
    assert args.publication_id == "publication-1"
    assert args.all_approved is False
    assert args.engage_run_id is None
    assert args.execute is False
    assert args.daily_limit == 0
    assert args.max_attempts == 3
    assert args.inter_publication_delay == 0
    assert args.continue_on_error is False
    assert args.master_database == str(
        publish_pending.DEFAULT_MASTER_DATABASE
    )
    assert args.social_browser_state == str(
        publish_pending.DEFAULT_SOCIAL_BROWSER_STATE
    )
    assert args.social_browser_state.replace("\\", "/").endswith(
        "comments_data/social_browser/state.json"
    )

    batch = publish_pending.parse_args(
        [
            "--database",
            "engage.sqlite",
            "--all-approved",
            "--engage-run-id",
            "engage-run",
        ]
    )
    assert batch.publication_id is None
    assert batch.all_approved is True
    assert batch.engage_run_id == "engage-run"

    with pytest.raises(SystemExit):
        publish_pending.parse_args(
            [
                "--database",
                "engage.sqlite",
                "--all-approved",
            ]
        )


def test_publish_pending_selects_exactly_one(tmp_path):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI review 8.4/10: this is a sufficiently long approved response.",
                "2026-07-26T10:00:00+07:00",
            ),
            (
                "publication-2",
                "https://www.tiktok.com/@creator/video/2",
                "AI review 8.4/10: this is another sufficiently long response.",
                "2026-07-26T10:01:00+07:00",
            ),
        ],
    )

    selected = publish_pending.select_approved_publications(
        database,
        publication_id="publication-2",
    )
    assert [row["publication_id"] for row in selected] == ["publication-2"]

    with pytest.raises(RuntimeError, match="exactly one publication"):
        publish_pending.select_approved_publications(database, publication_id="")


def test_publish_pending_batch_selection_is_explicitly_run_scoped(tmp_path):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI review 8.4/10: first approved response.",
                "2026-07-26T10:00:00+07:00",
            ),
            (
                "publication-2",
                "https://www.tiktok.com/@creator/video/2",
                "AI review 8.4/10: second approved response.",
                "2026-07-26T10:01:00+07:00",
            ),
        ],
    )
    conn = sqlite3.connect(database)
    conn.execute(
        """
        UPDATE publication_queue SET engage_run_id='different-run'
        WHERE publication_id='publication-2'
        """
    )
    conn.commit()
    conn.close()

    selected = publish_pending.select_approved_publications(
        database,
        all_approved=True,
        engage_run_id="engage-run",
    )
    assert [row["publication_id"] for row in selected] == ["publication-1"]


@pytest.mark.parametrize("approved_count", (50, 137))
def test_publish_pending_processes_every_approved_response_without_a_ceiling(
    tmp_path,
    monkeypatch,
    approved_count,
):
    database = tmp_path / "queue.sqlite"
    rows = [
        (
            f"publication-{index:02d}",
            f"https://www.tiktok.com/@creator/video/{index}",
            f"AI-assisted response {index} with its approved evidence.",
            (
                f"2026-07-{1 + index // 24:02d}"
                f"T{index % 24:02d}:00:00+07:00"
            ),
        )
        for index in range(approved_count)
    ]
    _create_queue(database, rows)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "published"}),
            stderr="",
        )

    monkeypatch.setattr(publish_pending, "supervise_adapter", fake_run)
    result = publish_pending.main(
        [
            "--database",
            str(database),
            "--all-approved",
            "--engage-run-id",
            "engage-run",
            "--execute",
        ]
    )

    assert result == 0
    assert len(commands) == approved_count
    assert [
        command[command.index("--publication-id") + 1]
        for command in commands
    ] == [
        f"publication-{index:02d}"
        for index in range(approved_count)
    ]
    assert all(command.count("--execute") == 1 for command in commands)
    assert all(
        command[command.index("--daily-limit") + 1] == "0"
        for command in commands
    )


def test_publish_pending_applies_optional_cadence_between_live_items(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI-assisted first approved response.",
                "2026-07-26T10:00:00+07:00",
            ),
            (
                "publication-2",
                "https://www.tiktok.com/@creator/video/2",
                "AI-assisted second approved response.",
                "2026-07-26T10:01:00+07:00",
            ),
        ],
    )
    sleeps = []
    monkeypatch.setattr(
        publish_pending,
        "supervise_adapter",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "published"}),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        publish_pending.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    result = publish_pending.main(
        [
            "--database",
            str(database),
            "--all-approved",
            "--engage-run-id",
            "engage-run",
            "--execute",
            "--inter-publication-delay",
            "1.5",
        ]
    )

    assert result == 0
    assert sleeps == [1.5]


def test_publish_pending_stops_on_first_failure_unless_explicitly_overridden(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                f"publication-{index}",
                f"https://www.tiktok.com/@creator/video/{index}",
                f"AI-assisted approved response {index}.",
                f"2026-07-26T10:0{index}:00+07:00",
            )
            for index in range(1, 4)
        ],
    )
    calls = []

    def fail_first(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1,
                command,
                output=json.dumps({"status": "uncertain"}),
                stderr="manual reconciliation required",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "published"}),
            stderr="",
        )

    monkeypatch.setattr(publish_pending, "supervise_adapter", fail_first)
    stopped = publish_pending.main(
        [
            "--database",
            str(database),
            "--all-approved",
            "--engage-run-id",
            "engage-run",
            "--execute",
        ]
    )
    assert stopped == 1
    assert len(calls) == 1

    calls.clear()
    continued = publish_pending.main(
        [
            "--database",
            str(database),
            "--all-approved",
            "--engage-run-id",
            "engage-run",
            "--execute",
            "--continue-on-error",
        ]
    )
    assert continued == 1
    assert len(calls) == 3


def test_live_mode_does_not_imply_execute(tmp_path, monkeypatch):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI review 8.4/10: this source-backed response is ready for review.",
                "2026-07-26T10:00:00+07:00",
            )
        ],
    )
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "dry_run"}),
            stderr="",
        )

    monkeypatch.setattr(publish_pending, "supervise_adapter", fake_run)
    result = publish_pending.main(
        [
            "--database",
            str(database),
            "--publication-id",
            "publication-1",
            "--social-browser-state",
            "comments_data/social_browser/state.json",
        ]
    )

    assert result == 0
    assert "--execute" not in observed["command"]


def test_batch_reports_confirmed_comment_showcase_recovery_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI-assisted approved response.",
                "2026-07-26T10:00:00+07:00",
            )
        ],
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "published",
                    "receipt_id": "receipt-1",
                    "showcase_capture_error": "exact comment not found",
                    "showcase_enqueue_error": "",
                    "showcase_preparation_error": "",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(publish_pending, "supervise_adapter", fake_run)
    result = publish_pending.main(
        [
            "--database",
            str(database),
            "--publication-id",
            "publication-1",
            "--execute",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "showcase needs auxiliary recovery" in output
    assert "publication_id=publication-1, receipt_id=receipt-1" in output
    assert "needs_attention=1" in output


@pytest.mark.parametrize("preparation_status", ("not_configured", "disabled"))
def test_batch_counts_unprepared_showcase_as_needing_attention(
    tmp_path,
    monkeypatch,
    capsys,
    preparation_status,
):
    database = tmp_path / "queue.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI-assisted approved response.",
                "2026-07-26T10:00:00+07:00",
            )
        ],
    )

    monkeypatch.setattr(
        publish_pending,
        "supervise_adapter",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "published",
                    "receipt_id": "receipt-1",
                    "comment_screenshot": {"sha256": "a" * 64},
                    "comment_showcase": {
                        "showcase_id": "showcase-1",
                        "status": "captured",
                    },
                    "showcase_preparation": {
                        "status": preparation_status,
                    },
                }
            ),
            stderr="",
        ),
    )

    result = publish_pending.main(
        [
            "--database",
            str(database),
            "--publication-id",
            "publication-1",
            "--execute",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "queued but not media-ready" in output
    assert f"preparation is {preparation_status}" in output
    assert "publish_media_ready=0" in output
    assert "needs_attention=1" in output


def test_execute_flag_is_forwarded_only_when_explicit():
    dry_run = publish_pending.build_adapter_command(
        database=Path("queue.sqlite"),
        publication_id="publication-1",
        social_browser_state="comments_data/social_browser/state.json",
        execute=False,
    )
    live = publish_pending.build_adapter_command(
        database=Path("queue.sqlite"),
        publication_id="publication-1",
        social_browser_state="comments_data/social_browser/state.json",
        execute=True,
    )

    assert "--execute" not in dry_run
    assert live.count("--execute") == 1
    assert "--cdp-url" not in dry_run
    assert "--social-browser-state" in dry_run
    assert "--master-database" in dry_run
    assert (
        dry_run[dry_run.index("--master-database") + 1]
        == str(publish_pending.DEFAULT_MASTER_DATABASE)
    )
    assert dry_run[dry_run.index("--daily-limit") + 1] == "0"
    assert dry_run[dry_run.index("--max-attempts") + 1] == "3"


def test_showcase_auto_prepare_config_is_forwarded_only_when_explicit():
    dry_run = publish_pending.build_adapter_command(
        database=Path("queue.sqlite"),
        publication_id="publication-1",
        social_browser_state="comments_data/social_browser/state.json",
        execute=False,
    )
    configured = publish_pending.build_adapter_command(
        database=Path("queue.sqlite"),
        publication_id="publication-1",
        social_browser_state="comments_data/social_browser/state.json",
        execute=True,
        showcase_auto_prepare_config="showcase-auto-prepare.json",
    )

    assert "--showcase-auto-prepare-config" not in dry_run
    assert configured[configured.index("--showcase-auto-prepare-config") + 1] == (
        "showcase-auto-prepare.json"
    )


def test_batch_publisher_propagates_explicit_master_database(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "queue.sqlite"
    master = tmp_path / "master.sqlite"
    _create_queue(
        database,
        [
            (
                "publication-1",
                "https://www.tiktok.com/@creator/video/1",
                "AI-assisted approved response.",
                "2026-07-26T10:00:00+07:00",
            )
        ],
    )
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "dry_run"}),
            stderr="",
        )

    monkeypatch.setattr(publish_pending, "supervise_adapter", fake_run)
    result = publish_pending.main(
        [
            "--database",
            str(database),
            "--publication-id",
            "publication-1",
            "--master-database",
            str(master),
        ]
    )

    assert result == 0
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("--master-database") + 1] == str(master)


def test_final_text_uses_deterministic_one_decimal_rating_and_hash():
    value, formatted = deterministic_public_rating(
        84,
        scale=10,
        increment=0.1,
    )
    assert value == 8.4
    assert formatted == "8.4/10"

    publication = _approved_publication()
    final_text, final_hash, rating = prepare_final_publication_text(publication)
    assert final_text == publication["draft_text"]
    assert final_hash == publication["draft_hash"]
    assert rating == "8.4/10"


def test_tiktok_target_identity_must_match_page_and_publish_request():
    assert (
        tiktok_video_id_from_url(
            "https://www.tiktok.com/@creator/video/123456?lang=en"
        )
        == "123456"
    )
    assert (
        tiktok_video_id_from_url(
            "https://www.tiktok.com/@creator/photo/654321?lang=en"
        )
        == "654321"
    )
    assert not tiktok_video_id_from_url("https://www.tiktok.com/explore")
    assert capture_targets_content(
        {
            "request_url": "https://www.tiktok.com/api/comment/publish",
            "request_body_template": {"aweme_id": "123456", "text": "safe"},
        },
        "123456",
    )
    assert not capture_targets_content(
        {
            "request_url": (
                "https://www.tiktok.com/api/comment/publish?aweme_id=other"
            ),
            "request_body_template": {},
        },
        "123456",
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda publication: publication.update(
                draft_text=publication["draft_text"].replace(
                    "8.4/10", "{PUBLIC_RATING}"
                )
            ),
            "unresolved rating placeholder",
        ),
        (
            lambda publication: publication.update(
                draft_text=publication["draft_text"].replace("8.4/10", "8/10")
            ),
            "exactly one deterministic public rating",
        ),
        (
            lambda publication: publication.update(draft_hash="not-the-final-hash"),
            "final-text hash",
        ),
    ],
)
def test_final_text_rejects_placeholder_wrong_rating_or_hash(mutation, message):
    publication = _approved_publication()
    mutation(publication)

    with pytest.raises(RuntimeError, match=message):
        prepare_final_publication_text(publication)


def test_rating_metadata_must_match_completed_analysis_score():
    publication = _approved_publication()
    decision = json.loads(publication["decision_json"])
    decision["public_rating"]["value"] = 8.0
    decision["public_rating"]["formatted"] = "8/10"
    publication["decision_json"] = json.dumps(decision)

    with pytest.raises(RuntimeError, match="deterministic rating"):
        prepare_final_publication_text(publication)


def test_final_text_requires_explicit_ai_disclosure():
    publication = _approved_publication(
        "Review 8.4/10: the explanation is useful; adding the source "
        "would make it easier for viewers to verify."
    )
    publication["draft_hash"] = hashlib.sha256(
        publication["draft_text"].encode("utf-8")
    ).hexdigest()

    with pytest.raises(RuntimeError, match="AI disclosure"):
        prepare_final_publication_text(publication)


def test_adapter_closes_only_its_page_not_the_shared_browser(
    tmp_path,
    monkeypatch,
):
    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self, page):
            self.page = page

        async def cookies(self, _url):
            return [{"name": "sessionid"}]

        async def new_page(self):
            return self.page

    class FakeBrowser:
        def __init__(self, context):
            self.contexts = [context]
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    class FakePlaywright:
        def __init__(self, browser):
            self.chromium = self
            self.browser = browser

        async def connect_over_cdp(self, _url, **_kwargs):
            return self.browser

    class FakePlaywrightContext:
        def __init__(self, playwright):
            self.playwright = playwright

        async def __aenter__(self):
            return self.playwright

        async def start(self):
            return self.playwright

        async def __aexit__(self, *_args):
            return False

    page = FakePage()
    browser = FakeBrowser(FakeContext(page))
    playwright = FakePlaywright(browser)
    database = tmp_path / "queue.sqlite"
    _create_engage_master_binding_database(database)
    publication = {
        "publication_id": "publication-1",
        "project": "engage",
        "target_url": "https://www.tiktok.com/@creator/video/1",
        "engage_run_id": "engage-run",
    }

    monkeypatch.setattr(
        publication_adapter,
        "async_playwright",
        lambda: FakePlaywrightContext(playwright),
    )
    monkeypatch.setattr(
        publication_adapter,
        "load_approved_publication",
        lambda *_args: publication,
    )
    monkeypatch.setattr(
        publication_adapter,
        "daily_publication_count",
        lambda *_args: 0,
    )
    class FakePreflight:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def ensure_ready(self):
            return {
                "reachable": True,
                "observed_account": "creator",
            }

    import social_browser

    monkeypatch.setattr(
        publication_adapter,
        "SocialBrowserPreflight",
        FakePreflight,
    )
    monkeypatch.setattr(
        social_browser,
        "load_engage_profile7_designation",
        lambda runtime_dir: {
            "user_data_dir": "Edge User Data",
            "profile_directory": "Profile 7",
            "designation_id": "profile7-designation",
            "mode": "existing_profile_attach",
        },
    )
    monkeypatch.setattr(
        social_browser,
        "load_verified_profile7_state",
        lambda runtime_dir, designation: {
            "cdp_url": "ws://127.0.0.1:9223/devtools/browser/profile7"
        },
    )

    async def fake_verified_context(received_browser, designation):
        return received_browser.contexts[0], {"verified": True}

    async def fake_authentication(context, platform):
        return {"authenticated": platform == "tiktok"}

    monkeypatch.setattr(
        social_browser,
        "verified_profile_context",
        fake_verified_context,
    )
    monkeypatch.setattr(
        social_browser,
        "platform_authentication",
        fake_authentication,
    )

    async def fake_run_on_page(*_args):
        raise RuntimeError("simulated page failure")

    monkeypatch.setattr(
        publication_adapter,
        "_run_on_page",
        fake_run_on_page,
    )
    args = SimpleNamespace(
        database=str(database),
        publication_id="publication-1",
        daily_limit=5,
        max_attempts=3,
        social_browser_state="comments_data/social_browser/state.json",
        browser_startup_timeout=45,
    )

    with pytest.raises(RuntimeError, match="simulated page failure"):
        asyncio.run(publication_adapter.run(args))

    assert page.closed is True
    assert browser.close_calls == 0
