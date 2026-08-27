import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import engage_tiktok as engage
from engage_tiktok import (
    CollectionIncompleteError,
    DEFAULT_MUSIC_CATALOGS,
    StageGateError,
    TikTokBrowserCollector,
    build_music_evidence,
    compact_ai_evidence_projection,
    connect_database,
    create_run,
    export_listen_evidence,
    normalize_direct_post_target,
    normalize_evidence,
    platform_music_observation,
    terminal_musicbrainz_result,
)
from tiktok_scraper.api_integration import TikTokAPIIntegration
from tiktok_scraper.music_enrichment import HTTPResponse
from tiktok_scraper.tt2dsp_resolution import TT2DSPResolver


def terminal_record(post_id="123", creator="maker"):
    return {
        "id": post_id,
        "url": f"https://www.tiktok.com/@{creator}/video/{post_id}",
        "username": creator,
        "content_type": "video",
        "caption": "A complete post",
        "view_count": 10,
        "music_id": "m1",
        "music_title": "Known Song",
        "music_author": "Known Artist",
        "music_duration_seconds": 180,
        "post_duration_seconds": 15,
        "music_metadata_status": "available",
        "comments": [],
        "ok": True,
        "complete": True,
        "exhausted": True,
        "limit_reached": False,
        "has_more": False,
        "source": "direct_api",
        "transcript_status": "unavailable",
    }


def test_tiktok_parser_separates_post_and_declared_music_duration():
    integration = TikTokAPIIntegration(
        enable_api=True,
        persist_session_secrets=False,
    )
    parsed = integration._extract_video_from_search_item(
        {
            "id": "123",
            "desc": "Post",
            "author": {"uniqueId": "maker"},
            "stats": {"playCount": 1},
            "video": {"duration": 15},
            "music": {
                "id": "music-1",
                "title": "Known Song",
                "authorName": "Known Artist",
                "duration": 180,
                "original": False,
                "playUrl": "https://signed.example/audio?token=secret",
                "coverLarge": "https://signed.example/art?token=secret",
            },
        }
    )

    assert parsed["duration_seconds"] == 15
    assert parsed["post_duration_seconds"] == 15
    assert parsed["music_duration_seconds"] == 180
    assert parsed["music_metadata_status"] == "available"
    packet, ready, _ = normalize_evidence(
        {**terminal_record(), **parsed},
        topic="known",
    )
    assert ready is True
    serialized = json.dumps(packet)
    assert "signed.example" not in serialized


def test_tiktok_parser_retains_safe_contained_track_declaration():
    integration = TikTokAPIIntegration(
        enable_api=True,
        persist_session_secrets=False,
    )
    parsed = integration._extract_video_from_search_item(
        {
            "id": "124",
            "author": {"uniqueId": "maker"},
            "video": {"duration": 15},
            "music": {
                "id": "outer-1",
                "title": "original sound - maker",
                "authorName": "maker",
                "original": True,
                "matchedSong": {
                    "id": "recording-1",
                    "title": "Contained Song",
                    "author": "Contained Artist",
                    "album": "Contained Album",
                    "durationMs": 209680,
                },
                "tt2dsp": {
                    "tt_to_dsp_song_infos": [
                        {
                            "song_id": "dsp-1",
                            "token": {"secret": "MUST_NOT_SURVIVE"},
                        }
                    ]
                },
            },
        }
    )

    assert parsed["music_title"] == "original sound - maker"
    assert parsed["music_contained_recording_status"] == "available"
    assert parsed["music_contained_recording_source"] == (
        "tiktok_item_music.matched_song"
    )
    assert parsed["music_contained_recording_title"] == "Contained Song"
    assert parsed["music_contained_recording_artist"] == "Contained Artist"
    assert parsed["music_contained_recording_duration_ms"] == 209680
    assert parsed["music_contained_recording_dsp_links"] == [
        {"song_id": "dsp-1"}
    ]
    serialized = json.dumps(parsed)
    assert "MUST_NOT_SURVIVE" not in serialized
    assert '"token"' not in serialized


def test_tiktok_dsp_linkage_is_partial_contained_track_status():
    parsed = TikTokAPIIntegration(
        enable_api=True,
        persist_session_secrets=False,
    )._extract_video_from_search_item(
        {
            "id": "125",
            "author": {"uniqueId": "maker"},
            "music": {
                "id": "outer-2",
                "title": "original sound - maker",
                "authorName": "maker",
                "original": True,
                "tt2dsp": {
                    "tt_to_dsp_song_infos": [
                        {
                            "meta_song_id": "meta-1",
                            "song_id": "song-1",
                            "platform": 1,
                            "button_type": 2,
                            "token": {"secret": "MUST_NOT_SURVIVE"},
                        }
                    ]
                },
            },
        }
    )

    assert parsed["music_contained_recording_status"] == "partial"
    assert parsed["music_contained_recording_source"] == (
        "tiktok_item_music.tt2dsp.tt_to_dsp_song_infos"
    )
    assert parsed["music_contained_recording_reason"] == (
        "contained_recording_linkage_returned_without_identity"
    )
    assert parsed["music_contained_recording_dsp_links"] == [
        {
            "meta_song_id": "meta-1",
            "song_id": "song-1",
            "platform": "1",
            "button_type": "2",
        }
    ]
    record = terminal_record("125")
    record.update(parsed)
    music = build_music_evidence(record, configured_catalogs=())
    assert music["platform_contained_recording"]["status"] == "partial"
    assert music["platform_contained_recording"]["dsp_links"] == (
        parsed["music_contained_recording_dsp_links"]
    )
    assert "MUST_NOT_SURVIVE" not in json.dumps(music)


def test_musicbrainz_uses_contained_track_instead_of_original_sound():
    captured = []

    class CapturingAdapter:
        def enrich(self, audio):
            captured.append(dict(audio))
            return terminal_musicbrainz_result(
                audio,
                status="not_found",
                reason="successful_empty_test_result",
            )

    record = terminal_record()
    record.update(
        {
            "music_title": "original sound - maker",
            "music_author": "maker",
            "music_is_original": True,
            "music_contained_recording_status": "available",
            "music_contained_recording_source": (
                "tiktok_item_music.matched_song"
            ),
            "music_contained_recording_id": "recording-1",
            "music_contained_recording_title": "Contained Song",
            "music_contained_recording_artist": "Contained Artist",
            "music_contained_recording_duration_ms": 209680,
        }
    )
    collector = TikTokBrowserCollector(
        musicbrainz_adapter=CapturingAdapter()
    )

    music = asyncio.run(
        collector._enrich_music(
            record,
            DEFAULT_MUSIC_CATALOGS,
            request_slot_reserver=lambda _provider, _interval: 0.0,
        )
    )

    assert captured == [
        {
            "music_id": "recording-1",
            "title": "Contained Song",
            "author": "Contained Artist",
            "is_original": False,
            "duration_ms": 209680,
            "isrc": "",
        }
    ]
    assert music["schema_version"] == "tiktok-music-evidence-v3"
    assert music["platform_contained_recording"]["status"] == "available"
    assert music["catalogs"]["musicbrainz"]["input_basis"] == (
        "platform_contained_recording"
    )


def test_tt2dsp_apple_resolution_feeds_musicbrainz_without_overwriting_tiktok():
    captured_audio = []
    reserved_providers = []

    class AppleTransport:
        def get(self, url, *, headers, timeout):
            del url, headers, timeout
            payload = {
                "resultCount": 1,
                "results": [
                    {
                        "kind": "song",
                        "trackId": 1107248959,
                        "trackName": "Hero (feat. Christina Perri)",
                        "artistName": "Cash Cash",
                        "collectionName": "Blood, Sweat & 3 Years",
                        "collectionId": 1107248683,
                        "trackTimeMillis": 197840,
                        "releaseDate": "2016-06-24T12:00:00Z",
                        "primaryGenreName": "Dance",
                        "trackExplicitness": "notExplicit",
                    }
                ],
            }
            return HTTPResponse(
                status_code=200,
                body=json.dumps(payload).encode("utf-8"),
                headers={},
            )

    class CapturingMusicBrainz:
        def enrich(self, audio):
            captured_audio.append(dict(audio))
            return terminal_musicbrainz_result(
                audio,
                status="not_found",
                reason="successful_empty_test_result",
            )

    record = terminal_record()
    record.update(
        {
            "music_title": "suara asli - Omg.Banget",
            "music_author": "Omg.Banget",
            "music_is_original": True,
            "music_contained_recording_status": "partial",
            "music_contained_recording_source": (
                "tiktok_item_music.tt2dsp.tt_to_dsp_song_infos"
            ),
            "music_contained_recording_dsp_links": [
                {
                    "meta_song_id": "6751368172487575554",
                    "song_id": "1107248959",
                    "platform": "1",
                },
                {
                    "meta_song_id": "6751368172487575554",
                    "song_id": "5g1BARk25uUJtEPSUwcjnU",
                    "platform": "3",
                },
            ],
            "music_contained_recording_reason": (
                "contained_recording_linkage_returned_without_identity"
            ),
        }
    )
    collector = TikTokBrowserCollector(
        musicbrainz_adapter=CapturingMusicBrainz(),
        tt2dsp_resolver=TT2DSPResolver(transport=AppleTransport()),
    )

    music = asyncio.run(
        collector._enrich_music(
            record,
            DEFAULT_MUSIC_CATALOGS,
            request_slot_reserver=lambda provider, _interval: (
                reserved_providers.append(provider) or 0.0
            ),
        )
    )

    assert reserved_providers == ["apple_itunes_lookup", "musicbrainz"]
    assert captured_audio == [
        {
            "music_id": "1107248959",
            "title": "Hero (feat. Christina Perri)",
            "author": "Cash Cash",
            "is_original": False,
            "duration_ms": 197840,
            "isrc": "",
        }
    ]
    assert music["platform_contained_recording"]["status"] == "partial"
    assert music["tt2dsp_resolution"]["status"] == "resolved"
    assert music["catalogs"]["musicbrainz"]["input_basis"] == (
        "tt2dsp_catalog_resolution"
    )
    assert music["identity_status"] == "catalog_correlated"


def test_normalized_evidence_retains_music_and_separate_durations():
    record = terminal_record()
    record["music_evidence"] = build_music_evidence(
        record,
        configured_catalogs=(),
    )

    packet, ready, issues = normalize_evidence(record, topic="known")

    assert ready is True
    assert issues == []
    assert packet["post_duration_ms"] == 15_000
    assert packet["music_evidence"]["platform_music"]["duration_ms"] == 180_000
    assert packet["music_evidence"]["platform_music"]["post_duration_ms"] == 15_000
    assert packet["music_evidence"]["platform_music"]["duration_comparison_allowed"] is True
    assert packet["music_evidence"]["platform_music"]["field_provenance"][
        "title"
    ] == {
        "status": "available",
        "source": "tiktok_item_music",
        "reason": "",
    }
    projection = compact_ai_evidence_projection(packet, evidence_hash="a" * 64)
    assert projection["music_evidence"] == packet["music_evidence"]


def test_unavailable_platform_music_uses_one_consistent_field_status():
    music = platform_music_observation(
        {"music_metadata_status": "unavailable"}
    )

    assert set(music["field_availability"].values()) == {"unavailable"}
    assert {
        value["status"] for value in music["field_provenance"].values()
    } == {"unavailable"}


def test_provider_failure_is_terminal_and_does_not_block_evidence():
    class BrokenAdapter:
        def enrich(self, audio):
            raise RuntimeError("provider unavailable")

    record = terminal_record()
    collector = TikTokBrowserCollector(musicbrainz_adapter=BrokenAdapter())
    music = asyncio.run(collector._enrich_music(record, DEFAULT_MUSIC_CATALOGS))
    record["music_evidence"] = music

    packet, ready, issues = normalize_evidence(record, topic="known")

    assert ready is True
    assert issues == []
    result = packet["music_evidence"]["catalogs"]["musicbrainz"]["result"]
    assert result["status"] == "provider_error"
    assert result["decision"]["reason"] == "adapter_exception"


def test_indonesian_original_sound_skips_musicbrainz_request_slot():
    record = terminal_record()
    record.update(
        {
            "music_title": "suara asli - Omg.Banget",
            "music_author": "Omg.Banget",
            "music_is_original": True,
        }
    )
    collector = TikTokBrowserCollector()

    def unexpected_request_slot(_provider, _interval):
        raise AssertionError("generic original sound must not reserve a provider slot")

    music = asyncio.run(
        collector._enrich_music(
            record,
            DEFAULT_MUSIC_CATALOGS,
            request_slot_reserver=unexpected_request_slot,
        )
    )

    result = music["catalogs"]["musicbrainz"]["result"]
    assert result["status"] == "unsupported"
    assert result["decision"]["reason"] == "generic_original_sound"


def test_generic_original_sound_remains_unsupported_after_provider_circuit_opens():
    record = terminal_record()
    record.update(
        {
            "music_title": "suara asli - BankBCA",
            "music_author": "BankBCA",
            "music_is_original": True,
        }
    )
    collector = TikTokBrowserCollector()
    collector._musicbrainz_circuit_open = True

    music = asyncio.run(
        collector.enrich_music_record(record, DEFAULT_MUSIC_CATALOGS)
    )

    catalog = music["catalogs"]["musicbrainz"]
    assert catalog["circuit_open"] is False
    assert catalog["status"] == "unsupported"
    assert catalog["result"]["decision"]["reason"] == "generic_original_sound"


@pytest.mark.parametrize(
    "bad",
    [
        "http://www.tiktok.com/@maker/video/123",
        "https://example.com/@maker/video/123",
        "https://www.tiktok.com/@maker",
        "https://www.tiktok.com/@maker/video/not-a-number",
    ],
)
def test_direct_url_normalizer_rejects_noncanonical_targets(bad):
    with pytest.raises(ValueError):
        normalize_direct_post_target(bad)


def test_direct_url_run_is_listen_only_and_persists_immutable_scope(tmp_path):
    conn = connect_database(tmp_path / "listen.sqlite")
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="ignored",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@Maker/video/123?x=1",
        )
        row = conn.execute(
            "SELECT * FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row["topic"] == ""
        assert row["direct_post_url"] == (
            "https://www.tiktok.com/@maker/video/123"
        )
        assert json.loads(row["music_catalogs_json"]) == ["musicbrainz"]
        with pytest.raises(ValueError, match="LISTEN-only"):
            create_run(
                conn,
                project="bad",
                topic="",
                requested_count=1,
                max_comments=10,
                max_pages=1,
                workflow="audit",
                source_mode="url",
                direct_post_url="https://www.tiktok.com/@maker/video/123",
            )
    finally:
        conn.close()


def test_cli_url_contract():
    parsed = engage.parse_args(
        [
            "collect",
            "--project",
            "listen_url",
            "--url",
            "https://www.tiktok.com/@maker/video/123",
            "--posts",
            "1",
            "--workflow",
            "listen",
        ]
    )
    assert parsed.url.endswith("/video/123")
    assert parsed.music_catalog is None


@pytest.mark.parametrize(
    "source_args",
    [
        ["--topic", "3D printing", "--posts", "5"],
        ["--creator", "@maker", "--posts", "5"],
        ["--creator", "@maker", "--all-posts"],
        [
            "--url",
            "https://www.tiktok.com/@maker/video/123",
            "--posts",
            "1",
        ],
    ],
)
def test_music_audit_parser_forces_listen_shadow(source_args):
    parsed = engage.parse_args(
        ["music-audit", "--project", "brand_music", *source_args]
    )

    assert parsed.command == "music-audit"
    assert parsed.workflow == "listen"
    assert parsed.mode == "shadow"


@pytest.mark.parametrize(
    "override",
    [
        ["--workflow", "engage"],
        ["--workflow", "audit"],
        ["--mode", "live"],
    ],
)
def test_music_audit_parser_rejects_workflow_and_mode_overrides(override):
    with pytest.raises(SystemExit) as exc:
        engage.parse_args(
            [
                "music-audit",
                "--project",
                "brand_music",
                "--topic",
                "3D printing",
                "--posts",
                "5",
                *override,
            ]
        )
    assert exc.value.code == 2


class _ReadyPreflight:
    async def ensure_ready(self):
        return {
            "reachable": True,
            "tiktok_authenticated": True,
            "observed_account": "collector",
            "profile": {"profile_directory": "Profile 7"},
        }


class _URLCollector:
    def __init__(self, *, known=False, transform=None, omit_music=False):
        self.known = known
        self.transform = transform
        self.omit_music = omit_music
        self.last_diagnostics = {}
        self.calls = []

    async def collect(
        self,
        *,
        topic,
        requested_count,
        max_comments,
        max_pages,
        record_callback,
        existing_post_ids=(),
        initial_evidence_ready_count=0,
        collection_policy="new_only",
        global_known_post_ids=(),
        current_run_post_ids=(),
        refresh_candidates=(),
        candidate_reserver=None,
        source_mode="topic",
        creator_handle="",
        direct_post_url="",
        music_catalogs=(),
    ):
        self.calls.append(
            {
                "source_mode": source_mode,
                "direct_post_url": direct_post_url,
                "music_catalogs": tuple(music_catalogs),
            }
        )
        if self.known:
            self.last_diagnostics = {
                "collection_stop_reason": "direct_post_globally_known"
            }
            return []
        record = terminal_record()
        record["direct_source_validation"] = {
            "required": True,
            "matched": True,
        }
        if self.transform is not None:
            record = self.transform(record)
        if self.omit_music:
            record["music_evidence"] = build_music_evidence(
                record,
                configured_catalogs=(),
            )
        else:
            platform_music = platform_music_observation(record)
            result = terminal_musicbrainz_result(
                platform_music,
                status="unsupported",
                reason="test_terminal",
            )
            record["music_evidence"] = build_music_evidence(
                record,
                configured_catalogs=music_catalogs,
                musicbrainz_result=result,
            )
        record_callback(record)
        return [record]


def test_collect_exact_passes_url_and_catalog_scope(tmp_path):
    conn = connect_database(tmp_path / "listen.sqlite")
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
        )
        collector = _URLCollector()
        result = asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=_ReadyPreflight(),
                collector=collector,
            )
        )
        assert result["status"] == "collection_complete"
        assert result["evidence_ready"] == 1
        assert collector.calls == [
            {
                "source_mode": "url",
                "direct_post_url": (
                    "https://www.tiktok.com/@maker/video/123"
                ),
                "music_catalogs": ("musicbrainz",),
            }
        ]
        updated_before = conn.execute(
            "SELECT updated_at FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        output = tmp_path / "listen-evidence.jsonl"
        assert export_listen_evidence(conn, run_id, output) == 1
        exported = json.loads(output.read_text(encoding="utf-8"))
        assert exported["schema_version"] == "tiktok-listen-evidence-export-v1"
        assert exported["post_id"] == "123"
        packet = exported["evidence_packet"]
        assert packet["caption"] == "A complete post"
        assert packet["music_evidence"]["platform_music"]["title"] == "Known Song"
        assert packet["comments"] == []
        assert conn.execute(
            "SELECT updated_at FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == updated_before
    finally:
        conn.close()


@pytest.mark.parametrize("collection_policy", ["new_only", "refresh_known"])
@pytest.mark.parametrize(
    "transform",
    [
        lambda record: {**record, "id": "999", "url": "https://www.tiktok.com/@maker/video/999"},
        lambda record: {**record, "username": "other"},
        lambda record: {**record, "content_type": "photo"},
    ],
)
def test_checkpoint_rejects_direct_url_scope_drift(
    tmp_path,
    collection_policy,
    transform,
):
    conn = connect_database(tmp_path / "listen.sqlite")
    refresh_candidate = {
        "post_id": "123",
        "url": "https://www.tiktok.com/@maker/video/123",
        "creator": "maker",
        "content_type": "video",
    }
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            collection_policy=collection_policy,
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
            refresh_post_ids=("123",) if collection_policy == "refresh_known" else (),
            refresh_candidates=(refresh_candidate,)
            if collection_policy == "refresh_known"
            else (),
        )
        with pytest.raises(StageGateError, match="Direct URL evidence"):
            asyncio.run(
                engage.collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=_ReadyPreflight(),
                    collector=_URLCollector(transform=transform),
                )
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_checkpoint_requires_terminal_frozen_music_catalog_result(tmp_path):
    conn = connect_database(tmp_path / "listen.sqlite")
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
        )
        with pytest.raises(CollectionIncompleteError):
            asyncio.run(
                engage.collect_exact(
                    conn,
                    run_id=run_id,
                    preflight=_ReadyPreflight(),
                    collector=_URLCollector(omit_music=True),
                )
            )
        row = conn.execute(
            "SELECT evidence_ready, evidence_json FROM engage_tiktok_posts "
            "WHERE run_id=? AND post_id='123'",
            (run_id,),
        ).fetchone()
        assert row["evidence_ready"] == 0
        packet = json.loads(row["evidence_json"])
        assert packet["evidence_ready"] is False
        assert "music_catalog_scope_mismatch" in packet["readiness_issues"]
        assert "music_catalog_not_terminal:musicbrainz" in packet[
            "readiness_issues"
        ]
    finally:
        conn.close()


def test_export_evidence_cli_does_not_attach_or_sync_master(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "listen.sqlite"
    conn = connect_database(database)
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
        )
        asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=_ReadyPreflight(),
                collector=_URLCollector(),
            )
        )
        updated_before = conn.execute(
            "SELECT updated_at FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    monkeypatch.setattr(
        engage,
        "attach_master_database",
        lambda *_args, **_kwargs: pytest.fail("export attached master DB"),
    )
    monkeypatch.setattr(
        engage,
        "_register_master_run_state",
        lambda *_args, **_kwargs: pytest.fail("export synchronized master DB"),
    )
    output = tmp_path / "export.jsonl"
    assert engage.main(
        [
            "--database",
            str(database),
            "export-evidence",
            "--run-id",
            run_id,
            "--file",
            str(output),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 1
    read_only = engage.sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert read_only.execute(
            "SELECT updated_at FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == updated_before
    finally:
        read_only.close()


def test_unknown_musicbrainz_result_fields_are_not_persisted():
    record = terminal_record()
    platform_music = platform_music_observation(record)
    result = terminal_musicbrainz_result(
        platform_music,
        status="unsupported",
        reason="test_terminal",
    )
    result["signed_audio_url"] = "https://signed.example/audio?token=secret"
    result.pop("enrichment_hash")
    result["enrichment_hash"] = engage.json_hash(result)

    music = build_music_evidence(
        record,
        configured_catalogs=("musicbrainz",),
        musicbrainz_result=result,
    )

    assert music["catalogs"] == {}
    assert "signed.example" not in json.dumps(music)


def test_export_rebuilds_music_projection_without_unknown_fields(tmp_path):
    conn = connect_database(tmp_path / "listen.sqlite")
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
        )
        asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=_ReadyPreflight(),
                collector=_URLCollector(),
            )
        )
        row = conn.execute(
            "SELECT evidence_json FROM engage_tiktok_posts "
            "WHERE run_id=? AND post_id='123'",
            (run_id,),
        ).fetchone()
        packet = json.loads(row["evidence_json"])
        music = packet["music_evidence"]
        music["signed_audio_url"] = (
            "https://signed.example/audio?token=SECRET"
        )
        music.pop("music_evidence_hash")
        music["music_evidence_hash"] = engage.json_hash(music)
        evidence_hash = engage.json_hash(packet)
        conn.execute(
            "UPDATE engage_tiktok_posts SET evidence_json=?, evidence_hash=? "
            "WHERE run_id=? AND post_id='123'",
            (engage.canonical_json(packet), evidence_hash, run_id),
        )
        conn.commit()

        output = tmp_path / "export.jsonl"
        assert export_listen_evidence(conn, run_id, output) == 1
        exported = output.read_text(encoding="utf-8")
        assert "SECRET" not in exported
        assert "signed_audio_url" not in exported
    finally:
        conn.close()


def test_export_rejects_database_and_master_artifact_targets(tmp_path):
    database = tmp_path / "listen.sqlite"
    master_database = tmp_path / "master.sqlite"
    conn = connect_database(database)
    try:
        run_id = create_run(
            conn,
            project="listen_url",
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            workflow="listen",
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
            master_database=str(master_database.resolve()),
        )
        asyncio.run(
            engage.collect_exact(
                conn,
                run_id=run_id,
                preflight=_ReadyPreflight(),
                collector=_URLCollector(),
            )
        )
        for protected in (
            database,
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
            master_database,
            Path(str(master_database) + "-wal"),
        ):
            with pytest.raises(StageGateError, match="cannot overwrite"):
                export_listen_evidence(conn, run_id, protected)
        assert not list(tmp_path.glob(".*.tmp"))
    finally:
        conn.close()


@pytest.mark.parametrize("collection_policy", ["new_only", "refresh_known"])
def test_production_direct_url_profile_fallback_is_exact_and_keeps_music(
    tmp_path, monkeypatch, collection_policy
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    designation = {
        "profile_directory": "Profile 7",
        "mode": "existing_profile_attach",
    }
    page = SimpleNamespace(closed=False)

    def is_closed():
        return page.closed

    async def close():
        page.closed = True

    async def goto(*_args, **_kwargs):
        return None

    page.is_closed = is_closed
    page.close = close
    page.goto = goto

    class Context:
        async def new_page(self):
            return page

    class Browser:
        contexts = [Context()]

    class Chromium:
        async def connect_over_cdp(self, url):
            return Browser()

    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Integration:
        def __init__(self, *, enable_api, persist_session_secrets):
            assert enable_api is True
            assert persist_session_secrets is False

        async def initialize_api(self, received_page):
            return True

        async def refresh_video_candidates_from_html(self, received_page, rows):
            assert len(rows) == 1
            rows[0]["metadata_refresh_ok"] = False
            return {"eligible": 1, "attempted": 1, "hydrated": 0, "failed": 1}

        async def get_comments_for_multiple_videos(self, received_page, rows, **kwargs):
            return {"123": terminal_record()}

        async def discover_search_videos(self, *args, **kwargs):
            raise AssertionError("direct URL must not search")

        async def discover_creator_profile_posts(self, page, creator, **kwargs):
            assert creator == "maker"
            assert kwargs["collect_all"] is False
            return [terminal_record()]

    import playwright.async_api
    import social_browser
    import tiktok_scraper.api_integration

    monkeypatch.setattr(
        social_browser,
        "load_engage_profile7_designation",
        lambda runtime: designation,
    )
    monkeypatch.setattr(
        social_browser,
        "load_verified_profile7_state",
        lambda runtime, supplied: {"cdp_url": "ws://127.0.0.1/profile7"},
    )
    monkeypatch.setattr(
        social_browser,
        "verified_profile_context",
        lambda browser, supplied: _async_value((browser.contexts[0], {})),
    )
    monkeypatch.setattr(
        social_browser,
        "platform_authentication",
        lambda context, platform: _async_value({"authenticated": True}),
    )
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: PlaywrightContext(),
    )
    monkeypatch.setattr(
        tiktok_scraper.api_integration,
        "TikTokAPIIntegration",
        Integration,
    )

    collector = TikTokBrowserCollector(
        state_path=state_path,
        musicbrainz_adapter=_UnsupportedMusicAdapter(),
    )
    monkeypatch.setattr(collector, "_cdp_url", lambda: "ws://127.0.0.1/profile7")
    rows = asyncio.run(
        collector.collect(
            topic="",
            requested_count=1,
            max_comments=10,
            max_pages=1,
            source_mode="url",
            direct_post_url="https://www.tiktok.com/@maker/video/123",
            collection_policy=collection_policy,
            refresh_candidates=(
                [terminal_record()]
                if collection_policy == "refresh_known"
                else ()
            ),
            music_catalogs=("musicbrainz",),
        )
    )

    assert len(rows) == 1
    assert rows[0]["music_evidence"]["platform_music"]["title"] == "Known Song"
    assert rows[0]["music_evidence"]["catalogs"]["musicbrainz"]["status"] == "unsupported"
    assert page.closed is True


async def _async_value(value):
    return value


class _UnsupportedMusicAdapter:
    def enrich(self, audio):
        from engage_tiktok import terminal_musicbrainz_result

        return terminal_musicbrainz_result(
            {
                "music_id": audio.get("music_id"),
                "title": audio.get("title"),
                "author": audio.get("author"),
                "is_original": audio.get("is_original"),
                "duration_ms": audio.get("duration_ms"),
            },
            status="unsupported",
            reason="test_terminal",
        )
