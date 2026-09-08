import asyncio
import sqlite3

import pytest

import engage_tiktok
from test_engage_tiktok import (
    RecordingCollector,
    RecordingPreflight,
    evidence,
    run_fake_production_collector,
)


@pytest.mark.parametrize(
    "workflow,supplied,expected",
    [
        ("engage", None, 0),
        ("engage", 7, 7),
        ("listen", None, 4),
        ("audit", None, 4),
    ],
)
def test_cli_persists_only_engage_default_as_uncapped(
    tmp_path, monkeypatch, workflow, supplied, expected
):
    database = tmp_path / "workflow.sqlite"

    async def fake_collect(conn, *, run_id, **kwargs):
        run = conn.execute(
            "SELECT max_pages, topic_query_policy FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert run["max_pages"] == expected
        assert run["topic_query_policy"] == "exact"
        return {"run_id": run_id, "status": "offline_fixture_only"}

    monkeypatch.setattr(engage_tiktok, "collect_exact", fake_collect)
    argv = [
        "--database", str(database),
        "--master-database", str(tmp_path / "master.sqlite"),
        "collect", "--project", "pagination-default",
        "--topic", "coffee", "--posts", "1", "--workflow", workflow,
    ]
    if supplied is not None:
        argv.extend(["--max-pages", str(supplied)])
    assert engage_tiktok.main(argv) == 0


@pytest.mark.parametrize(
    "workflow,query_policy",
    [("listen", "exact"), ("audit", "exact"), ("engage", "related_variants_v1")],
)
def test_zero_topic_bound_does_not_reinterpret_other_workflows(workflow, query_policy):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        engage_tiktok.ensure_schema(conn)
        with pytest.raises(ValueError, match="collection bounds"):
            engage_tiktok.create_run(
                conn, project="bounded-workflow", topic="coffee",
                requested_count=1, max_comments=10, max_pages=0,
                workflow=workflow, topic_query_policy=query_policy,
            )


def _integration_for_rounds(rounds):
    class Integration:
        instance = None

        def __init__(self, **kwargs):
            self.calls = []
            self.hydrated_ids = []
            self.last_search_diagnostics = {}
            type(self).instance = self

        async def initialize_api(self, page):
            return True

        async def discover_search_videos(
            self, page, topic, *, max_offsets, include_related_queries, target_count
        ):
            assert topic == "coffee"
            assert include_related_queries is False
            assert max_offsets > 0
            index = min(len(self.calls), len(rounds) - 1)
            ids, reason, has_more, relevant = rounds[index]
            self.calls.append((max_offsets, target_count))
            self.last_search_diagnostics = {
                "stop_reason": reason,
                "has_more": has_more,
                "pages_received": max_offsets,
                "queries_attempted": 1,
                "query_variants_planned": 1,
            }
            return [
                {
                    **evidence(post_id),
                    "caption": "Coffee brewing technique" if relevant else "Saturn nebula",
                }
                for post_id in ids
            ]

        async def hydrate_video_candidates(self, page, candidates):
            self.hydrated_ids.extend(row["id"] for row in candidates)

        async def get_comments_for_multiple_videos(self, page, candidates, **kwargs):
            return {
                row["id"]: {
                    "comments": [], "ok": True, "complete": True,
                    "exhausted": True, "limit_reached": False, "has_more": False,
                    "transcript_status": "unavailable",
                    "subtitle_no_caption_reason": "not_provided",
                }
                for row in candidates
            }

    return Integration


@pytest.mark.parametrize("reason", ["candidate_target_reached", "page_cap_reached"])
def test_uncapped_same_query_continues_past_many_known_prefixes(
    tmp_path, monkeypatch, reason
):
    known = [str(index) for index in range(1, 6)]
    rounds = [(known[:index], reason, True, True) for index in range(1, 6)]
    rounds.append(([*known, "99"], "source_exhausted", False, True))
    integration = _integration_for_rounds(rounds)

    records, collector, page, browser = run_fake_production_collector(
        tmp_path, monkeypatch, integration, requested_count=1, max_pages=0,
        collector_options={"global_known_post_ids": known, "music_catalogs": ()},
    )

    assert [row["id"] for row in records] == ["99"]
    assert integration.instance.hydrated_ids == ["99"]
    assert len(integration.instance.calls) == 6
    budgets, targets = zip(*integration.instance.calls)
    assert list(budgets) == sorted(budgets)
    assert all(later > earlier for earlier, later in zip(targets, targets[1:]))
    assert collector.last_diagnostics["evidence_ready_candidates"] == 1
    assert collector.last_diagnostics["collection_stop_reason"] == "exact_count_reached"
    assert page.closed and browser.close_calls == 0


def test_irrelevant_posts_do_not_stop_deeper_search_or_fill_quota(tmp_path, monkeypatch):
    rounds = [
        ([str(index)], "page_cap_reached", True, False)
        for index in range(1, 5)
    ]
    rounds.append((["99"], "source_exhausted", False, True))
    integration = _integration_for_rounds(rounds)
    records, collector, _, _ = run_fake_production_collector(
        tmp_path, monkeypatch, integration, requested_count=1, max_pages=0,
        collector_options={"music_catalogs": ()},
    )
    ready_ids = [
        row["id"] for row in records
        if engage_tiktok.normalize_evidence(row, topic="coffee")[1]
    ]
    assert ready_ids == ["99"]
    assert len(integration.instance.calls) == 5
    assert collector.last_diagnostics["evidence_ready_candidates"] == 1


@pytest.mark.parametrize(
    "bound,reason,has_more",
    [
        (3, "page_cap_reached", True),
        (0, "page_cap_reached", None),
        (0, "page_cap_reached", False),
        (0, "source_exhausted", False),
        (0, "pagination_stalled", True),
        (0, "cursor_did_not_advance", True),
        (0, "rendered_search_frontier_stalled", None),
    ],
)
def test_explicit_cap_or_unproven_frontier_stops_honestly(
    tmp_path, monkeypatch, bound, reason, has_more
):
    integration = _integration_for_rounds([(["1"], reason, has_more, True)])
    records, collector, _, _ = run_fake_production_collector(
        tmp_path, monkeypatch, integration, requested_count=1, max_pages=bound,
        collector_options={"global_known_post_ids": ["1"], "music_catalogs": ()},
    )
    assert records == []
    assert len(integration.instance.calls) == 1
    assert collector.last_diagnostics["evidence_ready_candidates"] == 0
    assert collector.last_diagnostics["collection_stop_reason"] != "exact_count_reached"


def test_repeated_source_snapshot_stops_without_claiming_exhaustion(tmp_path, monkeypatch):
    integration = _integration_for_rounds([(["1"], "page_cap_reached", True, True)])
    records, collector, _, _ = run_fake_production_collector(
        tmp_path, monkeypatch, integration, requested_count=1, max_pages=0,
        collector_options={"global_known_post_ids": ["1"], "music_catalogs": ()},
    )
    assert records == []
    assert len(integration.instance.calls) == 4
    assert collector.last_diagnostics["collection_stop_reason"] == "exact_query_frontier_stalled"
    assert collector.last_diagnostics["discovery_rounds"][-1]["source_stall_rounds"] == 3


@pytest.mark.parametrize("bound", [0, 7])
def test_resume_preserves_page_bound_and_ready_checkpoints(tmp_path, bound):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        engage_tiktok.ensure_schema(conn)
        run_id = engage_tiktok.create_run(
            conn, project="resume-pagination", topic="coffee", requested_count=2,
            max_comments=10, max_pages=bound, music_catalogs=(),
        )
        calls = []
        initial = RecordingCollector(calls, [evidence("1")])
        with pytest.raises(engage_tiktok.CollectionIncompleteError):
            asyncio.run(engage_tiktok.collect_exact(
                conn, run_id=run_id, preflight=RecordingPreflight(calls), collector=initial,
            ))
        with pytest.raises(engage_tiktok.StageGateError, match="exact-count"):
            engage_tiktok.export_analysis_queue(conn, run_id, tmp_path / "blocked.jsonl")
        saved = conn.execute(
            "SELECT evidence_hash FROM engage_tiktok_posts WHERE run_id=? AND post_id='1'",
            (run_id,),
        ).fetchone()[0]
        calls = []
        resumed = RecordingCollector(calls, [evidence("2")])
        result = asyncio.run(engage_tiktok.collect_exact(
            conn, run_id=run_id, preflight=RecordingPreflight(calls),
            collector=resumed, resume=True,
        ))
        assert initial.arguments["max_pages"] == bound
        assert resumed.arguments["max_pages"] == bound
        assert result["evidence_ready"] == 2
        assert result["status"] == "collection_complete"
        assert conn.execute(
            "SELECT evidence_hash FROM engage_tiktok_posts WHERE run_id=? AND post_id='1'",
            (run_id,),
        ).fetchone()[0] == saved
