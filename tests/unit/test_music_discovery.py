"""Offline acceptance tests for the separate MUSIC DISCOVERY research mode.

All persistent state is created under pytest's temporary directory.  These
tests never use the workspace's collection/master databases or a browser.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest

import music_discovery as discovery


PROFILE = "https://www.youtube.com/@pelagisenja"
WORK = "https://www.youtube.com/watch?v=abcdefghijk"
SOURCE = "https://pelagisenja.example/about"


@pytest.fixture(autouse=True)
def no_network_or_browser_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("MUSIC DISCOVERY's offline helper attempted network/process access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def _candidate(name="Pelagi Senja", profile=PROFILE):
    payload = discovery.candidate_template()
    payload.update(
        {
            "name": name,
            "artist_type": "band",
            "primary_profile_url": profile,
            "discovery_source": {
                "url": PROFILE,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "note": "Found an independently published original performance.",
            },
            "indonesia": {
                "status": "supported",
                "summary": "Artist biography describes this as a Bandung band.",
                "sources": [SOURCE],
            },
            "original_music": {
                "status": "supported",
                "summary": "The artist credits the original composition to the band.",
                "sources": [WORK],
            },
            "recognition": {
                "status": "under_recognized",
                "summary": "A provisional researcher assessment of limited documented exposure.",
                "sources": [PROFILE],
            },
            "works": [
                {
                    "title": "Awan Pertama",
                    "url": WORK,
                    "published_at": (
                        datetime.now(timezone.utc) - timedelta(days=1)
                    ).isoformat(),
                    "kind": "original",
                }
            ],
            "observations": [],
            "listening_review": {
                "status": "not_reviewed",
                "reviewer": "",
                "notes": "",
                "sources": [],
            },
            "spotify_presence": "present",
            "rationale": "Independently discovered candidate; listening review remains pending.",
            "caveats": ["Researcher claims require external source checking."],
        }
    )
    return payload


def _snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in Path(root).rglob("*")
        if path.is_file()
    }


def _new_run(tmp_path, artists=2, **kwargs):
    result = discovery.init_run(
        artists=artists, output_root=tmp_path / "discovery_runs", **kwargs
    )
    return Path(result["run_dir"]), result


def test_template_does_not_claim_verified_research_or_listening():
    payload = discovery.candidate_template()
    assert payload["indonesia"]["status"] == "uncertain"
    assert payload["original_music"]["status"] == "uncertain"
    assert payload["recognition"]["status"] == "uncertain"
    assert payload["listening_review"]["status"] == "not_reviewed"
    assert payload["spotify_presence"] == "unknown"


def test_template_instances_do_not_share_mutable_evidence():
    first = discovery.candidate_template()
    first["works"].append({"title": "Only in the first template"})
    assert discovery.candidate_template()["works"] == []


def test_template_claims_have_independent_source_lists():
    payload = discovery.candidate_template()
    payload["indonesia"]["sources"].append(SOURCE)
    assert payload["original_music"]["sources"] == []
    assert payload["recognition"]["sources"] == []


def test_spotify_presence_is_allowed_for_independently_discovered_artists():
    payload = discovery.normalize_candidate(_candidate())
    assert payload["spotify_presence"] == "present"
    assert payload["primary_profile_url"] == PROFILE


@pytest.mark.parametrize(
    "spotify_url",
    [
        "https://open.spotify.com/playlist/1234",
        "https://open.spotify.com/artist/1234",
        "https://www.spotify.com/id/",
        "https://spotify.link/1234",
        "https://spoti.fi/1234",
    ],
)
@pytest.mark.parametrize("field", ["primary_profile_url", "discovery_source"])
def test_spotify_cannot_be_disguised_as_an_independent_seed(spotify_url, field):
    payload = _candidate()
    if field == "discovery_source":
        payload[field]["url"] = spotify_url
    else:
        payload[field] = spotify_url
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


def test_youtube_identity_query_is_preserved():
    payload = discovery.normalize_candidate(_candidate())
    assert payload["works"][0]["url"] == WORK
    assert payload["original_music"]["sources"] == [WORK]


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://example.com/artist",
        "https://user:password@example.com/artist",
        "https://127.0.0.1/artist",
        "https://localhost/artist",
        "https://10.0.0.8/artist",
        "https://example.com/song?access_token=private-value",
        "https://example.com/song?signature=private-value",
    ],
)
def test_candidate_rejects_nonpublic_or_credential_bearing_urls(unsafe_url):
    payload = _candidate()
    payload["works"][0]["url"] = unsafe_url
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


@pytest.mark.parametrize("claim", ["indonesia", "original_music", "recognition"])
@pytest.mark.parametrize("missing", ["summary", "sources"])
def test_affirmative_claims_require_a_source_and_explanation(claim, missing):
    payload = _candidate()
    payload[claim][missing] = [] if missing == "sources" else ""
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


@pytest.mark.parametrize("missing", ["reviewer", "notes", "sources"])
def test_listening_claim_requires_an_actual_attributed_review(missing):
    payload = _candidate()
    payload["listening_review"] = {
        "status": "reviewed",
        "reviewer": "Human scout",
        "notes": "Listened to the linked performance; provisional musical assessment.",
        "sources": [WORK],
    }
    payload["listening_review"][missing] = [] if missing == "sources" else ""
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


@pytest.mark.parametrize("published_at", ["not-a-date", "2026-02-30", "2026-08-30T12:30:00"])
def test_invalid_or_timezone_ambiguous_timestamps_are_not_accepted(published_at):
    payload = _candidate()
    payload["works"][0]["published_at"] = published_at
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_observed_metrics_require_finite_nonnegative_numbers(value):
    payload = _candidate()
    payload["observations"] = [
        {
            "url": WORK,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "metrics": {"views": value},
        }
    ]
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


@pytest.mark.parametrize("artists", [0, -1, True, 1.5])
def test_invalid_artist_target_does_not_create_a_run(tmp_path, artists):
    with pytest.raises(ValueError):
        _new_run(tmp_path, artists=artists)
    assert not any(tmp_path.rglob("*.sqlite"))


def test_run_names_are_semantic_unique_and_outputs_are_isolated(tmp_path):
    first_dir, _ = _new_run(tmp_path, label="Bandung Indie", artists=3)
    second_dir, _ = _new_run(tmp_path, label="Bandung Indie", artists=3)
    assert first_dir != second_dir
    assert first_dir.parent == tmp_path / "discovery_runs"
    assert "music_discovery" in first_dir.name
    assert "bandung_indie" in first_dir.name
    assert (first_dir / "brief.json").is_file()
    assert (first_dir / "discovery.sqlite").is_file()
    assert not (tmp_path / "comments_data").exists()


def test_overlong_output_location_fails_before_creating_database(tmp_path):
    root = tmp_path / ("a" * 100) / ("b" * 100)
    with pytest.raises(ValueError):
        discovery.init_run(artists=1, output_root=root)
    assert not any(tmp_path.rglob("*.sqlite"))


def test_mode_advertises_agent_led_not_implemented_live_collection(tmp_path):
    run_dir, _ = _new_run(tmp_path)
    status = discovery.status_run(run_dir)
    assert status["workflow"] == "music_discovery"
    assert status["mode"] == "agent_led_research"
    assert status["live_collectors_enabled"] is False


def test_status_and_validate_do_not_modify_any_run_file(tmp_path):
    run_dir, _ = _new_run(tmp_path)
    discovery.record_candidate(run_dir, _candidate())
    before = _snapshot(run_dir)
    discovery.status_run(run_dir)
    discovery.validate_run(run_dir)
    assert _snapshot(run_dir) == before


@pytest.mark.parametrize("command", ["status", "validate", "report"])
def test_arbitrary_database_is_not_treated_as_a_discovery_run(tmp_path, command):
    unrelated = tmp_path / "existing_collection"
    unrelated.mkdir()
    db = unrelated / "discovery.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE user_data (note TEXT)")
        connection.execute("INSERT INTO user_data VALUES ('preserve me')")
    before = _snapshot(unrelated)
    with pytest.raises(ValueError):
        getattr(discovery, f"{command}_run")(unrelated)
    assert _snapshot(unrelated) == before


def test_cli_init_and_status_emit_machine_readable_offline_results(tmp_path, capsys):
    assert discovery.main(
        [
            "init",
            "--artists",
            "2",
            "--label",
            "Bandung Indie",
            "--output-root",
            str(tmp_path / "discovery_runs"),
        ]
    ) == 0
    initialized = json.loads(capsys.readouterr().out)
    run_dir = Path(initialized["run_dir"])
    before = _snapshot(run_dir)
    assert discovery.main(["status", "--run-dir", str(run_dir)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["workflow"] == "music_discovery"
    assert status["live_collectors_enabled"] is False
    assert _snapshot(run_dir) == before


def test_cli_record_accepts_candidate_file_and_retains_source_identity(tmp_path, capsys):
    run_dir, _ = _new_run(tmp_path)
    payload_path = tmp_path / "candidate.json"
    payload_path.write_text(json.dumps(_candidate()), encoding="utf-8")
    assert discovery.main(
        ["record", "--run-dir", str(run_dir), "--file", str(payload_path)]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["recorded_candidate_id"]
    assert result["recorded_revision"] == 1


def test_qualification_counts_artists_not_works_or_revisions(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=2)
    payload = _candidate()
    second_work = deepcopy(payload["works"][0])
    second_work.update(title="Awan Kedua", url="https://www.youtube.com/watch?v=lmnopqrstuv")
    payload["works"].append(second_work)
    discovery.record_candidate(run_dir, payload)
    payload["rationale"] = "Additional research on the same artist, not a new artist."
    status = discovery.record_candidate(run_dir, payload)
    assert status["candidate_count"] == 1
    assert status["revision_count"] == 2
    assert status["qualified_count"] == 1
    assert status["status"] == "research_incomplete"
    assert status["candidates"][0]["metric_changes"] == []


def test_partial_and_full_targets_keep_listening_pending_explicit(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=2)
    discovery.record_candidate(run_dir, _candidate())
    partial = discovery.report_run(run_dir)
    assert partial["qualified_count"] == 1
    assert partial["requested_artists"] == 2
    assert partial["status"] == "research_incomplete"
    discovery.record_candidate(
        run_dir,
        _candidate(name="Lini Rasa", profile="https://www.youtube.com/@linirasa"),
    )
    full = discovery.report_run(run_dir)
    assert full["qualified_count"] == 2
    assert full["status"] == "research_target_met"
    assert full["listening_reviewed_count"] == 0
    assert full["qualified_listening_reviewed_count"] == 0
    assert all(not candidate["listening_reviewed"] for candidate in full["candidates"])
    markdown = Path(full["report_markdown"]).read_text(encoding="utf-8")
    assert "Listening review pending" in markdown
    assert "no musical-quality judgment" in markdown
    assert "did not search, listen, or collect platform data" in markdown
    assert discovery.validate_run(run_dir)["validated"] is True


def test_reviewed_candidate_does_not_imply_all_candidates_have_been_listened_to(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=2)
    payload = _candidate()
    payload["listening_review"] = {
        "status": "reviewed",
        "reviewer": "Human scout",
        "notes": "Listening notes for the linked original performance, not a metric-derived score.",
        "sources": [WORK],
    }
    discovery.record_candidate(run_dir, payload)
    status = discovery.record_candidate(
        run_dir,
        _candidate(name="Lini Rasa", profile="https://www.youtube.com/@linirasa"),
    )
    assert status["listening_reviewed_count"] == 1
    assert status["qualified_listening_reviewed_count"] == 1
    assert status["candidate_count"] == 2


def test_unreviewed_listening_field_cannot_carry_a_musical_judgment():
    payload = _candidate()
    payload["listening_review"]["notes"] = "Excellent voice inferred from many likes."
    with pytest.raises(ValueError):
        discovery.normalize_candidate(payload)


@pytest.mark.parametrize("date_case", ["unknown", "old", "future"])
def test_unknown_old_and_future_work_dates_do_not_count_as_fresh(tmp_path, date_case):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    dates = {
        "unknown": None,
        "old": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        "future": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    payload["works"][0]["published_at"] = dates[date_case]
    status = discovery.record_candidate(run_dir, payload)
    assert status["qualified_count"] == 0
    candidate = status["candidates"][0]
    assert candidate["freshness_evidence"] == []
    assert "recent_published_work" in candidate["missing_criteria"]


@pytest.mark.parametrize(
    "published_at,expected",
    [
        ("2026-08-23", False),
        ("2026-08-24", True),
        ("2026-08-31", True),
        ("2026-09-01", False),
        ("2026-08-24T11:59:59+00:00", False),
        ("2026-08-24T12:00:00+00:00", True),
        ("2026-08-31T12:00:00+00:00", True),
        ("2026-08-31T12:00:01+00:00", False),
    ],
)
def test_frozen_window_boundaries_and_date_precision_are_explicit(
    tmp_path, monkeypatch, published_at, expected
):
    anchor = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(discovery, "_now", lambda: anchor)
    run_dir, initial = _new_run(tmp_path, artists=1)
    payload = _candidate()
    payload["works"][0]["published_at"] = published_at
    status = discovery.record_candidate(run_dir, payload)
    assert status["qualified_count"] == int(expected)
    assert status["brief"]["freshness_window"] == initial["brief"]["freshness_window"]
    if expected:
        freshness = status["candidates"][0]["freshness_evidence"][0]
        assert freshness["precision"] == ("date" if len(published_at) == 10 else "timestamp")
        if len(published_at) == 10:
            assert freshness["comparison"] == "calendar_date_in_window"


def test_resuming_same_run_preserves_window_history_and_latest_claims(tmp_path, monkeypatch):
    anchor = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(discovery, "_now", lambda: anchor)
    run_dir, initial = _new_run(tmp_path, artists=1)
    payload = _candidate()
    payload["works"][0]["published_at"] = "2026-08-30T12:00:00+00:00"
    first = discovery.record_candidate(run_dir, payload)
    monkeypatch.setattr(discovery, "_now", lambda: anchor + timedelta(days=14))
    payload["name"] = "Pelagi Senja — corrected artist name"
    payload["indonesia"] = {"status": "uncertain", "summary": "Location requires rechecking.", "sources": []}
    second = discovery.record_candidate(run_dir, payload)
    assert first["recorded_candidate_id"] == second["recorded_candidate_id"]
    assert second["recorded_revision"] == 2
    assert second["candidate_count"] == 1
    assert second["qualified_count"] == 0
    assert second["brief"] == initial["brief"]
    with sqlite3.connect((run_dir / "discovery.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        history = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM candidate_revisions ORDER BY revision")]
    assert len(history) == 2
    assert history[0]["name"] == "Pelagi Senja"
    assert history[0]["indonesia"]["status"] == "supported"
    assert history[1]["indonesia"]["status"] == "uncertain"


def _observation(value, at="2026-08-29T12:00:00+00:00", url=WORK, metric="views"):
    return {"url": url, "observed_at": at, "metrics": {metric: value}}


def test_one_observation_does_not_create_growth_or_quality_scores(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    payload["observations"] = [_observation(1000000)]
    candidate = discovery.record_candidate(run_dir, payload)["candidates"][0]
    assert candidate["metric_changes"] == []
    assert candidate["listening_reviewed"] is False
    assert "quality_score" not in candidate
    assert "talent_score" not in candidate


def test_matching_url_metric_and_distinct_times_yield_real_changes_across_revisions(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    payload["observations"] = [_observation(100)]
    discovery.record_candidate(run_dir, payload)
    payload["observations"] = [_observation(160, at="2026-08-30T12:00:00+00:00")]
    changes = discovery.record_candidate(run_dir, payload)["candidates"][0]["metric_changes"]
    assert len(changes) == 1
    assert changes[0]["url"] == WORK
    assert changes[0]["metric"] == "views"
    assert changes[0]["absolute_change"] == 60
    assert changes[0]["percent_change"] == pytest.approx(60)
    assert changes[0]["observation_count"] == 2


@pytest.mark.parametrize("case", ["different_url", "different_metric", "same_timestamp"])
def test_unmatched_or_duplicate_observations_do_not_establish_growth(tmp_path, case):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    second = _observation(100, at="2026-08-30T12:00:00+00:00")
    if case == "different_url":
        second["url"] = "https://www.youtube.com/watch?v=lmnopqrstuv"
    elif case == "different_metric":
        second["metrics"] = {"likes": 100}
    else:
        second["observed_at"] = "2026-08-29T12:00:00+00:00"
    payload["observations"] = [_observation(100), second]
    candidate = discovery.record_candidate(run_dir, payload)["candidates"][0]
    assert candidate["metric_changes"] == []


def test_conflicting_same_time_metrics_are_not_silently_overwritten(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    payload["observations"] = [_observation(100), _observation(120)]
    candidate = discovery.record_candidate(run_dir, payload)["candidates"][0]
    assert candidate["metric_changes"] == [
        {"url": WORK, "metric": "views", "status": "conflicting_observations"}
    ]


def test_zero_baseline_is_not_reported_as_infinite_percentage_growth(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    payload["observations"] = [_observation(0), _observation(25, at="2026-08-30T12:00:00+00:00")]
    change = discovery.record_candidate(run_dir, payload)["candidates"][0]["metric_changes"][0]
    assert change["absolute_change"] == 25
    assert change["percent_change"] is None


def test_report_artifacts_stay_in_run_and_validate_without_rewriting(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=1)
    discovery.record_candidate(run_dir, _candidate())
    report = discovery.report_run(run_dir)
    assert Path(report["report_json"]).parent == run_dir
    assert Path(report["report_markdown"]).parent == run_dir
    before = _snapshot(run_dir)
    validation = discovery.validate_run(run_dir)
    assert validation["validation_result"] == "pass"
    assert validation["reports_present"] is True
    assert _snapshot(run_dir) == before


def test_modified_brief_cannot_silently_change_requested_artist_count(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=2)
    brief_path = run_dir / "brief.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    brief["requested_artists"] = 1
    brief_path.write_text(json.dumps(brief), encoding="utf-8")
    before = _snapshot(run_dir)
    with pytest.raises(ValueError):
        discovery.validate_run(run_dir)
    assert _snapshot(run_dir) == before


def test_report_after_revision_is_detected_as_stale_until_regenerated(tmp_path):
    run_dir, _ = _new_run(tmp_path, artists=1)
    payload = _candidate()
    discovery.record_candidate(run_dir, payload)
    discovery.report_run(run_dir)
    payload["rationale"] = "A newly sourced explanation supersedes the first dossier."
    discovery.record_candidate(run_dir, payload)
    before = _snapshot(run_dir)
    with pytest.raises(ValueError):
        discovery.validate_run(run_dir)
    assert _snapshot(run_dir) == before
    discovery.report_run(run_dir)
    assert discovery.validate_run(run_dir)["validated"] is True
