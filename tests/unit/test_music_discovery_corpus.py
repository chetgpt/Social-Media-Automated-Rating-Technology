"""End-to-end offline corpus/CLI tests with synthetic local workflow evidence."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest

import music_discovery as discovery
import music_discovery_corpus as corpus
import music_discovery_sources as sources


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline discovery attempted network or subprocess")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _project(tmp_path, count=3, name="source.sqlite"):
    path = tmp_path / name
    now = datetime.now(timezone.utc)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE engage_tiktok_runs (run_id TEXT PRIMARY KEY, project TEXT, topic TEXT)")
        db.execute("CREATE TABLE engage_tiktok_posts (run_id TEXT,post_id TEXT,evidence_json TEXT,evidence_ready INTEGER,status TEXT,evidence_hash TEXT)")
        db.execute("INSERT INTO engage_tiktok_runs VALUES ('run_a','pilot','music')")
        for index in range(count):
            post_id = str(7000000000000000000 + index)
            evidence = {"post_id": post_id, "platform": "tiktok", "creator": "fixture_act",
                        "creator_identity": {"id": "123"}, "url": f"https://www.tiktok.com/@fixture_act/video/{post_id}",
                        "evidence_ready": True, "caption": "My original song, released today",
                        "caption_status": "available", "transcript_status": "unavailable", "comments": [],
                        "observed_at": now.isoformat(), "published_at": (now-timedelta(days=1)).isoformat(),
                        "metrics": {"views": 100, "followers": 0}, "metric_availability": {"views": "available", "followers": "unavailable"}}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            db.execute("INSERT INTO engage_tiktok_posts VALUES ('run_a',?,?,1,'collected',?)", (post_id, json.dumps(evidence), digest))
    return path


def _run(tmp_path, database, **filters):
    run = discovery.init_run(artists="all", output_root=tmp_path / "outputs")
    run_dir = Path(run["run_dir"])
    corpus.create_corpus(run_dir, [database], **filters)
    return run_dir


def _dossier(run_dir, post_id):
    observation = corpus.post_packet(run_dir, post_id)["observations"][0]
    payload = discovery.candidate_template(tiktok=True)
    payload.update(name="Fixture Act", artist_type="solo", primary_profile_url="https://www.tiktok.com/@fixture_act")
    payload["discovery_source"] = {"url": observation["projection"]["url"], "observed_at": observation["observed_at"], "note": "Saved TikTok source"}
    payload["tiktok_evidence"] = [{"post_id": post_id, "observation_id": observation["observation_id"], "role": "uploader_performer", "summary": "Fixture attributes performance to uploader"}]
    payload["identity_links"]["stable_tiktok_ids"] = ["123"]
    return payload


def test_all_scan_is_not_capped_by_artist_target_or_review_batch(tmp_path):
    source = _project(tmp_path, 65)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    run_dir = _run(tmp_path, source)
    status = discovery.status_run(run_dir)
    assert status["requested_artists"] == "all"
    assert status["tiktok_corpus"]["selected_posts"] == 65
    assert status["tiktok_corpus"]["pending_posts"] == 65
    batch = corpus.export_review(run_dir, posts=20)
    assert batch["batch_posts"] == 20
    assert batch["corpus"]["pending_posts"] == 65
    assert discovery.status_run(run_dir)["status"] == "research_incomplete"
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_completed_source_is_frozen_and_does_not_reread_new_source_rows(tmp_path):
    source = _project(tmp_path)
    run_dir = _run(tmp_path, source)
    source.unlink()  # Synthetic fixture only; frozen corpus is self-contained.
    assert corpus.resume_source(run_dir)["selected_posts"] == 3
    assert corpus.export_review(run_dir)["batch_posts"] == 3
    assert corpus.validate_corpus(run_dir)["selected_posts"] == 3


def test_explicit_multiple_databases_deduplicate_posts_and_observations(tmp_path):
    source = _project(tmp_path)
    copy = tmp_path / "restored.sqlite"
    copy.write_bytes(source.read_bytes())
    run_dir = Path(discovery.init_run(artists="all", output_root=tmp_path / "out")["run_dir"])
    summary = corpus.create_corpus(run_dir, [source, copy])
    assert summary["source_record_count"] == 6
    assert summary["selected_posts"] == 3
    packet = corpus.post_packet(run_dir, "7000000000000000000")
    assert len(packet["observations"]) == 1
    assert len(packet["references"]) == 2


def test_record_source_links_aliases_and_pending_classifications(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    post_id = "7000000000000000000"
    payload = _dossier(run_dir, post_id)
    result = discovery.record_candidate(run_dir, payload)
    assert result["tiktok_linked_artists"] == 1
    assert result["web_only_artists"] == 0
    assessment = result["candidates"][0]["artist_classification"]
    assert assessment["career_stage"]["label"] == "uncertain"
    assert assessment["momentum"]["label"] == "uncertain"
    assert assessment["observation_problems"] == []  # Unavailable None metrics must not poison observations.
    corpus.review_post(run_dir, post_id=post_id, disposition="reviewed", note="Artist identified; career context pending")
    result = discovery.report_run(run_dir)
    assert result["status"] == "research_complete_with_gaps"
    assert result["qualified_count"] == 0
    assert discovery.validate_run(run_dir)["validated"]
    alias = deepcopy(payload)
    alias["primary_profile_url"] = "https://www.youtube.com/@fixture_act"
    with pytest.raises(ValueError, match="overlaps"):
        discovery.record_candidate(run_dir, alias)


def test_made_up_source_links_rejected_before_writing(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    payload = _dossier(run_dir, "7000000000000000000")
    payload["tiktok_evidence"][0]["observation_id"] = "invented"
    before = (run_dir / "discovery.sqlite").read_bytes()
    with pytest.raises(ValueError, match="frozen TikTok observation"):
        discovery.record_candidate(run_dir, payload)
    assert (run_dir / "discovery.sqlite").read_bytes() == before


def test_review_cannot_claim_artist_without_linked_dossier(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    with pytest.raises(ValueError, match="record at least one"):
        corpus.review_post(run_dir, post_id="7000000000000000000", disposition="reviewed", note="Done")
    summary = corpus.review_post(run_dir, post_id="7000000000000000000", disposition="unresolved", note="Performer is mentioned but identity unresolved")
    assert summary["unresolved_posts"] == 1
    assert summary["pending_posts"] == 0
    assert discovery.status_run(run_dir)["status"] == "research_complete_with_gaps"
    assert corpus.post_packet(run_dir, "7000000000000000000")["review"]["note"] == "Performer is mentioned but identity unresolved"
    snapshot = discovery.report_run(run_dir)
    assert snapshot["post_review_gaps"][0]["post_id"] == "7000000000000000000"
    assert "Performer is mentioned but identity unresolved" in (run_dir / "shortlist.md").read_text(encoding="utf-8")
    assert discovery.validate_run(run_dir)["validated"]


def test_brand_mentions_do_not_become_artist_audience_metrics(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    payload = _dossier(run_dir, "7000000000000000000")
    payload["tiktok_evidence"][0]["role"] = "mentioned_performer"
    assert corpus.linked_observations(run_dir, payload) == []


def test_failed_scan_rolls_back_partial_source_and_resume_is_same_run(tmp_path, monkeypatch):
    source = _project(tmp_path)
    run_dir = Path(discovery.init_run(artists="all", output_root=tmp_path / "out")["run_dir"])
    real = sources.iter_tiktok_records
    def failing(*args, **kwargs):
        generator = real(*args, **kwargs)
        try:
            yield next(generator)
            raise ValueError("simulated read failure")
        finally:
            generator.close()
    monkeypatch.setattr(sources, "iter_tiktok_records", failing)
    with pytest.raises(ValueError):
        corpus.create_corpus(run_dir, [source])
    summary = corpus.corpus_summary(run_dir)
    assert summary["source_record_count"] == 0
    assert summary["completed_databases"] == []
    assert summary["scan_status"] == "source_scan_incomplete"
    monkeypatch.setattr(sources, "iter_tiktok_records", real)
    assert corpus.resume_source(run_dir)["selected_posts"] == 3


def test_empty_filtered_scope_is_not_artist_discovery_success(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path), projects=["not_present"])
    assert discovery.status_run(run_dir)["status"] == "no_saved_posts"


def test_validation_is_offline_and_detects_tampered_corpus(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    discovery.report_run(run_dir)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in run_dir.iterdir() if p.is_file()}
    assert discovery.validate_run(run_dir)["validated"]
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in run_dir.iterdir() if p.is_file()} == before
    with closing(sqlite3.connect(run_dir / corpus.FILENAME)) as db, db:
        db.execute("UPDATE source_records SET sha256='tampered'")
    with pytest.raises(ValueError, match="integrity"):
        discovery.validate_run(run_dir)


def test_cli_from_tiktok_and_export_review(tmp_path, capsys):
    source = _project(tmp_path, 1)
    assert discovery.main(["from-tiktok", "--database", str(source), "--output-root", str(tmp_path / "out")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["tiktok_corpus"]["selected_posts"] == 1
    assert discovery.main(["export-review", "--run-dir", result["run_dir"], "--posts", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["batch_posts"] == 1


def _qualify(payload):
    url = payload["primary_profile_url"]
    for field, status in (("indonesia", "supported"), ("original_music", "supported"), ("recognition", "under_recognized")):
        payload[field] = {"status": status, "summary": "Synthetic cited assessment", "sources": [url]}
    payload["works"] = [{"title": "Fixture song", "url": payload["discovery_source"]["url"],
                         "published_at": (datetime.now(timezone.utc)-timedelta(days=1)).isoformat(), "kind": "original"}]
    payload["listening_review"] = {"status": "reviewed", "reviewer": "Fixture reviewer", "notes": "Synthetic listening assessment", "sources": [url]}
    return payload


def _known_classifications(payload):
    today = datetime.now(timezone.utc).date().isoformat()
    for axis, claim in (("career_stage", "active"), ("momentum", "not_established")):
        payload["classification"][axis].update(claim=claim, assessed_on=today,
                applies_through=today, rationale="Synthetic cited assessment", sources=[payload["primary_profile_url"]])
    return payload


def test_reviewed_post_cannot_lose_last_resolved_dossier_link(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    post_id = "7000000000000000000"
    payload = _dossier(run_dir, post_id)
    discovery.record_candidate(run_dir, payload)
    corpus.review_post(run_dir, post_id=post_id, disposition="reviewed", note="Source attribution assessed")
    before = (run_dir / "discovery.sqlite").read_bytes()
    payload["tiktok_evidence"] = []
    with pytest.raises(ValueError, match="current resolved artist link"):
        discovery.record_candidate(run_dir, payload)
    assert (run_dir / "discovery.sqlite").read_bytes() == before
    corpus.review_post(run_dir, post_id=post_id, disposition="unresolved", note="Earlier attribution needs revision")
    result = discovery.record_candidate(run_dir, payload)
    assert result["status"] == "research_complete_with_gaps"
    assert result["tiktok_corpus"]["unresolved_posts"] == 1
    discovery.report_run(run_dir)
    assert discovery.validate_run(run_dir)["validated"]


def test_unresolved_artist_role_cannot_qualify_or_hide_behind_not_artist(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    post_id = "7000000000000000000"
    payload = _qualify(_dossier(run_dir, post_id))
    payload["tiktok_evidence"][0]["role"] = "unresolved"
    result = discovery.record_candidate(run_dir, payload)
    assert result["qualified_count"] == 0
    assert "resolved_tiktok_artist_role" in result["candidates"][0]["missing_criteria"]
    with pytest.raises(ValueError, match="record at least one"):
        corpus.review_post(run_dir, post_id=post_id, disposition="reviewed", note="Unresolved performer")
    with pytest.raises(ValueError, match="already has artist links"):
        corpus.review_post(run_dir, post_id=post_id, disposition="not_artist", note="No act")


def test_not_artist_checkpoint_must_be_reopened_before_adding_artist_link(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    post_id = "7000000000000000000"
    corpus.review_post(run_dir, post_id=post_id, disposition="not_artist", note="Initial assessment found no act")
    with pytest.raises(ValueError, match="change its review to unresolved"):
        discovery.record_candidate(run_dir, _dossier(run_dir, post_id))
    corpus.review_post(run_dir, post_id=post_id, disposition="unresolved", note="Revisit a possible artist lead")
    assert discovery.record_candidate(run_dir, _dossier(run_dir, post_id))["candidate_count"] == 1


def test_qualified_listening_count_uses_same_origin_as_qualified_count(tmp_path):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    payload = _qualify(_dossier(run_dir, "7000000000000000000"))
    discovery.record_candidate(run_dir, payload)
    external = deepcopy(payload)
    external.update(name="Web-only Act", primary_profile_url="https://www.youtube.com/@web_only_fixture",
                    tiktok_evidence=[], identity_links={"aliases": [], "profile_urls": [], "stable_tiktok_ids": []})
    result = discovery.record_candidate(run_dir, external)
    assert result["qualified_count"] == 1
    assert result["qualified_listening_reviewed_count"] == 1
    assert result["web_only_qualified_count"] == 1
    assert result["web_only_listening_reviewed_count"] == 1


@pytest.mark.parametrize("gap", ["original_music", "dates", "both", "none"])
def test_all_completion_discloses_original_and_date_uncertainty(tmp_path, gap):
    run_dir = _run(tmp_path, _project(tmp_path, 1))
    post_id = "7000000000000000000"
    payload = _known_classifications(_qualify(_dossier(run_dir, post_id)))
    # Known old work is valid coverage information, even if it doesn't qualify
    # for the separate recent-activity shortlist.
    payload["works"][0]["published_at"] = "2020-01-01"
    if gap in ("original_music", "both"):
        payload["original_music"] = {"status": "uncertain", "summary": "Not established", "sources": []}
    if gap in ("dates", "both"):
        payload["works"] = []
    discovery.record_candidate(run_dir, payload)
    corpus.review_post(run_dir, post_id=post_id, disposition="reviewed", note="Saved evidence reviewed")
    result = discovery.status_run(run_dir)
    assert result["candidates"][0]["artist_classification"]["career_stage"]["label"] == "active"
    assert result["status"] == ("research_complete" if gap == "none" else "research_complete_with_gaps")
    assert result["qualified_count"] == 0


def _live_run(tmp_path):
    return Path(discovery.init_run(artists="all", input_mode="live_search", output_root=tmp_path / "live")["run_dir"])


def _origin(job="q001"):
    return {"kind": "live_search", "query": "music", "project": "pilot", "run_id": "run_a",
            "handoff": "fixture_handoff.json", "query_job_id": job}


def test_live_v2_brief_preserves_old_v1_capability_contract(tmp_path):
    live = discovery.status_run(_live_run(tmp_path))
    legacy = discovery.init_run(artists=10, output_root=tmp_path / "legacy")
    assert live["live_collectors_enabled"] is True
    assert live["brief"]["schema"] == "music-discovery-brief-v2"
    assert live["input_mode"] == "live_search"
    assert live["status"] == "live_collection_pending"
    assert legacy["brief"]["schema"] == "music-discovery-brief-v1"
    assert legacy["live_collectors_enabled"] is False


def test_live_append_is_idempotent_readonly_and_source_bound(tmp_path):
    source = _project(tmp_path, 1)
    before = source.read_bytes()
    run_dir = _live_run(tmp_path)
    first = corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin=_origin())
    assert first["selected_posts"] == 1
    assert corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin=_origin()) == first
    assert source.read_bytes() == before
    packet = corpus.post_packet(run_dir, "7000000000000000000")
    assert packet["discovery_origins"] == [_origin()]
    assert corpus.validate_corpus(run_dir)["selected_posts"] == 1
    with pytest.raises(ValueError, match="different evidence batch"):
        corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin={**_origin(), "query": "wrong"})


def test_new_live_observations_reopen_review_and_preserve_old_notes(tmp_path):
    source = _project(tmp_path, 1)
    later = _project(tmp_path, 1, "later.sqlite")
    run_dir = _live_run(tmp_path)
    post_id = "7000000000000000000"
    corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin=_origin())
    corpus.review_post(run_dir, post_id=post_id, disposition="unresolved", note="Earlier identity lead")
    result = corpus.append_source_batch(run_dir, [later], run_ids=["run_a"], origin=_origin("q002"))
    assert result["pending_posts"] == 1
    assert result["reviewed_posts"] == 0
    packet = corpus.post_packet(run_dir, post_id)
    assert len(packet["observations"]) == 2
    assert packet["review"] is None
    assert packet["review_history"][0]["note"] == "Earlier identity lead"
    assert packet["review_history"][0]["reason"] == "new_live_evidence"
    assert len(packet["discovery_origins"]) == 2


def test_copied_live_observation_adds_provenance_without_reopening(tmp_path):
    source = _project(tmp_path, 1)
    copied = tmp_path / "copy.sqlite"
    copied.write_bytes(source.read_bytes())
    run_dir = _live_run(tmp_path)
    post_id = "7000000000000000000"
    corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin=_origin())
    corpus.review_post(run_dir, post_id=post_id, disposition="unresolved", note="Same real measurement")
    result = corpus.append_source_batch(run_dir, [copied], run_ids=["run_a"], origin=_origin("q002"))
    assert result["pending_posts"] == 0
    packet = corpus.post_packet(run_dir, post_id)
    assert len(packet["observations"]) == 1
    assert len(packet["references"]) == 2
    assert packet["review"]["note"] == "Same real measurement"


def test_live_append_cannot_change_legacy_frozen_source_scope(tmp_path):
    source = _project(tmp_path, 1)
    run_dir = _run(tmp_path, source)
    before = (run_dir / corpus.FILENAME).read_bytes()
    with pytest.raises(ValueError, match="saved-data corpus is frozen"):
        corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin=_origin())
    assert (run_dir / corpus.FILENAME).read_bytes() == before


def test_live_append_rejects_wrong_query_without_importing_rows(tmp_path):
    source = _project(tmp_path, 1)
    run_dir = _live_run(tmp_path)
    with pytest.raises(ValueError, match="exact query, project and run"):
        corpus.append_source_batch(run_dir, [source], run_ids=["run_a"], origin={**_origin(), "query": "not_music"})
    assert corpus.corpus_summary(run_dir)["selected_posts"] == 0
