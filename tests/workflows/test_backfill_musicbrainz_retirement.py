"""Offline coverage of new backfills and preserved pre-retirement runs."""

import asyncio
import copy
import json

import pytest

import engage_tiktok as engage
import music_backfill_tiktok as backfill


def candidate(post_id="123"):
    return {
        "post_id": post_id,
        "canonical_url": f"https://www.tiktok.com/@maker/video/{post_id}",
        "creator_handle": "maker",
        "content_type": "video",
        "base_snapshot_id": f"snapshot-{post_id}",
        "base_evidence_hash": "a" * 64,
        "base_observed_at": "2026-01-01T00:00:00+00:00",
    }


def forbid_provider_activity(*args, **kwargs):
    raise AssertionError("retired MusicBrainz must not request, reserve, or defer")


@pytest.fixture(autouse=True)
def offline_providers(monkeypatch):
    # Real metadata normalization and hashing are exercised below. Any provider
    # I/O would make these synthetic backfill tests fail rather than go online.
    monkeypatch.setattr(engage.MusicBrainzAdapter, "enrich", forbid_provider_activity)
    monkeypatch.setattr("requests.sessions.Session.request", forbid_provider_activity)


class MusicOnlyIntegration:
    async def refresh_video_candidates_from_html(self, page, records):
        assert page is None
        for record in records:
            record.update(
                metadata_refresh_ok=True,
                music_metadata_status="available",
                music_id="known-song",
                music_title="Known Song",
                music_author="Known Artist",
            )
        return {"attempted": len(records), "hydrated": len(records), "failed": 0}


@pytest.mark.parametrize("field,value", [
    ("configured_catalogs", []),
    ("configured_catalogs", ()),
    ("configured_catalogs_json", "[]"),
])
def test_frozen_empty_catalogs_never_fall_back_to_a_default(monkeypatch, field, value):
    monkeypatch.setattr(backfill, "DEFAULT_MUSIC_CATALOGS", ("musicbrainz",))
    assert backfill._run_catalogs({field: value}) == ()


def test_historical_catalog_list_is_read_without_modification():
    run = {"configured_catalogs_json": '["musicbrainz"]', "run_hash": "a" * 64}
    original = copy.deepcopy(run)
    assert backfill._run_catalogs(run) == ("musicbrainz",)
    assert run == original
    assert backfill._run_catalogs({}) == ()


def test_new_backfill_registration_freezes_empty_catalogs_in_temporary_master(tmp_path):
    database = tmp_path / "master.sqlite"
    args = backfill.parse_args([
        "--master-database", str(database), "run", "--project", "synthetic",
        "--topic", "synthetic", "--all-eligible",
    ])
    conn = backfill.master_state.connect_master(database)
    try:
        run_id = backfill._register_run(
            conn, args=args, scope_mode="topic", scope_value="synthetic",
            candidates=[], retry_statuses=backfill.DEFAULT_RETRY_STATUSES,
        )
        run = backfill.master_state.get_music_backfill_run(conn, run_id=run_id)
        assert run["configured_catalogs"] == []
        assert run["target_schema_version"] == backfill.TARGET_MUSIC_SCHEMA
        original_hash = run["run_hash"]
        status = backfill._status_packet(conn, run_id)
        assert status["configured_catalogs"] == []
        assert status["selected"] == 0
        assert backfill.master_state.get_music_backfill_run(
            conn, run_id=run_id
        )["run_hash"] == original_hash
    finally:
        conn.close()


def test_new_backfill_music_observation_has_no_musicbrainz_catalog():
    collector = backfill.MusicBackfillBrowserCollector()
    observations = asyncio.run(collector.collect_with_page(
        page=None, integration=MusicOnlyIntegration(), candidates=[candidate()],
        scope_mode="url", scope_value=candidate()["canonical_url"], max_pages=None,
        observation_callback=lambda observation: None,
        request_slot_reserver=forbid_provider_activity,
        provider_cooldown=forbid_provider_activity,
    ))
    music = observations[0].music_evidence
    assert observations[0].status == "completed"
    assert music["configured_catalogs"] == []
    assert "musicbrainz" not in music["catalogs"]
    assert music["platform_music"]["status"] == "available"
    assert backfill._sanitize_music_evidence(music, expected_catalogs=()) == music


def test_frozen_musicbrainz_backfill_resumes_only_pending_post_with_retired_outcome(monkeypatch):
    run = {
        "run_id": "historical", "project": "synthetic", "scope_mode": "post_ids",
        "scope_value": '["123","456"]', "configured_catalogs": ["musicbrainz"],
        "target_schema_version": backfill.TARGET_MUSIC_SCHEMA,
        "candidates": [candidate("123"), candidate("456")],
        "run_hash": "b" * 64,
    }
    historical_observation = {
        "post_id": "123", "status": "completed", "music_evidence_hash": "c" * 64,
        "music_evidence": {"historical_payload": "must remain unchanged"},
    }
    original_observation = copy.deepcopy(historical_observation)
    observations = [historical_observation]
    monkeypatch.setattr(backfill.master_state, "get_music_backfill_run", lambda *a, **k: run)
    monkeypatch.setattr(backfill, "_observation_rows", lambda *a, **k: list(observations))
    monkeypatch.setattr(backfill.master_state, "update_music_backfill_run",
                        lambda *a, **k: run.update(status=k.get("status", run.get("status"))))
    monkeypatch.setattr(backfill.master_state, "reserve_provider_request_slot", forbid_provider_activity)
    monkeypatch.setattr(backfill.master_state, "defer_provider_requests", forbid_provider_activity)
    monkeypatch.setattr(backfill.master_state, "record_music_backfill_observation",
                        lambda *a, **k: observations.append(dict(k)))

    def finalize(*args, **kwargs):
        run["status"] = "backfill_complete" if len(observations) == 2 else "backfill_incomplete"
        run["error"] = kwargs.get("error", "")

    monkeypatch.setattr(backfill.master_state, "finalize_music_backfill_run", finalize)

    class ResumeCollector(backfill.MusicBackfillBrowserCollector):
        async def collect(self, *, candidates, expected_account, account_callback, **kwargs):
            assert [item["post_id"] for item in candidates] == ["456"]
            assert kwargs["configured_catalogs"] == ("musicbrainz",)
            return await self.collect_with_page(
                page=None, integration=MusicOnlyIntegration(), candidates=candidates, **kwargs,
            )

    result = asyncio.run(backfill._execute_run(
        object(), run_id="historical", collector=ResumeCollector(),
    ))
    assert result["status"] == "backfill_complete"
    assert result["configured_catalogs"] == ["musicbrainz"]
    assert observations[0] == original_observation
    assert run["run_hash"] == "b" * 64
    music = observations[1]["music_evidence"]
    assert music["configured_catalogs"] == ["musicbrainz"]
    assert music["catalogs"]["musicbrainz"]["status"] == "unsupported"
    assert music["catalogs"]["musicbrainz"]["error"]["code"] == "provider_retired"
    assert backfill._sanitize_music_evidence(music, expected_catalogs=("musicbrainz",)) == music

    # A recorded retirement is terminal at the existing target version, so
    # default backfill selection does not repeatedly retry that observation.
    state = backfill.master_state._music_evidence_state(
        music, target_schema_version=backfill.TARGET_MUSIC_SCHEMA,
        retryable_statuses=backfill.DEFAULT_RETRY_STATUSES,
    )
    assert state["terminal_current"] is True
    assert state["base_status"] == "terminal"
    assert json.loads(json.dumps(music))["music_evidence_hash"] == music["music_evidence_hash"]
