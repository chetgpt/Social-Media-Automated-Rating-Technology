import argparse
import asyncio
import ast
from pathlib import Path

import pytest

import music_backfill_tiktok as backfill


def candidate(post_id="123", creator="maker"):
    return {
        "post_id": post_id,
        "canonical_url": f"https://www.tiktok.com/@{creator}/video/{post_id}",
        "creator_handle": creator,
        "content_type": "video",
        "base_snapshot_id": f"snapshot-{post_id}",
        "base_evidence_hash": "a" * 64,
        "base_observed_at": "2026-01-01T00:00:00+00:00",
        "base_music_schema": "tiktok-music-evidence-v1",
    }


class FakeMusicCollector:
    def __init__(self):
        self.records = []

    async def enrich_music_record(
        self,
        record,
        *,
        configured_catalogs,
        request_slot_reserver=None,
        provider_cooldown=None,
    ):
        del request_slot_reserver, provider_cooldown
        self.records.append(dict(record))
        return {
            "schema_version": backfill.TARGET_MUSIC_SCHEMA,
            "configured_catalogs": list(configured_catalogs),
            "platform_music": {
                "status": record.get("music_metadata_status", "available")
            },
            "music_evidence_hash": str(record.get("id", "0")).zfill(64)[-64:],
        }


def test_parser_requires_exactly_one_scope_and_one_amount():
    with pytest.raises(SystemExit):
        backfill.parse_args(["run", "--project", "p", "--all-eligible"])
    with pytest.raises(SystemExit):
        backfill.parse_args(
            [
                "run",
                "--project",
                "p",
                "--creator",
                "maker",
                "--topic",
                "banking",
                "--limit",
                "1",
            ]
        )
    with pytest.raises(SystemExit):
        backfill.parse_args(
            ["run", "--project", "p", "--creator", "maker"]
        )
    with pytest.raises(SystemExit):
        backfill.parse_args(
            [
                "run",
                "--project",
                "p",
                "--creator",
                "maker",
                "--all-eligible",
                "--limit",
                "1",
            ]
        )


def test_parser_accepts_repeatable_ids_and_read_only_commands(tmp_path):
    args = backfill.parse_args(
        [
            "--master-database",
            str(tmp_path / "master.sqlite"),
            "run",
            "--project",
            "history",
            "--post-id",
            "123",
            "--post-id",
            "456",
            "--limit",
            "2",
        ]
    )
    assert args.post_id == ["123", "456"]
    assert args.limit == 2
    assert backfill.parse_args(["status", "--run-id", "r1"]).command == "status"
    assert (
        backfill.parse_args(
            ["export", "--run-id", "r1", "--file", str(tmp_path / "out.jsonl")]
        ).command
        == "export"
    )


def test_scope_normalizes_creator_and_direct_url():
    creator_args = argparse.Namespace(
        creator="https://www.tiktok.com/@BankBCA/",
        topic=None,
        url=None,
        post_id=None,
    )
    assert backfill._scope_from_args(creator_args) == (
        "creator",
        "bankbca",
        {"creator_handle": "bankbca"},
    )
    url_args = argparse.Namespace(
        creator=None,
        topic=None,
        url="https://www.tiktok.com/@Maker/video/123?x=ignored",
        post_id=None,
    )
    assert backfill._scope_from_args(url_args) == (
        "url",
        "https://www.tiktok.com/@maker/video/123",
        {"direct_url": "https://www.tiktok.com/@maker/video/123"},
    )


def test_direct_refresh_collects_no_comments_transcript_or_subtitles():
    class MusicOnlyIntegration:
        async def refresh_video_candidates_from_html(self, page, records):
            assert page == "page"
            assert records == [
                {
                    "id": "123",
                    "url": "https://www.tiktok.com/@maker/video/123",
                    "username": "maker",
                    "content_type": "video",
                }
            ]
            records[0].update(
                {
                    "metadata_refresh_ok": True,
                    "music_metadata_status": "available",
                    "music_title": "Known Song",
                    "music_author": "Known Artist",
                }
            )
            return {"attempted": 1, "hydrated": 1, "failed": 0}

        async def get_comments_for_video(self, *args, **kwargs):
            raise AssertionError("comments are outside MUSIC AUDIT BACKFILL")

        async def get_transcript_for_video(self, *args, **kwargs):
            raise AssertionError("transcripts are outside MUSIC AUDIT BACKFILL")

    music = FakeMusicCollector()
    collector = backfill.MusicBackfillBrowserCollector(music_collector=music)
    checkpoints = []
    result = asyncio.run(
        collector.collect_with_page(
            page="page",
            integration=MusicOnlyIntegration(),
            candidates=[candidate()],
            scope_mode="url",
            scope_value="https://www.tiktok.com/@maker/video/123",
            max_pages=None,
            observation_callback=checkpoints.append,
        )
    )

    assert len(result) == len(checkpoints) == 1
    assert checkpoints[0].status == "completed"
    assert music.records[0]["music_title"] == "Known Song"
    assert "comments" not in music.records[0]
    assert "transcript" not in music.records[0]
    assert "subtitles" not in music.records[0]


def test_exact_ids_fall_back_to_exact_owner_profile_without_substitution():
    class ExactFallbackIntegration:
        last_creator_profile_diagnostics = {
            "terminal_verified": True,
            "inventory_complete": True,
        }

        def __init__(self):
            self.profile_calls = []

        async def refresh_video_candidates_from_html(self, page, records):
            assert [row["id"] for row in records] == ["123", "456"]
            records[0].update(
                {
                    "metadata_refresh_ok": True,
                    "music_metadata_status": "available",
                    "music_title": "Direct Song",
                    "music_author": "Direct Artist",
                }
            )
            records[1]["metadata_refresh_ok"] = False
            return {"attempted": 2, "hydrated": 1, "failed": 1}

        async def discover_creator_profile_posts(
            self, page, creator, *, collect_all, max_pages
        ):
            self.profile_calls.append((page, creator, collect_all, max_pages))
            return [
                {
                    "id": "456",
                    "username": "maker",
                    "music_metadata_status": "available",
                    "music_title": "Profile Song",
                    "music_author": "Profile Artist",
                },
                {
                    "id": "999",
                    "username": "maker",
                    "music_metadata_status": "available",
                    "music_title": "Must Not Enter Frozen Set",
                    "music_author": "Other",
                },
            ]

    integration = ExactFallbackIntegration()
    music = FakeMusicCollector()
    collector = backfill.MusicBackfillBrowserCollector(music_collector=music)
    checkpoints = []
    result = asyncio.run(
        collector.collect_with_page(
            page="page",
            integration=integration,
            candidates=[candidate("123"), candidate("456")],
            scope_mode="post_ids",
            scope_value='["123","456"]',
            max_pages=7,
            observation_callback=checkpoints.append,
        )
    )

    assert integration.profile_calls == [("page", "maker", True, 7)]
    assert [item.candidate["post_id"] for item in result] == ["123", "456"]
    assert [item.status for item in checkpoints] == ["completed", "completed"]
    assert [record["music_title"] for record in music.records] == [
        "Direct Song",
        "Profile Song",
    ]
    assert all(record["id"] != "999" for record in music.records)


def test_exact_fallback_requires_terminal_inventory_before_claiming_absence():
    class IncompleteFallbackIntegration:
        last_creator_profile_diagnostics = {
            "terminal_verified": False,
            "inventory_complete": False,
        }

        async def refresh_video_candidates_from_html(self, page, records):
            records[0]["metadata_refresh_ok"] = False
            return {"attempted": 1, "hydrated": 0, "failed": 1}

        async def discover_creator_profile_posts(self, *args, **kwargs):
            return []

    music = FakeMusicCollector()
    collector = backfill.MusicBackfillBrowserCollector(music_collector=music)
    checkpoints = []
    with pytest.raises(backfill.CreatorInventoryIncompleteError):
        asyncio.run(
            collector.collect_with_page(
                page="page",
                integration=IncompleteFallbackIntegration(),
                candidates=[candidate("123")],
                scope_mode="url",
                scope_value="https://www.tiktok.com/@maker/video/123",
                max_pages=2,
                observation_callback=checkpoints.append,
            )
        )
    assert checkpoints == []
    assert music.records == []


def test_exact_fallback_records_terminal_profile_absence():
    class TerminalFallbackIntegration:
        last_creator_profile_diagnostics = {
            "terminal_verified": True,
            "inventory_complete": True,
        }

        async def refresh_video_candidates_from_html(self, page, records):
            records[0]["metadata_refresh_ok"] = False
            return {"attempted": 1, "hydrated": 0, "failed": 1}

        async def discover_creator_profile_posts(self, *args, **kwargs):
            return []

    music = FakeMusicCollector()
    collector = backfill.MusicBackfillBrowserCollector(music_collector=music)
    checkpoints = []
    asyncio.run(
        collector.collect_with_page(
            page="page",
            integration=TerminalFallbackIntegration(),
            candidates=[candidate("123")],
            scope_mode="post_ids",
            scope_value='["123"]',
            max_pages=None,
            observation_callback=checkpoints.append,
        )
    )

    assert len(checkpoints) == 1
    assert checkpoints[0].status == "unavailable"
    assert checkpoints[0].error == "post_absent_from_terminal_creator_inventory"
    assert music.records[0]["music_metadata_status"] == "unavailable"


def test_exact_fallback_rejects_media_type_mismatch():
    class WrongTypeFallbackIntegration:
        last_creator_profile_diagnostics = {
            "target_handle": "maker",
            "terminal_verified": True,
            "inventory_complete": True,
        }

        async def refresh_video_candidates_from_html(self, page, records):
            records[0]["metadata_refresh_ok"] = False
            return {"attempted": 1, "hydrated": 0, "failed": 1}

        async def discover_creator_profile_posts(self, *args, **kwargs):
            return [
                {
                    "id": "123",
                    "username": "maker",
                    "content_type": "photo",
                    "url": "https://www.tiktok.com/@maker/photo/123",
                    "music_metadata_status": "available",
                }
            ]

    collector = backfill.MusicBackfillBrowserCollector(
        music_collector=FakeMusicCollector()
    )
    with pytest.raises(backfill.MusicBackfillError, match="URL mismatch"):
        asyncio.run(
            collector.collect_with_page(
                page="page",
                integration=WrongTypeFallbackIntegration(),
                candidates=[candidate("123")],
                scope_mode="post_ids",
                scope_value='["123"]',
                max_pages=None,
                observation_callback=lambda observation: None,
            )
        )


def test_exact_fallback_rejects_corrupt_frozen_url_binding_before_profile_call():
    class CorruptFrozenFallbackIntegration:
        last_creator_profile_diagnostics = {}

        def __init__(self):
            self.profile_called = False

        async def refresh_video_candidates_from_html(self, page, records):
            records[0]["metadata_refresh_ok"] = False
            return {"attempted": 1, "hydrated": 0, "failed": 1}

        async def discover_creator_profile_posts(self, *args, **kwargs):
            self.profile_called = True
            return []

    corrupt = candidate("123")
    corrupt["canonical_url"] = "https://www.tiktok.com/@maker/video/999"
    integration = CorruptFrozenFallbackIntegration()
    collector = backfill.MusicBackfillBrowserCollector(
        music_collector=FakeMusicCollector()
    )
    with pytest.raises(backfill.MusicBackfillError, match="identity mismatch"):
        asyncio.run(
            collector.collect_with_page(
                page="page",
                integration=integration,
                candidates=[corrupt],
                scope_mode="post_ids",
                scope_value='["123"]',
                max_pages=None,
                observation_callback=lambda observation: None,
            )
        )
    assert integration.profile_called is False


def test_creator_inventory_is_exact_owner_and_terminal_absence_is_recorded():
    class CreatorIntegration:
        last_creator_profile_diagnostics = {
            "terminal_verified": True,
            "inventory_complete": True,
        }

        async def discover_creator_profile_posts(
            self, page, creator, *, collect_all, max_pages
        ):
            assert page == "page"
            assert creator == "bankbca"
            assert collect_all is True
            assert max_pages is None
            return [
                {
                    "id": "123",
                    "url": "https://www.tiktok.com/@bankbca/video/123",
                    "username": "bankbca",
                    "music_metadata_status": "available",
                    "music_title": "Known",
                    "music_author": "Artist",
                }
            ]

        async def refresh_video_candidates_from_html(self, page, records):
            raise AssertionError("creator backfill must use its one frozen inventory")

    music = FakeMusicCollector()
    collector = backfill.MusicBackfillBrowserCollector(music_collector=music)
    checkpoints = []
    asyncio.run(
        collector.collect_with_page(
            page="page",
            integration=CreatorIntegration(),
            candidates=[candidate("123", "bankbca"), candidate("456", "bankbca")],
            scope_mode="creator",
            scope_value="bankbca",
            max_pages=None,
            observation_callback=checkpoints.append,
        )
    )

    assert [item.status for item in checkpoints] == ["completed", "unavailable"]
    assert checkpoints[1].error == "post_absent_from_terminal_creator_inventory"
    assert music.records[1]["music_metadata_status"] == "unavailable"


def test_creator_inventory_cannot_claim_absence_without_terminal_frontier():
    class IncompleteCreatorIntegration:
        last_creator_profile_diagnostics = {
            "terminal_verified": False,
            "inventory_complete": False,
        }

        async def discover_creator_profile_posts(self, *args, **kwargs):
            return []

    collector = backfill.MusicBackfillBrowserCollector(
        music_collector=FakeMusicCollector()
    )
    checkpoints = []
    with pytest.raises(backfill.CreatorInventoryIncompleteError):
        asyncio.run(
            collector.collect_with_page(
                page="page",
                integration=IncompleteCreatorIntegration(),
                candidates=[candidate("123", "bankbca")],
                scope_mode="creator",
                scope_value="bankbca",
                max_pages=2,
                observation_callback=checkpoints.append,
            )
        )
    assert checkpoints == []


def test_creator_inventory_rejects_owner_mismatch():
    class WrongOwnerIntegration:
        last_creator_profile_diagnostics = {
            "terminal_verified": True,
            "inventory_complete": True,
        }

        async def discover_creator_profile_posts(self, *args, **kwargs):
            return [{"id": "123", "username": "someone_else"}]

    collector = backfill.MusicBackfillBrowserCollector(
        music_collector=FakeMusicCollector()
    )
    with pytest.raises(backfill.MusicBackfillError, match="owner mismatch"):
        asyncio.run(
            collector.collect_with_page(
                page="page",
                integration=WrongOwnerIntegration(),
                candidates=[candidate("123", "bankbca")],
                scope_mode="creator",
                scope_value="bankbca",
                max_pages=None,
                observation_callback=lambda observation: None,
            )
        )


def test_run_freezes_limited_selection_and_passes_target_schema(monkeypatch):
    selected_calls = []
    registered = []
    fake_conn = object()

    monkeypatch.setattr(backfill.master_state, "connect_master", lambda path: fake_conn)
    monkeypatch.setattr(
        backfill.master_state,
        "select_music_backfill_candidates",
        lambda conn, schema, **kwargs: (
            selected_calls.append(kwargs)
            or [candidate("1"), candidate("2"), candidate("3")]
        ),
        raising=False,
    )

    def register(conn, schema, **kwargs):
        registered.append(kwargs)
        return kwargs

    monkeypatch.setattr(
        backfill.master_state, "register_music_backfill_run", register,
        raising=False,
    )
    monkeypatch.setattr(
        backfill,
        "_execute_run",
        lambda conn, **kwargs: _finished_packet(kwargs["run_id"]),
    )

    class Args:
        master_database = "master.sqlite"
        project = "history"
        creator = "maker"
        topic = None
        url = None
        post_id = None
        all_eligible = False
        limit = 2
        target_schema = backfill.TARGET_MUSIC_SCHEMA
        retry_status = None
        force = False
        expected_account = ""
        max_pages = None

    # ``object`` has no close method, so use a minimal connection shell.
    class Conn:
        def close(self):
            pass

    fake_conn = Conn()
    result = backfill._run_command(Args())

    assert result["status"] == "backfill_complete"
    assert selected_calls[0]["target_schema_version"] == backfill.TARGET_MUSIC_SCHEMA
    assert [item["post_id"] for item in registered[0]["candidates"]] == ["1", "2"]


def test_run_rejects_unknown_exact_url_before_run_creation(monkeypatch):
    class Conn:
        def close(self):
            pass

    class Args:
        master_database = "master.sqlite"
        project = "history"
        creator = None
        topic = None
        url = "https://www.tiktok.com/@maker/video/123"
        post_id = None
        all_eligible = True
        limit = None
        target_schema = backfill.TARGET_MUSIC_SCHEMA
        retry_status = None
        force = False
        expected_account = ""
        max_pages = None

    monkeypatch.setattr(backfill.master_state, "connect_master", lambda path: Conn())
    monkeypatch.setattr(
        backfill.master_state,
        "select_music_backfill_candidates",
        lambda conn, schema, **kwargs: [],
    )
    monkeypatch.setattr(
        backfill,
        "_register_run",
        lambda *args, **kwargs: pytest.fail("unknown URL must not create a run"),
    )

    with pytest.raises(backfill.MusicBackfillError, match="unknown music backfill post ID: 123"):
        backfill._run_command(Args())


def test_run_rejects_every_unknown_id_in_mixed_exact_scope(monkeypatch):
    calls = []

    class Conn:
        def close(self):
            pass

    class Args:
        master_database = "master.sqlite"
        project = "history"
        creator = None
        topic = None
        url = None
        post_id = ["123", "456", "789"]
        all_eligible = False
        limit = 1
        target_schema = backfill.TARGET_MUSIC_SCHEMA
        retry_status = None
        force = False
        expected_account = ""
        max_pages = None

    def select(conn, schema, **kwargs):
        calls.append(kwargs)
        assert kwargs["force"] is True
        return [candidate("123"), candidate("789")]

    monkeypatch.setattr(backfill.master_state, "connect_master", lambda path: Conn())
    monkeypatch.setattr(backfill.master_state, "select_music_backfill_candidates", select)
    monkeypatch.setattr(
        backfill,
        "_register_run",
        lambda *args, **kwargs: pytest.fail("mixed unknown IDs must not create a run"),
    )

    with pytest.raises(backfill.MusicBackfillError, match="unknown music backfill post ID: 456"):
        backfill._run_command(Args())
    assert len(calls) == 1


def test_run_allows_known_but_ineligible_exact_scope_to_complete_empty(monkeypatch):
    calls = []
    registered = []
    finalized = []

    class Conn:
        def close(self):
            pass

    class Args:
        master_database = "master.sqlite"
        project = "history"
        creator = None
        topic = None
        url = None
        post_id = ["123"]
        all_eligible = True
        limit = None
        target_schema = backfill.TARGET_MUSIC_SCHEMA
        retry_status = None
        force = False
        expected_account = ""
        max_pages = None

    def select(conn, schema, **kwargs):
        calls.append(kwargs)
        return [candidate("123")] if kwargs["force"] else []

    def register(conn, *, args, scope_mode, scope_value, candidates, retry_statuses):
        registered.append(list(candidates))
        return "backfill-empty"

    monkeypatch.setattr(backfill.master_state, "connect_master", lambda path: Conn())
    monkeypatch.setattr(backfill.master_state, "select_music_backfill_candidates", select)
    monkeypatch.setattr(backfill, "_register_run", register)
    monkeypatch.setattr(
        backfill,
        "_finalize_run",
        lambda conn, run_id, **kwargs: finalized.append(run_id),
    )
    monkeypatch.setattr(
        backfill,
        "_status_packet",
        lambda conn, run_id: {
            "run_id": run_id,
            "status": "backfill_complete",
            "selected": 0,
        },
    )

    result = backfill._run_command(Args())

    assert [call["force"] for call in calls] == [True, False]
    assert registered == [[]]
    assert finalized == ["backfill-empty"]
    assert result["selected"] == 0


def test_run_rejects_exact_url_with_wrong_frozen_identity(monkeypatch):
    class Conn:
        def close(self):
            pass

    class Args:
        master_database = "master.sqlite"
        project = "history"
        creator = None
        topic = None
        url = "https://www.tiktok.com/@imposter/video/123"
        post_id = None
        all_eligible = True
        limit = None
        target_schema = backfill.TARGET_MUSIC_SCHEMA
        retry_status = None
        force = False
        expected_account = ""
        max_pages = None

    monkeypatch.setattr(backfill.master_state, "connect_master", lambda path: Conn())
    monkeypatch.setattr(
        backfill.master_state,
        "select_music_backfill_candidates",
        lambda conn, schema, **kwargs: [candidate("123", "maker")],
    )
    monkeypatch.setattr(
        backfill,
        "_register_run",
        lambda *args, **kwargs: pytest.fail("mismatched URL must not create a run"),
    )

    with pytest.raises(backfill.MusicBackfillError, match="unknown music backfill URL"):
        backfill._run_command(Args())


async def _finished_packet(run_id):
    return {"run_id": run_id, "status": "backfill_complete"}


def test_resume_skips_append_only_terminal_observations(monkeypatch):
    run = {
        "run_id": "r1",
        "status": "planned",
        "scope_mode": "post_ids",
        "scope_value": '["123","456"]',
        "target_schema_version": backfill.TARGET_MUSIC_SCHEMA,
        "expected_account": "",
        "candidates": [candidate("123"), candidate("456")],
    }
    observations = [
        {
            "post_id": "123",
            "status": "completed",
            "music_evidence_hash": "1" * 64,
        }
    ]
    updates = []

    monkeypatch.setattr(
        backfill,
        "_sanitize_music_evidence",
        lambda supplied, *, expected_catalogs: dict(supplied),
    )

    monkeypatch.setattr(
        backfill.master_state,
        "get_music_backfill_run",
        lambda conn, schema, *, run_id: run,
        raising=False,
    )
    monkeypatch.setattr(
        backfill.master_state,
        "music_backfill_observations",
        lambda conn, schema, *, run_id, validate_base_snapshots=True: list(
            observations
        ),
        raising=False,
    )
    monkeypatch.setattr(
        backfill.master_state,
        "update_music_backfill_run",
        lambda conn, schema, **kwargs: updates.append(kwargs) or run.update(kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        backfill.master_state,
        "reserve_provider_request_slot",
        lambda *args, **kwargs: 0.0,
    )
    monkeypatch.setattr(
        backfill.master_state, "defer_provider_requests", lambda *args, **kwargs: None
    )

    def record(conn, schema, **kwargs):
        assert kwargs["post_id"] == "456"
        observations.append(dict(kwargs))
        return {"created": True, **kwargs}

    monkeypatch.setattr(
        backfill.master_state, "record_music_backfill_observation", record,
        raising=False,
    )

    def finalize(conn, schema, *, run_id, error=""):
        run["status"] = (
            "backfill_complete"
            if len(backfill._completed_post_ids(observations)) == 2
            else "backfill_incomplete"
        )
        run["error"] = error
        return run

    monkeypatch.setattr(
        backfill.master_state, "finalize_music_backfill_run", finalize,
        raising=False,
    )

    class ResumeCollector:
        async def collect(self, *, candidates, observation_callback, **kwargs):
            assert [item["post_id"] for item in candidates] == ["456"]
            await backfill._maybe_await(
                observation_callback(
                    backfill.MusicObservation(
                        candidate=dict(candidates[0]),
                        music_observed_at="2026-02-01T00:00:00+00:00",
                        music_evidence={
                            "schema_version": backfill.TARGET_MUSIC_SCHEMA,
                            "music_evidence_hash": "2" * 64,
                        },
                    )
                )
            )
            return []

    result = asyncio.run(
        backfill._execute_run(
            object(),
            run_id="r1",
            collector=ResumeCollector(),
        )
    )

    assert result["status"] == "backfill_complete"
    assert [row["post_id"] for row in observations] == ["123", "456"]
    assert updates[0]["status"] == "running"


def test_module_has_no_ai_or_publication_imports():
    source = Path(backfill.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {
        "openai",
        "tiktok_publication_adapter",
        "publish_pending",
        "tiktok_scraper.analysis_workflow",
    }
    assert imported.isdisjoint(forbidden)
