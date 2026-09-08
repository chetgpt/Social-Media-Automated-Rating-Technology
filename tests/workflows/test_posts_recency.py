"""Offline doubles only: publication-window enforcement and legacy isolation."""

import asyncio
import json
from contextlib import closing

import pytest

import engage_tiktok as engage
from tiktok_scraper.publication_window import normalize_publication_window
from test_engage_tiktok import evidence, RecordingPreflight, run_fake_production_collector


WINDOW = normalize_publication_window("2026-08-24T00:00:00Z", "2026-08-31T00:00:00Z")


def _record(post_id, when=None):
    record = evidence(str(post_id))
    record["caption"] = "Coffee preparation and coffee brewing tutorial"
    if when is not None:
        record["published_at"] = when
    return record


def _run(conn, *, count=1, window=WINDOW, **kwargs):
    return engage.create_run(conn, project="posts_discovery_test", topic="coffee",
        requested_count=count, max_comments=1, max_pages=2, workflow="listen",
        music_catalogs=(), publication_window=window, **kwargs)


class WindowCollector:
    def __init__(self, records, *, bypass_reservation=False, mutate=None):
        self.records = records
        self.bypass_reservation = bypass_reservation
        self.mutate = mutate
        self.received_windows = []
        self.last_diagnostics = {"collection_stop_reason": "source_exhausted"}

    async def collect(self, *, topic, requested_count, max_comments, max_pages,
        publication_window=None, publication_exclusion_callback=None,
        record_callback=None, existing_post_ids=(), initial_evidence_ready_count=0,
        candidate_reserver=None):
        self.received_windows.append(publication_window)
        for original in self.records:
            record = dict(original)
            if record["id"] in existing_post_ids:
                continue
            if not self.bypass_reservation and not candidate_reserver(record["id"], record):
                continue
            if self.mutate:
                self.mutate(record)
            if record_callback(record):
                break
        return self.records


def _collect(conn, run_id, collector, *, resume=False):
    return asyncio.run(engage.collect_exact(conn, run_id=run_id,
        preflight=RecordingPreflight([]), collector=collector, resume=resume))


@pytest.mark.parametrize("kwargs", [
    {"source_mode": "creator", "creator_handle": "maker"},
    {"source_mode": "url", "direct_post_url": "https://www.tiktok.com/@maker/video/123"},
    {"collection_policy": "refresh_known"},
])
def test_window_cannot_be_used_to_change_other_collection_modes(tmp_path, kwargs):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        with pytest.raises(ValueError, match="topic new_only LISTEN"):
            _run(conn, **kwargs)


def test_window_rejects_non_listen_workflow(tmp_path):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        with pytest.raises(ValueError, match="topic new_only LISTEN"):
            engage.create_run(conn, project="test", topic="coffee", requested_count=1,
                max_comments=1, max_pages=1, publication_window=WINDOW, workflow="audit")


def test_reservation_fence_and_newest_first_complete_export(tmp_path):
    records = [_record(1, "2026-08-23T23:59:59Z"), _record(2, "2026-08-24T00:00:00Z"),
        _record(3), _record(4, "2026-08-31T00:00:00Z"), _record(5, "2026-08-30T23:59:59Z")]
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn, count=2)
        status = _collect(conn, run_id, WindowCollector(records))
        assert status["evidence_ready"] == 2
        assert status["publication_window"] == WINDOW
        assert status["publication_window_exclusions"] == {
            "publication_time_unknown": 1, "published_before_window": 1,
            "published_at_or_after_window_end": 1,
        }
        assert conn.execute("SELECT COUNT(*) FROM engage_tiktok_posts").fetchone()[0] == 2
        target = tmp_path / "posts_evidence.jsonl"
        assert engage.export_listen_evidence(conn, run_id, target) == 2
        exported = [json.loads(line) for line in target.read_text().splitlines()]
        assert [item["post_id"] for item in exported] == ["5", "2"]
        assert all(item["evidence_packet"]["published_at"].endswith(".000000Z") for item in exported)


@pytest.mark.parametrize("changed", [None, "2025-01-01T00:00:00Z", "not a timestamp"])
def test_hydration_cannot_change_timestamp_and_enter_master(tmp_path, changed):
    def mutate(record):
        record["published_at"] = changed
    with closing(engage.connect_database(tmp_path / "test.sqlite", master_database=tmp_path / "master.sqlite")) as conn:
        run_id = _run(conn)
        collector = WindowCollector([_record(1, WINDOW["start"])], mutate=mutate)
        with pytest.raises(engage.CollectionIncompleteError):
            _collect(conn, run_id, collector)
        assert conn.execute("SELECT COUNT(*) FROM engage_tiktok_posts").fetchone()[0] == 0
        schema = engage._master_database_schema(conn)
        assert conn.execute(f"SELECT COUNT(*) FROM {schema}.tiktok_master_posts").fetchone()[0] == 0
        status = engage.run_status(conn, run_id)
        assert sum(status["publication_window_exclusions"].values()) == 1
        assert status["evidence_ready"] == 0


def test_checkpoint_fence_rejects_collector_bypassing_reservation(tmp_path):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn)
        with pytest.raises(engage.CollectionIncompleteError):
            _collect(conn, run_id, WindowCollector([_record(1)], bypass_reservation=True))
        assert engage.run_status(conn, run_id)["publication_window_exclusions"] == {"publication_time_unknown": 1}


def test_same_run_resume_keeps_original_window_and_exclusion_counts(tmp_path):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn, count=2)
        with pytest.raises(engage.CollectionIncompleteError):
            _collect(conn, run_id, WindowCollector([_record(1, WINDOW["start"]), _record(2)]))
        collector = WindowCollector([_record(1, WINDOW["start"]), _record(2), _record(3, "2026-08-25T00:00:00Z")])
        status = _collect(conn, run_id, collector, resume=True)
        assert collector.received_windows == [WINDOW]
        assert status["evidence_ready"] == 2
        assert status["publication_window_exclusions"] == {"publication_time_unknown": 1}


@pytest.mark.parametrize("replacement", [{}, normalize_publication_window("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z")])
def test_window_cannot_change_after_creation_even_before_browser(tmp_path, replacement):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn)
        conn.execute("UPDATE engage_tiktok_runs SET publication_window_json=?", (json.dumps(replacement),))
        conn.commit()
        calls = []
        with pytest.raises(engage.StageGateError, match="publication-window binding"):
            asyncio.run(engage.collect_exact(conn, run_id=run_id, preflight=RecordingPreflight(calls), collector=WindowCollector([])))
        assert not calls


def test_legacy_collector_rejected_before_browser_when_window_required(tmp_path):
    class Legacy:
        async def collect(self, **kwargs):
            raise AssertionError("must not run")
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn)
        calls = []
        with pytest.raises(engage.StageGateError, match="recency-capable"):
            asyncio.run(engage.collect_exact(conn, run_id=run_id, preflight=RecordingPreflight(calls), collector=Legacy()))
        assert not calls


def test_ordinary_music_audit_has_no_recency_requirement(tmp_path):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn, window={})
        result = _collect(conn, run_id, WindowCollector([_record(1)]))
        assert result["status"] == "collection_complete"
        assert "publication_window" not in result
        assert conn.execute("SELECT COUNT(*) FROM engage_tiktok_publication_exclusions").fetchone()[0] == 0


def test_recency_export_revalidates_timestamp_even_with_rehashed_packet(tmp_path):
    with closing(engage.connect_database(tmp_path / "test.sqlite")) as conn:
        run_id = _run(conn)
        _collect(conn, run_id, WindowCollector([_record(1, WINDOW["start"])]))
        packet = json.loads(conn.execute("SELECT evidence_json FROM engage_tiktok_posts").fetchone()[0])
        packet["published_at"] = "2025-01-01T00:00:00Z"
        conn.execute("UPDATE engage_tiktok_posts SET evidence_json=?,evidence_hash=?", (engage.canonical_json(packet), engage.json_hash(packet)))
        conn.commit()
        with pytest.raises(engage.StageGateError, match="outside the publication window"):
            engage.export_listen_evidence(conn, run_id, tmp_path / "evidence.jsonl")


def test_production_discovery_sorts_and_filters_before_hydration(tmp_path, monkeypatch):
    hydrated = []
    reserved = []
    exclusions = []
    class Integration:
        def __init__(self, **kwargs):
            self.last_search_diagnostics = {"stop_reason": "source_exhausted"}
        async def initialize_api(self, page):
            return True
        async def discover_search_videos(self, *args, **kwargs):
            return [_record(1, "2026-08-25T00:00:00Z"), _record(2, "2026-08-20T00:00:00Z"),
                _record(3), _record(4, "2026-08-30T00:00:00Z"), _record(5, WINDOW["end"])]
        async def hydrate_video_candidates(self, page, candidates):
            hydrated.extend(item["id"] for item in candidates)
        async def get_comments_for_multiple_videos(self, page, candidates, **kwargs):
            return {item["id"]: dict(item) for item in candidates}
    records, collector, page, browser = run_fake_production_collector(tmp_path, monkeypatch,
        Integration, requested_count=2, collector_options={
            "publication_window": WINDOW, "music_catalogs": (),
            "candidate_reserver": lambda post_id, raw: reserved.append(post_id) or True,
            "publication_exclusion_callback": lambda post_id, stage, decision: exclusions.append((post_id, stage, decision["reason"])),
        })
    assert hydrated == reserved == ["4", "1"]
    assert [item["id"] for item in records] == ["4", "1"]
    assert len(exclusions) == 3
    assert collector.last_diagnostics["publication_window"] == WINDOW
    assert browser.close_calls == 0 and page.closed


def test_cli_date_bounds_validate_without_touching_database(tmp_path, capsys):
    db = tmp_path / "must_not_exist.sqlite"
    assert engage.main(["--database", str(db), "music-audit", "--project", "test", "--topic", "coffee", "--posts", "1", "--published-after", "2026-08-24"]) == 1
    assert not db.exists()
    assert "both publication-window bounds" in capsys.readouterr().out


def test_resume_cli_accepts_verification_flags():
    args = engage.parse_args(["resume-collect", "--run-id", "run-1", "--published-after", WINDOW["start"], "--published-before", WINDOW["end"]])
    assert args.published_after == WINDOW["start"]


def test_resume_cli_mismatch_stops_before_master_attachment(tmp_path, monkeypatch, capsys):
    db = tmp_path / "test.sqlite"
    with closing(engage.connect_database(db)) as conn:
        run_id = _run(conn)
    monkeypatch.setattr(engage, "attach_master_database", lambda *args, **kwargs: pytest.fail("must not touch master"))
    assert engage.main(["--database", str(db), "resume-collect", "--run-id", run_id,
        "--published-after", "2026-08-01T00:00:00Z", "--published-before", WINDOW["end"]]) == 1
    assert "immutable saved window" in capsys.readouterr().out


def test_production_hydrated_date_change_is_replaced_only_by_eligible_post(tmp_path, monkeypatch):
    hydrated = []
    excluded = []
    class Integration:
        def __init__(self, **kwargs):
            self.last_search_diagnostics = {}
        async def initialize_api(self, page):
            return True
        async def discover_search_videos(self, *args, **kwargs):
            return [_record(1, "2026-08-25T00:00:00Z"), _record(2, "2026-08-30T00:00:00Z")]
        async def hydrate_video_candidates(self, page, candidates):
            hydrated.extend(item["id"] for item in candidates)
        async def get_comments_for_multiple_videos(self, page, candidates, **kwargs):
            return {item["id"]: {**item, "published_at": "2020-01-01T00:00:00Z" if item["id"] == "2" else item["published_at"]} for item in candidates}
    records, collector, _, _ = run_fake_production_collector(tmp_path, monkeypatch,
        Integration, requested_count=1, collector_options={
            "publication_window": WINDOW, "music_catalogs": (),
            "publication_exclusion_callback": lambda post_id, stage, decision: excluded.append((post_id, stage, decision["reason"])),
        })
    assert hydrated == ["2", "1"]
    assert [item["id"] for item in records] == ["1"]
    assert excluded == [("2", "checkpoint", "published_before_window")]
    assert collector.last_diagnostics["evidence_ready_candidates"] == 1
