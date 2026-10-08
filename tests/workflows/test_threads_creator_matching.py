"""Synthetic corpus regressions. No platform access, AI claims or real approval."""
import copy
import json

import pytest

import engage_social as cli
from social_engage.state import State, StateError, digest


@pytest.fixture
def corpus(tmp_path):
    clock = [1000000.0]
    state = State(tmp_path / "social.sqlite3", "threads", clock=lambda: clock[0])
    scope = {"platform": "threads", "workflow": "engage", "mode": "shadow", "source": "topic", "target": "pottery",
             "account": "operator", "posts": 3, "comments": 10, "candidate_limit": 20}
    run = state.create(scope)
    state.freeze(run, ["101", "102", "103"], {"id": "9", "username": "operator"})
    for pid, author in [("101", "potter_a"), ("102", "potter_b"), ("103", "potter_c")]:
        evidence = {"platform": "threads", "id": pid, "author": author,
                    "url": f"https://www.threads.com/@{author}/post/Ab{pid}",
                    "text": "Beginner pottery: dry the base slowly before firing. A checklist for learners.",
                    "comments": [{"id": "c" + pid, "text": "The checklist helped me check the base.", "author": "reader"}],
                    "comments_complete": True, "metrics": {}, "observed_at": "2026-09-14T00:00:00Z"}
        state.checkpoint(run, evidence)
    state.finalize(run)
    for pid in ("101", "102", "103"):
        p = state.packet(run, pid)
        state.analysis(run, pid, {"evidence_hash": p["evidence_hash"], "agent_id": "analyst", "summary": "Specific drying checklist for beginners.",
                       "limitations": "Text only.", "response_type": "positive_support", "score": 8, "evidence_refs": ["text"]})
    yield state, run, clock
    state.close()


def result_input(state, run):
    exported = state.export_creator_matches(run)
    refs = [{"field": "text", "quote": "Beginner pottery: dry the base slowly before firing."}]
    match = {"post_id": "102", "confidence": 92, "reason": "Also shares a drying checklist for beginner potters.",
             "similarities": [{"dimension": d, "detail": "Synthetic supporting detail.", "source_refs": copy.deepcopy(refs),
                               "candidate_refs": copy.deepcopy(refs)} for d in ("subtopic", "technique", "audience_level")]}
    return {"run_id": run, "scope_hash": exported["scope_hash"], "agent_id": "matcher", "results": [
        {"post_id": "101", "matches": [match], "no_match_reason": ""},
        {"post_id": "102", "matches": [], "no_match_reason": "No additional useful connection proposed."},
        {"post_id": "103", "matches": [], "no_match_reason": "No additional useful connection proposed."}]}


def draft_input(state, run):
    p = state.packet(run, "101")
    return {"evidence_hash": p["evidence_hash"], "analysis_hash": p["analysis_hash"],
            "creator_match_hash": p.get("creator_match_hash"), "agent_id": "writer", "evidence_refs": ["text"],
            "text": "8/10 — Your checklist makes the drying step explicit for beginners. (AI assisted)"}


def review_input(state, run):
    p = state.packet(run, "101")
    return {"evidence_hash": p["evidence_hash"], "draft_hash": p["draft_hash"], "creator_match_hash": p["creator_match_hash"],
            "agent_id": "critic", "notes": "Synthetic review fixture only.", "checks": {k: True for k in
            ("grounded", "specific", "useful", "not_repetitive", "tone", "disclosure", "response_type", "rating",
             "creator_match_grounding", "creator_mention_usefulness")}}


def ready_shadow(state, run):
    state.import_creator_matches(run, result_input(state, run))
    state.draft(run, "101", draft_input(state, run))
    state.review(run, "101", review_input(state, run))


def test_full_corpus_to_shadow_has_context_bindings_and_rendered_mentions(corpus):
    state, run, _ = corpus
    exported = state.export_creator_matches(run)
    assert len(exported["posts"]) == 3
    assert exported["posts"][0]["evidence"]["comments"][0]["id"] == "c101"
    ready_shadow(state, run)
    packet = state.packet(run, "101")
    assert packet["creator_match_evidence"][0]["evidence"]["comments"][0]["id"] == "c102"
    shown = state.present(run, "101")
    assert shown["text"].endswith("@potter_b: Also shares a drying checklist for beginner potters.")
    assert shown["creator_match_hash"] == packet["creator_match_hash"]
    assert shown["publication_enabled"] is False


@pytest.mark.parametrize("change,reason", [
    (lambda d: d.update(run_id="another"), "stale_creator_matches"),
    (lambda d: d.update(scope_hash="stale"), "stale_creator_matches"),
    (lambda d: d["results"].pop(), "complete_matching_batch_required"),
    (lambda d: d["results"][2].update(post_id="102"), "complete_matching_batch_required"),
    (lambda d: d["results"][2].update(no_match_reason=""), "invalid_no_match_reason"),
    (lambda d: d["results"][0]["matches"][0].update(post_id="outside"), "matching_same_run_candidate_required"),
    (lambda d: d["results"][0]["matches"][0].update(post_id="101"), "matching_distinct_creator_required"),
    (lambda d: d["results"][0]["matches"][0].update(confidence=84), "invalid_match_confidence"),
    (lambda d: d["results"][0]["matches"][0].update(confidence=True), "invalid_match_confidence"),
    (lambda d: d["results"][0]["matches"][0].update(confidence=float("nan")), "invalid_match_confidence"),
    (lambda d: d["results"][0]["matches"][0].update(reason="Contact @someone for more information."), "invalid_public_connection_reason"),
    (lambda d: d["results"][0]["matches"][0]["similarities"][0].update(dimension="metrics"), "invalid_matching_dimensions"),
    (lambda d: d["results"][0]["matches"][0]["similarities"][0]["candidate_refs"][0].update(quote="This quote was never present."), "matching_quote_not_in_evidence"),
    (lambda d: d["results"][0]["matches"][0]["similarities"][0]["candidate_refs"][0].update(field="transcript"), "unsupported_matching_reference"),
    (lambda d: d["results"][0]["matches"].append(copy.deepcopy(d["results"][0]["matches"][0])), "matching_distinct_creator_required"),
])
def test_invalid_batch_is_atomic(corpus, change, reason):
    state, run, _ = corpus
    data = result_input(state, run)
    before = state.load(run)
    change(data)
    with pytest.raises(StateError, match=reason):
        state.import_creator_matches(run, data)
    assert state.load(run) == before


@pytest.mark.parametrize("field,value", [("platform", "linkedin"), ("workflow", "listen"), ("workflow", "audit"),
                                         ("source", "creator"), ("mode", "live")])
def test_scope_gate(corpus, field, value):
    state, run, _ = corpus
    doc = state.load(run)
    doc["scope"][field] = value
    doc["scope_hash"] = digest(doc["scope"])
    if field == "platform":
        state.platform = value
    state._save(doc)  # Synthetic invalid-scope fixture, never operational state.
    with pytest.raises(StateError, match="matching_requires_threads"):
        state.export_creator_matches(run)


def test_pending_matching_blocks_drafts_and_batch_reimport_is_immutable(corpus):
    state, run, _ = corpus
    data = result_input(state, run)
    with pytest.raises(StateError, match="creator_matching_pending"):
        state.draft(run, "101", draft_input(state, run))
    state.import_creator_matches(run, data)
    state.import_creator_matches(run, data)
    data["results"][2]["no_match_reason"] = "Changed after freeze."
    with pytest.raises(StateError, match="already_frozen"):
        state.import_creator_matches(run, data)


@pytest.mark.parametrize("change,reason", [
    (lambda d: d.update(creator_match_hash="old"), "stale_draft_creator_matches"),
    (lambda d: d.update(text=d["text"] + " @stranger"), "base_draft_must_not_supply_mentions"),
    (lambda d: d.update(text="8/10 (AI) " + "x" * 470), "invalid_response"),
])
def test_draft_checks_rendered_output(corpus, change, reason):
    state, run, _ = corpus
    state.import_creator_matches(run, result_input(state, run))
    data = draft_input(state, run)
    change(data)
    with pytest.raises(StateError, match=reason):
        state.draft(run, "101", data)


@pytest.mark.parametrize("change,reason", [
    (lambda d: d.update(agent_id="matcher"), "independent_match_reviewer_required"),
    (lambda d: d.update(creator_match_hash="old"), "stale_review_creator_matches"),
    (lambda d: d["checks"].pop("creator_match_grounding"), "complete_review_checks_required"),
])
def test_match_review_requires_independence_and_extra_checks(corpus, change, reason):
    state, run, _ = corpus
    state.import_creator_matches(run, result_input(state, run))
    state.draft(run, "101", draft_input(state, run))
    data = review_input(state, run)
    change(data)
    with pytest.raises(StateError, match=reason):
        state.review(run, "101", data)


@pytest.mark.parametrize("operation", ["refresh", "purge", "expire"])
def test_candidate_changes_scrub_dependents_and_revisions(corpus, operation):
    state, run, clock = corpus
    if operation == "expire":
        doc = state.load(run)
        doc["posts"]["102"]["expires"] = clock[0] + 10
        state._save(doc)
    ready_shadow(state, run)
    state.present(run, "101")
    doc = state.load(run)
    doc["posts"]["101"]["revisions"] = [copy.deepcopy(doc["posts"]["101"])]
    state._save(doc)
    if operation == "refresh":
        state.refresh(run, {**doc["posts"]["102"]["evidence"], "text": "Updated candidate text."})
    elif operation == "purge":
        state.purge_expired("102")
    else:
        clock[0] = doc["posts"]["102"]["expires"] + 1
        state.purge_expired()
    after = state.load(run)
    assert after["creator_matching"]["status"] == "invalidated"
    assert "results" not in after["creator_matching"]
    if operation == "expire":
        assert "evidence" in after["posts"]["101"]
        assert after["posts"]["102"]["stage"] == "purged"
    for p in after["posts"].values():
        for version in [p, *p.get("revisions", [])]:
            assert not any(k in version for k in ("draft", "review", "presentation", "authorization"))


def test_live_is_blocked_before_network_and_reservation(corpus, monkeypatch):
    state, run, _ = corpus
    ready_shadow(state, run)
    shown = state.present(run, "101")
    for operation in (lambda: state.request_live(run, "101"),
                      lambda: state.authorize(run, "101", shown["presentation_hash"], shown["approval_token"]),
                      lambda: state.ready(run, "101"), lambda: state.reserve(run, "101")):
        with pytest.raises(StateError, match="threads_creator_matching_shadow_only"):
            operation()
    assert state.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    def fail(*args):
        raise AssertionError("must not create a platform adapter")
    monkeypatch.setattr(cli, "live_adapter", fail)
    monkeypatch.setattr(cli, "State", lambda *args: state)
    monkeypatch.setattr(state, "close", lambda: None)
    args = cli.parser().parse_args(["--database", str(state.path), "publish", "--run-id", run, "--post-id", "101"])
    with pytest.raises(StateError, match="threads_creator_matching_shadow_only"):
        cli.execute(args)


def test_all_no_match_valid_and_cannot_enable_after_drafting(corpus):
    state, run, _ = corpus
    data = result_input(state, run)
    for row in data["results"]:
        row.update(matches=[], no_match_reason="Insufficient specific evidence for a useful connection.")
    state.import_creator_matches(run, data)
    state.draft(run, "101", draft_input(state, run))
    assert "@" not in state.packet(run, "101")["draft"]["text"]
    with pytest.raises(StateError, match="matching_must_precede_drafts"):
        state.export_creator_matches(run)


def test_cli_export_import_uses_same_model_neutral_state(corpus, tmp_path, monkeypatch):
    state, run, _ = corpus
    monkeypatch.setattr(cli, "State", lambda *args: state)
    monkeypatch.setattr(state, "close", lambda: None)
    target = tmp_path / "corpus.json"
    args = ["--database", str(state.path)]
    result = cli.execute(cli.parser().parse_args(args + ["export-creator-matches", "--run-id", run, "--file", str(target)]))
    assert result["posts"] == 3 and json.loads(target.read_text(encoding="utf-8"))["scope_hash"] == result["scope_hash"]
    target = tmp_path / "results.json"
    target.write_text(json.dumps(result_input(state, run)), encoding="utf-8")
    status = cli.execute(cli.parser().parse_args(args + ["import-creator-matches", "--run-id", run, "--file", str(target)]))
    assert status["creator_matching"]["status"] == "complete"


@pytest.mark.parametrize("kind", ["incomplete", "unanalyzed", "wrong_author_url", "expired"])
def test_corpus_rejects_unready_evidence(corpus, kind):
    state, run, clock = corpus
    doc = state.load(run)
    if kind == "incomplete":
        doc["collection_verified"] = False
    elif kind == "unanalyzed":
        doc["posts"]["102"].pop("analysis")
    elif kind == "wrong_author_url":
        p = doc["posts"]["102"]
        p["evidence"]["author"] = "imposter"
        p["evidence_hash"] = digest(p["evidence"])
        p["analysis"]["evidence_hash"] = p["evidence_hash"]
        p["analysis_hash"] = digest(p["analysis"])
    else:
        clock[0] = doc["posts"]["102"]["expires"] + 1
    state._save(doc)
    with pytest.raises(StateError):
        state.export_creator_matches(run)


def test_nonpositive_candidate_and_comment_only_core_dimension_rejected(corpus):
    state, run, _ = corpus
    doc = state.load(run)
    p = doc["posts"]["102"]
    p["analysis"]["response_type"] = "clarifying_question"
    p["analysis_hash"] = digest(p["analysis"])
    state._save(doc)
    data = result_input(state, run)
    with pytest.raises(StateError, match="matching_positive_candidate_required"):
        state.import_creator_matches(run, data)
    # Third candidate remains eligible; its comment cannot establish a core teaching claim.
    match = data["results"][0]["matches"][0]
    match["post_id"] = "103"
    match["similarities"][0]["candidate_refs"] = [{"field": "comment", "comment_id": "c103", "quote": "The checklist helped me check the base."}]
    with pytest.raises(StateError, match="matching_direct_text_required"):
        state.import_creator_matches(run, data)


def test_refreshed_candidate_can_be_reanalyzed_and_matching_recomputed(corpus):
    state, run, _ = corpus
    ready_shadow(state, run)
    old_hash = state.packet(run, "101")["creator_match_hash"]
    old = state.load(run)["posts"]["102"]
    state.refresh(run, {**old["evidence"], "text": old["evidence"]["text"] + " Updated notes."})
    with pytest.raises(StateError):
        state.export_creator_matches(run)
    state.analysis(run, "102", {**old["analysis"], "evidence_hash": state.packet(run, "102")["evidence_hash"]})
    state.import_creator_matches(run, result_input(state, run))
    assert state.packet(run, "101")["creator_match_hash"] != old_hash


def test_single_creator_corpus_has_only_honest_no_match_results(corpus):
    state, run, _ = corpus
    doc = state.load(run)
    for p in doc["posts"].values():
        p["evidence"]["author"] = "one_creator"
        p["evidence"]["url"] = "https://www.threads.com/@one_creator/post/Ab" + p["evidence"]["id"]
        p["evidence_hash"] = digest(p["evidence"])
        p["analysis"]["evidence_hash"] = p["evidence_hash"]
        p["analysis_hash"] = digest(p["analysis"])
    state._save(doc)
    data = result_input(state, run)
    with pytest.raises(StateError, match="matching_distinct_creator_required"):
        state.import_creator_matches(run, data)
    for row in data["results"]:
        row.update(matches=[], no_match_reason="All collected posts belong to the same creator.")
    state.import_creator_matches(run, data)
    assert state.status(run)["creator_matching"]["status"] == "complete"


def test_frozen_candidate_analysis_hash_cannot_be_substituted(corpus):
    state, run, _ = corpus
    state.import_creator_matches(run, result_input(state, run))
    doc = state.load(run)
    doc["posts"]["102"]["analysis"]["summary"] = "Changed classification explanation."
    doc["posts"]["102"]["analysis_hash"] = digest(doc["posts"]["102"]["analysis"])
    state._save(doc)
    with pytest.raises(StateError, match="stale_creator_matches"):
        state.packet(run, "101")
