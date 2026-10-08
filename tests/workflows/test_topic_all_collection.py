import json

import pytest

import engage_tiktok as engage


def record(post_id, decision="accept", *, failed=False):
    value = {
        "id": str(post_id), "url": f"https://www.tiktok.com/@maker/video/{post_id}",
        "username": "maker", "content_type": "video", "caption": "Coffee preparation",
        "view_count": 5, "comments": [], "ok": True, "complete": True,
        "has_more": False, "transcript_status": "" if failed else "unavailable",
        "music_metadata_status": "not_provided", "topic_relevance_required": True,
        "topic_relevance": {"decision": decision},
    }
    value["music_evidence"] = engage.build_music_evidence(value, configured_catalogs=())
    return value


@pytest.fixture
def active(tmp_path):
    conn = engage.connect_database(tmp_path / "topic.sqlite")
    run_id = engage.create_run(conn, project="topic_all", topic="coffee", requested_count=0,
                               max_comments=5, max_pages=0, workflow="listen", source_mode="topic",
                               collect_all=True, music_catalogs=())
    attempt, recovered = engage._claim_collection_attempt(conn, run_id, resume=False)
    assert not recovered
    yield conn, run_id, attempt
    conn.close()


def freeze(active, records, *, terminal=True, **overrides):
    conn, run_id, attempt = active
    proof = {"terminal": terminal, "terminal_verified": terminal,
             "inventory_complete": terminal, "has_more": not terminal,
             "keyword": "coffee", "collect_all": True, "page_budget": 25,
             "stop_reason": "source_exhausted" if terminal else "page_cap_reached",
             "selected_post_ids": [item["id"] for item in records], **overrides}
    return engage._checkpoint_topic_inventory(conn, run_id=run_id, attempt_id=attempt,
                                               candidates=records, metadata=proof)


def checkpoint(active, value):
    conn, run_id, attempt = active
    return engage._checkpoint_collection_record(conn, run_id=run_id, attempt_id=attempt,
                                                topic="coffee", raw=value)


def test_topic_all_resolves_target_and_excludes_only_complete_irrelevant_evidence(active, tmp_path):
    conn, run_id, attempt = active
    rows = [record("123"), record("124", "reject"), record("125", "review", failed=True)]
    assert freeze(active, rows) == 3
    assert checkpoint(active, rows[0]) == (False, True)
    assert checkpoint(active, rows[1]) == (False, False)
    assert checkpoint(active, rows[2]) == (False, False)
    status = engage.run_status(conn, run_id)
    assert status["requested_count"] == 2  # failed hydration cannot be an exclusion
    assert status["topic_inventory"]["relevance_excluded_count"] == 1
    with pytest.raises(engage.StageGateError, match="unresolved selected"):
        engage._assert_topic_inventory_completion(conn, engage._run_row(conn, run_id))
    assert checkpoint(active, record("125")) == (True, True)
    engage._finish_collection_attempt(conn, run_id=run_id, attempt_id=attempt,
        status="collection_complete", event="complete", error="", payload={})
    exported = tmp_path / "evidence.jsonl"
    engage.export_listen_evidence(conn, run_id, exported)
    assert {json.loads(line)["post_id"] for line in exported.read_text().splitlines()} == {"123", "125"}
    assert engage._claim_collection_attempt(conn, run_id, resume=True) == ("", True)


def test_topic_all_partial_zero_is_not_complete_and_inventory_can_expand_on_resume(active):
    conn, run_id, attempt = active
    freeze(active, [], terminal=False)
    engage._finish_collection_attempt(conn, run_id=run_id, attempt_id=attempt,
        status="collection_incomplete", event="incomplete", error="stalled", payload={})
    resumed, recovered = engage._claim_collection_attempt(conn, run_id, resume=True)
    assert resumed and not recovered
    assert freeze((conn, run_id, resumed), [record("123")], terminal=True) == 1
    assert checkpoint((conn, run_id, resumed), record("123")) == (True, True)
    engage._assert_topic_inventory_completion(conn, engage._run_row(conn, run_id))


@pytest.mark.parametrize("override", [
    {"has_more": None}, {"keyword": "another topic"}, {"collect_all": False},
    {"selected_post_ids": []},
])
def test_topic_all_refuses_unproven_end_wrong_query_or_omitted_candidate(active, override):
    with pytest.raises(engage.StageGateError):
        freeze(active, [record("123")], **override)


def test_topic_all_frozen_selection_and_exclusion_proof_cannot_be_rewritten(active):
    conn, run_id, _ = active
    freeze(active, [record("123")])
    with pytest.raises(engage.StageGateError, match="cannot change"):
        freeze(active, [record("123"), record("124")])
    # A fabricated exclusion cannot turn zero evidence into a completed ALL run.
    conn.execute("UPDATE engage_tiktok_runs SET topic_excluded_post_ids_json='[\"123\"]', requested_count=0, requested=0 WHERE run_id=?", (run_id,))
    conn.commit()
    with pytest.raises(engage.StageGateError, match="no saved evidence"):
        engage._assert_topic_inventory_completion(conn, engage._run_row(conn, run_id))


def test_topic_all_empty_verified_search_can_complete(active):
    conn, run_id, attempt = active
    assert freeze(active, []) == 0
    engage._finish_collection_attempt(conn, run_id=run_id, attempt_id=attempt,
        status="collection_complete", event="complete", error="", payload={})
    assert engage.run_status(conn, run_id)["topic_inventory"]["terminal_verified"] is True
