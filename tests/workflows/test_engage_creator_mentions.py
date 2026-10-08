"""Same-run matching uses synthetic posts and temporary databases only."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import runpy

import pytest

import engage_tiktok as engage
import engage_creator_matching as matching

HELPERS = runpy.run_path(str(Path(__file__).with_name("test_engage_stage_gates.py")))
JAZZ = "Jazz guitar tutorial: ii-V-I voicings for intermediate players with step-by-step chord examples."
ROCK = "Rock guitar tutorial: distorted power chords for beginner players using a loud rock riff."


@pytest.fixture
def corpus(tmp_path, request):
    conn = engage.connect_database(tmp_path / "state.sqlite")
    try:
        run_id = engage.create_run(conn, project="jazz", topic="guitar tutorial", requested_count=5,
                                   max_comments=20, max_pages=10, mode=getattr(request, "param", "shadow"), music_catalogs=())
        class Collector(HELPERS["StaticCollector"]):
            async def collect(self, **kwargs):
                records = await super().collect(**kwargs)
                for index, record in enumerate(records):
                    handle = ("creator_a", "creator_b", "creator_k", "creator_l", "creator_m")[index]
                    record.update(username=handle, url=f"https://www.tiktok.com/@{handle}/video/{index+1}",
                                  caption=JAZZ if index in (0, 3, 4) else ROCK)
                return records
        asyncio.run(engage.collect_exact(conn, run_id=run_id, preflight=HELPERS["ReadyPreflight"](), collector=Collector()))
        HELPERS["analyze"](conn, run_id, tmp_path, [1, 2, 3, 4, 5])
        yield conn, run_id, tmp_path
    finally:
        conn.close()


def export(corpus, limit=2):
    conn, run_id, root = corpus
    path = root / "matching-queue.jsonl"
    assert engage.export_creator_matches(conn, run_id, path, max_mentions=limit) == 5
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def selected(post_id="1"):
    dimensions = [("subtopic", "Jazz guitar tutorial"), ("technique", "ii-V-I voicings"),
                  ("audience_level", "intermediate players")]
    return {"post_id": post_id, "confidence": 94,
            "reason": "Also teaches ii-V-I jazz voicings for intermediate guitar players.",
            "similarities": [{"dimension": dimension, "detail": quote,
                              "source_refs": [{"field": "caption", "quote": quote}],
                              "candidate_refs": [{"field": "caption", "quote": quote}]}
                             for dimension, quote in dimensions]}


def outcomes(queue):
    return [{"post_id": row["post_id"], "scope_hash": row["scope_hash"],
             "matches": [selected("1"), selected("5")] if row["post_id"] == "4" else [],
             "no_match_reason": "No additional connection selected with sufficient evidence."}
            for row in queue]


def write(corpus, records, name="matching-results.jsonl"):
    return HELPERS["write_records"](corpus[2] / name, records)


def import_outcomes(corpus, records):
    conn, run_id, _ = corpus
    mode = conn.execute("SELECT mode FROM engage_tiktok_runs WHERE run_id=?", (run_id,)).fetchone()[0]
    probes = [synthetic_native_probe(corpus, records)] if mode == "live" else []
    return engage.import_creator_matches(conn, run_id, write(corpus, records), actor="codex-matcher", native_probes=probes)


def synthetic_native_probe(corpus, records):
    """Explicit fake UI receipt for the synthetic LIVE integration corpus."""
    conn, run_id, root = corpus
    account = conn.execute("SELECT expected_account FROM engage_tiktok_runs WHERE run_id=?", (run_id,)).fetchone()[0]
    selected_records = [record for record in records if record.get("matches")]
    source = HELPERS["post_row"](conn, run_id, selected_records[0]["post_id"])
    labels = {}
    for record in selected_records:
        for match in record["matches"]:
            candidate = HELPERS["post_row"](conn, run_id, match["post_id"])
            handle = json.loads(candidate["evidence_json"])["creator"]
            labels[handle] = match.get("mention_label", "@" + handle)
    final = " ".join(f"{label}: Synthetic rehearsal only." for label in labels.values())
    path = root / "synthetic-native-probe.json"
    path.write_text(json.dumps({
        "schema_version": "engage-mentions-probe-v1", "status": "passed",
        "publication_enabled": False, "url": source["url"],
        "expected_account": account, "observed_account": account,
        "handles": list(labels),
        "observed_mentions": [{"creator_handle": handle, "mention_label": label} for handle, label in labels.items()],
        "cases": [{"case": index, "status": "passed", "text": final} for index in (1, 2)],
        "blocked_publish_requests": 0, "editor_cleared": True, "temporary_tab_closed": True,
    }), encoding="utf-8")
    return path


def add_semantic_context(corpus, *, normalized_comments=False):
    """Synthetic stored evidence, with transport noise and nested real citations."""
    conn, run_id, _ = corpus
    for post_id in (1, 2, 3, 4, 5):
        row = HELPERS["post_row"](conn, run_id, post_id)
        evidence = json.loads(row["evidence_json"])
        evidence.update(
            transcript=JAZZ,
            transcript_status="available",
            transcript_language="en",
            # Omitted malformed entries must not renumber citation indices.
            transcript_segments=[None, {"text": JAZZ, "start": 0, "end": 4,
                                        "url": "https://transport.invalid/segment?token=transport-secret"}],
            subtitle_tracks=[{"language_code": "en", "name": "English", "source": "tiktok",
                              "is_auto_generated": True,
                              "url": "https://transport.invalid/subtitle?token=transport-secret",
                              "token": "transport-secret"}],
            subtitle_selected_track={"language": "en", "display_name": "English", "source": "tiktok",
                                     "auto_generated": True, "headers": {"authorization": "transport-secret"}},
            subtitle_no_caption_reason="",
            comments_status={"ok": True, "complete": False, "has_more": True, "limit_reached": True,
                             "source": "api", "error": "", "token": "transport-secret"},
            comments=[{"cid": f"comment-{post_id}", "text": "Can intermediate players try these ii-V-I voicings?",
                       "digg_count": 0, "reply_comment_total": 2,
                       "user": {"unique_id": "learner", "nickname": "Guitar learner",
                                "avatar": "https://transport.invalid/avatar", "sec_uid": "transport-secret"},
                       "reply_comment": [{"cid": f"reply-{post_id}", "text": "Yes, keep the chord changes slow at first.",
                                          "user": {"unique_id": f"creator_{post_id}"},
                                          "replies": [{"id": f"nested-{post_id}",
                                                       "comment_text": "The intermediate players explanation helped me.",
                                                       "author_handle": "another_learner",
                                                       "comment_language": "en", "is_author_digged": True,
                                                       "reply_to_reply_id": f"reply-{post_id}",
                                                       "image_list": [{"url": "https://transport.invalid/image"}]}]}]}],
        )
        if normalized_comments:
            evidence["comments"] = [{
                "comment_id": f"comment-{post_id}",
                "text": "Can intermediate players try these ii-V-I voicings?",
                "author_handle": "learner", "author_display_name": "Guitar learner",
                "likes": 0, "reported_reply_count": 2, "creator_pinned": True,
                "avatar": "https://transport.invalid/avatar", "session_token": "transport-secret",
                "replies": [{"comment_id": f"reply-{post_id}",
                             "text": "Yes, keep the chord changes slow at first.",
                             "author_handle": f"creator_{post_id}",
                             "parent_comment_id": f"comment-{post_id}",
                             "replies": [{"comment_id": f"nested-{post_id}",
                                          "text": "The intermediate players explanation helped me.",
                                          "author_handle": "another_learner", "language": "en",
                                          "creator_liked": True, "reply_to_comment_id": f"reply-{post_id}"}]}],
            }]
        evidence_hash = engage.json_hash(evidence)
        analysis_hash = engage.json_hash({"evidence_hash": evidence_hash, "analysis": json.loads(row["analysis_json"])})
        conn.execute("UPDATE engage_tiktok_posts SET evidence_json=?,evidence_hash=?,analysis_hash=? WHERE run_id=? AND post_id=?",
                     (engage.canonical_json(evidence), evidence_hash, analysis_hash, run_id, str(post_id)))
    conn.commit()


def contextual_outcomes(queue):
    records = outcomes(queue)
    source = next(record for record in records if record["post_id"] == "4")
    for match in source["matches"]:
        technique = match["similarities"][1]
        technique["source_refs"] = [{"field": "subtitle_segment", "segment_index": 1, "quote": "ii-V-I voicings"}]
        technique["candidate_refs"] = deepcopy(technique["source_refs"])
        audience = match["similarities"][2]
        audience["source_refs"] = [{"field": "comment", "comment_id": "nested-4", "quote": "intermediate players"}]
        audience["candidate_refs"] = [{"field": "comment", "comment_id": f"nested-{match['post_id']}", "quote": "intermediate players"}]
    return records


@pytest.mark.parametrize("normalized_comments", [False, True])
def test_matching_and_critic_receive_full_sanitized_subtitle_and_comment_context(corpus, normalized_comments):
    add_semantic_context(corpus, normalized_comments=normalized_comments)
    conn, run_id, root = corpus
    raw_before = {row["post_id"]: row["evidence_json"] for row in conn.execute("SELECT post_id,evidence_json FROM engage_tiktok_posts")}
    queue = export(corpus)
    source = next(record for record in queue if record["post_id"] == "4")
    context = source["context_evidence"]
    assert source["match_evidence"] == {"caption": JAZZ, "transcript": JAZZ, "visual_text": ""}
    assert context["transcript_segments"] == [{"segment_index": 1, "text": JAZZ, "start": 0, "end": 4}]
    assert context["transcript_language"] == "en" and context["transcript_status"] == "available"
    assert context["subtitle_tracks"][0]["display_name"] == "English"
    assert context["subtitle_selected_track"]["auto_generated"] is True
    assert context["subtitle_no_caption_reason"] == ""
    assert context["comments_status"]["has_more"] is True
    assert context["observed_at"]
    comment = context["comments"][0]
    assert comment["likes"] == 0 and comment["reported_reply_count"] == 2
    assert comment["author_handle"] == "learner"
    assert comment["creator_pinned"] is normalized_comments
    nested = comment["replies"][0]["replies"][0]
    assert nested["text"] == "The intermediate players explanation helped me."
    assert nested["comment_id"] == "nested-4" and nested["parent_comment_id"] == "reply-4"
    assert nested["reply_to_comment_id"] == "reply-4" and nested["creator_liked"] is True
    assert "transport-secret" not in json.dumps(queue) and "transport.invalid" not in json.dumps(queue)
    assert "not independent corroboration" in source["prompt"]
    result = import_outcomes(corpus, contextual_outcomes(queue))
    assert result["creator_mentions"] == 2
    HELPERS["draft"](conn, run_id, root, [1, 2, 3, 4, 5])
    engage.export_review_queue(conn, run_id, root / "context-reviews.jsonl")
    review_queue = [json.loads(line) for line in (root / "context-reviews.jsonl").read_text(encoding="utf-8").splitlines()]
    review = next(record for record in review_queue if record["post_id"] == "4")
    for candidate in review["creator_match_evidence"]:
        original = next(record for record in queue if record["post_id"] == candidate["post_id"])
        assert candidate["context_evidence"] == original["context_evidence"]
        assert candidate["evidence_hash"] == original["evidence_hash"]
        assert "transport-secret" not in json.dumps(candidate)
    reviewed_match = review["creator_mentions"]["matches"][0]
    assert reviewed_match["similarities"][1]["source_refs"][0]["segment_index"] == 1
    assert reviewed_match["similarities"][2]["candidate_refs"][0]["comment_id"] == "nested-1"
    assert {row["post_id"]: row["evidence_json"] for row in conn.execute("SELECT post_id,evidence_json FROM engage_tiktok_posts")} == raw_before


@pytest.mark.parametrize("reference", [
    {"field": "comment", "comment_id": "missing", "quote": "intermediate players"},
    {"field": "comment", "comment_id": "nested-1", "quote": "invented absent evidence"},
    {"field": "comment", "comment_id": 1, "quote": "intermediate players"},
    {"field": "comment", "quote": "intermediate players"},
    {"field": "subtitle_segment", "segment_index": True, "quote": "ii-V-I voicings"},
    {"field": "subtitle_segment", "segment_index": "1", "quote": "ii-V-I voicings"},
    {"field": "subtitle_segment", "segment_index": 1.0, "quote": "ii-V-I voicings"},
    {"field": "subtitle_segment", "segment_index": -1, "quote": "ii-V-I voicings"},
    {"field": "subtitle_segment", "segment_index": 0, "quote": "ii-V-I voicings"},
    {"field": "subtitle_segment", "segment_index": 2, "quote": "ii-V-I voicings"},
    {"field": "subtitle_segment", "segment_index": 1, "quote": "invented absent evidence"},
])
def test_context_references_must_resolve_exact_stored_comment_or_subtitle(corpus, reference):
    add_semantic_context(corpus)
    records = contextual_outcomes(export(corpus))
    source = next(record for record in records if record["post_id"] == "4")
    source["matches"][0]["similarities"][2]["candidate_refs"] = [reference]
    with pytest.raises(engage.StageGateError):
        import_outcomes(corpus, records)
    assert matching.state(corpus[0], corpus[1])["status"] == "pending"
    assert {row[0] for row in corpus[0].execute("SELECT creator_mentions_json FROM engage_tiktok_posts")} == {"{}"}


@pytest.mark.parametrize("dimension", ["subtopic", "technique", "learning_goal"])
@pytest.mark.parametrize("side,comment_id", [("source_refs", "nested-4"), ("candidate_refs", "nested-1")])
def test_comments_alone_cannot_establish_core_similarity_on_either_post(corpus, dimension, side, comment_id):
    add_semantic_context(corpus)
    records = contextual_outcomes(export(corpus))
    source = next(record for record in records if record["post_id"] == "4")
    similarity = source["matches"][0]["similarities"][0 if dimension == "subtopic" else 1]
    similarity["dimension"] = dimension
    similarity[side] = [{"field": "comment", "comment_id": comment_id, "quote": "intermediate players"}]
    with pytest.raises(engage.StageGateError, match="direct post-text references on both posts"):
        import_outcomes(corpus, records)


def test_supplementary_comments_are_valid_but_ambiguous_stored_ids_are_not():
    evidence = {"caption": JAZZ, "comments": [{"cid": "1", "text": "intermediate players"},
                                                {"cid": "2", "text": "a different audience response"}]}
    refs = [{"field": "caption", "quote": "ii-V-I voicings"},
            {"field": "comment", "comment_id": "1", "quote": "intermediate players"}]
    assert matching._refs(refs, evidence) == refs
    evidence["comments"].append({"id": "1", "comment_text": "This duplicate ID cannot select a unique comment."})
    with pytest.raises(ValueError, match="exactly one stored comment"):
        matching._refs(refs, evidence)


@pytest.mark.parametrize("stale_candidate", [False, True])
@pytest.mark.parametrize("mention_label", [None, "@Piano Archive 🎹"])
@pytest.mark.parametrize("corpus", ["live"], indirect=True)
def test_jazz_connections_flow_through_draft_review_presentation_and_handoff(corpus, stale_candidate, mention_label):
    conn, run_id, root = corpus
    if stale_candidate:
        import datetime as dt
        candidate = HELPERS["post_row"](conn, run_id, 1)
        evidence = json.loads(candidate["evidence_json"])
        evidence["observed_at"] = (dt.datetime.now().astimezone() - dt.timedelta(hours=2)).isoformat()
        evidence_hash = engage.json_hash(evidence)
        analysis_hash = engage.json_hash({"evidence_hash": evidence_hash, "analysis": json.loads(candidate["analysis_json"])})
        conn.execute("UPDATE engage_tiktok_posts SET evidence_json=?,evidence_hash=?,analysis_hash=? WHERE run_id=? AND post_id='1'",
                     (engage.canonical_json(evidence), evidence_hash, analysis_hash, run_id))
        conn.commit()
    queue = export(corpus)
    assert {row["creator"] for row in queue} == {"creator_a", "creator_b", "creator_k", "creator_l", "creator_m"}
    records = outcomes(queue)
    if mention_label is not None:
        next(item for item in records if item["post_id"] == "4")["matches"][0]["mention_label"] = mention_label
    result = import_outcomes(corpus, records)
    assert result == {"applied": 5, "matched_posts": 1, "creator_mentions": 2}
    draft_queue = root / "draft-queue.jsonl"
    assert engage.export_draft_queue(conn, run_id, draft_queue) == 5
    HELPERS["draft"](conn, run_id, root, [1, 2, 3, 4, 5])
    row = HELPERS["post_row"](conn, run_id, 4)
    assert "8.4/10" in row["draft_text"]
    assert f"{mention_label or '@creator_a'}:" in row["draft_text"] and "@creator_m:" in row["draft_text"]
    assert "@creator_b" not in row["draft_text"] and "@creator_k" not in row["draft_text"]
    assert engage.export_review_queue(conn, run_id, root / "reviews.jsonl") == 5
    review_queue = [json.loads(line) for line in (root / "reviews.jsonl").read_text(encoding="utf-8").splitlines()]
    source_review = next(item for item in review_queue if item["post_id"] == "4")
    assert source_review["creator_mentions"]["matches"][0]["similarities"][1]["candidate_refs"][0]["quote"] == "ii-V-I voicings"
    for post_id in (1, 2, 3, 4, 5):
        review = HELPERS["review_record"](conn, run_id, post_id)
        if post_id == 4:
            with pytest.raises(engage.StageGateError, match="creator_match_grounding"):
                engage.import_review_results(conn, run_id, write(corpus, [review], "critic.jsonl"), actor="codex-reviewer")
            review.update(creator_match_grounding="pass", creator_mention_usefulness="pass")
        engage.import_review_results(conn, run_id, write(corpus, [review], "critic.jsonl"), actor="codex-reviewer")
    shown = engage.present_response(conn, run_id, "4", presented_to="Taylor")
    assert shown["final_response"] == row["draft_text"]
    refreshed = HELPERS["post_row"](conn, run_id, 4)
    engage.authorize_response(conn, run_id, "4", authorized_by="Taylor", expected_draft_hash=refreshed["draft_hash"],
                              expected_review_hash=refreshed["review_hash"], expected_presentation_hash=shown["presentation_hash"],
                              approval_token=shown["approval_token"])
    if stale_candidate:
        with pytest.raises(engage.StageGateError, match="matched creator evidence is stale"):
            engage.handoff_publication(conn, run_id, "4")
        assert conn.execute("SELECT COUNT(*) FROM publication_queue").fetchone()[0] == 0
        return
    handoff = engage.handoff_publication(conn, run_id, "4")
    assert handoff["draft_hash"] == refreshed["draft_hash"]
    decision = json.loads(conn.execute("SELECT decision_json FROM publication_queue WHERE publication_id=?", (handoff["publication_id"],)).fetchone()[0])
    assert decision["creator_mentions"]["matches"][0]["creator_handle"] == "creator_a"
    stored_match = decision["creator_mentions"]["matches"][0]
    assert stored_match["mention_label"] == (mention_label or "@creator_a")
    assert stored_match["url"] == "https://www.tiktok.com/@creator_a/video/1"
    from tiktok_publication_adapter import load_approved_publication
    publication = load_approved_publication(root / "state.sqlite", handoff["publication_id"])
    assert publication["final_text"] == refreshed["draft_text"]
    assert engage.run_status(conn, run_id)["creator_matching"]["creator_mentions"] == 2
    conn.execute("UPDATE engage_tiktok_posts SET evidence_hash='changed' WHERE run_id=? AND post_id='1'", (run_id,))
    conn.commit()
    with pytest.raises(RuntimeError, match="stale") as failure:
        load_approved_publication(root / "state.sqlite", handoff["publication_id"])
    assert "creator mentions" in str(failure.value.__cause__)


def test_pending_matching_blocks_drafts_but_disabled_runs_are_unchanged(corpus):
    conn, run_id, root = corpus
    assert engage.export_draft_queue(conn, run_id, root / "legacy.jsonl") == 5
    export(corpus)
    with pytest.raises(engage.StageGateError, match="complete creator matching"):
        engage.export_draft_queue(conn, run_id, root / "pending.jsonl")
    with pytest.raises(engage.StageGateError, match="complete creator matching"):
        HELPERS["draft"](conn, run_id, root, [1])


@pytest.mark.parametrize("mutation", ["self", "duplicate_creator", "outside_run", "rock", "low_confidence", "only_topic", "forged_quote", "third_match", "scope", "missing_outcome", "reason_handle"])
def test_bad_match_batches_are_atomic(corpus, mutation):
    queue = export(corpus)
    records = outcomes(queue)
    source = next(record for record in records if record["post_id"] == "4")
    if mutation == "self": source["matches"][0]["post_id"] = "4"
    elif mutation == "duplicate_creator": source["matches"][1]["post_id"] = "1"
    elif mutation == "outside_run": source["matches"][0]["post_id"] = "999"
    elif mutation == "rock": source["matches"][0]["post_id"] = "2"
    elif mutation == "low_confidence": source["matches"][0]["confidence"] = 84
    elif mutation == "only_topic": source["matches"][0]["similarities"] = source["matches"][0]["similarities"][:1]
    elif mutation == "forged_quote": source["matches"][0]["similarities"][0]["candidate_refs"][0]["quote"] = "invented absent evidence"
    elif mutation == "third_match": source["matches"].append(selected("3"))
    elif mutation == "scope": source["scope_hash"] = "bad"
    elif mutation == "missing_outcome": records.pop()
    elif mutation == "reason_handle": source["matches"][0]["reason"] = "Mention @someone_else for even more tutorials."
    with pytest.raises(engage.StageGateError):
        import_outcomes(corpus, records)
    conn, run_id, _ = corpus
    assert matching.state(conn, run_id)["status"] == "pending"
    assert {row[0] for row in conn.execute("SELECT creator_mentions_json FROM engage_tiktok_posts")} == {"{}"}


def test_matching_is_immutable_and_idempotent_before_drafting(corpus):
    queue = export(corpus)
    records = outcomes(queue)
    import_outcomes(corpus, records)
    assert import_outcomes(corpus, records) == {"applied": 0, "already_complete": True}
    changed = deepcopy(records)
    changed[3]["matches"].pop()
    with pytest.raises(engage.StageGateError, match="immutable"):
        import_outcomes(corpus, changed)
    with pytest.raises(engage.StageGateError, match="frozen"):
        export(corpus, 1)


def test_only_positive_support_can_mention_or_be_matched(corpus):
    conn, run_id, _ = corpus
    row = HELPERS["post_row"](conn, run_id, 4)
    analysis = json.loads(row["analysis_json"])
    analysis["response_type"] = "clarifying_question"
    analysis_hash = engage.json_hash({"evidence_hash": row["evidence_hash"], "analysis": analysis})
    conn.execute("UPDATE engage_tiktok_posts SET analysis_json=?,analysis_hash=? WHERE run_id=? AND post_id='4'", (engage.canonical_json(analysis), analysis_hash, run_id))
    conn.commit()
    with pytest.raises(engage.StageGateError, match="positive-support"):
        import_outcomes(corpus, outcomes(export(corpus)))
    records = outcomes(export(corpus))
    next(item for item in records if item["post_id"] == "4")["matches"] = []
    import_outcomes(corpus, records)
    output = corpus[2] / "constructive-draft-queue.jsonl"
    engage.export_draft_queue(conn, run_id, output)
    source = next(json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
                  if json.loads(line)["post_id"] == "4")
    assert "Write only the score placeholder" not in source["prompt"]
    assert source["creator_mentions"]["matches"] == []


def test_no_match_remains_a_normal_comment_without_mentions(corpus):
    queue = export(corpus)
    records = outcomes(queue)
    for record in records: record["matches"] = []
    import_outcomes(corpus, records)
    HELPERS["draft"](corpus[0], corpus[1], corpus[2], [1, 2, 3, 4, 5])
    assert all("@" not in row[0] for row in corpus[0].execute("SELECT draft_text FROM engage_tiktok_posts"))


@pytest.mark.parametrize("label", ["@PianoArchive", "@Piano Archive 🎹", "@🎹", "@" + "a" * 64])
def test_observed_mention_label_is_preserved_exactly(label):
    assert matching._mention_label(label) == label


@pytest.mark.parametrize("label", [
    None, False, 12, "", "@", "creator_a", "@@a", "@a@b", "@ ", "@Name ",
    "@a\nb", "@a\rb", "@a\tb", "@a\x00b", "@a\u200bb", "@a\u202eb",
    "@a\u2067b", "@a\u2028b", "@a\u2029b", "@" + "a" * 65,
])
def test_mention_label_rejects_malformed_and_hidden_text(label):
    with pytest.raises(ValueError, match="mention_label"):
        matching._mention_label(label)


def test_invalid_label_rejects_entire_match_import(corpus):
    records = outcomes(export(corpus))
    next(item for item in records if item["post_id"] == "4")["matches"][0]["mention_label"] = "@Piano\n@unapproved"
    with pytest.raises(engage.StageGateError, match="mention_label"):
        import_outcomes(corpus, records)
    assert matching.state(corpus[0], corpus[1])["status"] == "pending"
    assert {row[0] for row in corpus[0].execute("SELECT creator_mentions_json FROM engage_tiktok_posts")} == {"{}"}


def test_native_labels_are_hash_bound_and_completed_selection_is_immutable(corpus):
    records = outcomes(export(corpus))
    chosen = next(item for item in records if item["post_id"] == "4")["matches"][0]
    chosen["mention_label"] = "@Piano Archive 🎹"
    import_outcomes(corpus, records)
    conn, run_id, _ = corpus
    original = matching.post_context(conn, run_id, "4")
    assert original["matches"][0]["mention_label"] == chosen["mention_label"]
    assert original["matches"][0]["creator_handle"] == "creator_a"
    assert import_outcomes(corpus, records)["already_complete"] is True
    chosen["mention_label"] = "@Another display label"
    with pytest.raises(engage.StageGateError, match="immutable"):
        import_outcomes(corpus, records)
    tampered = deepcopy(original)
    tampered["matches"][0]["mention_label"] = chosen["mention_label"]
    conn.execute("UPDATE engage_tiktok_posts SET creator_mentions_json=? WHERE run_id=? AND post_id='4'",
                 (matching.canonical(tampered), run_id))
    with pytest.raises(ValueError, match="changed"):
        matching.post_context(conn, run_id, "4")


def test_completed_legacy_match_documents_keep_existing_hashes(corpus):
    records = outcomes(export(corpus))
    import_outcomes(corpus, records)
    conn, run_id, _ = corpus
    before = {row["post_id"]: row["creator_mentions_json"] for row in conn.execute("SELECT post_id,creator_mentions_json FROM engage_tiktok_posts")}
    documents = matching.contexts(conn, run_id)
    assert all("mention_label" not in match for document in documents.values() for match in document["matches"])
    assert import_outcomes(corpus, records)["already_complete"] is True
    assert {row["post_id"]: row["creator_mentions_json"] for row in conn.execute("SELECT post_id,creator_mentions_json FROM engage_tiktok_posts")} == before


@pytest.mark.parametrize("mutation", ["label", "reason", "prefix_handle", "prefix_unicode", "repeated_suffix", "trailing_mention"])
def test_native_label_comment_requires_exact_suffix_and_no_extra_mentions(mutation):
    document = {"matches": [{"creator_handle": "creator_a", "mention_label": "@Piano Archive 🎹",
                              "reason": "Also teaches the same jazz chord changes."}]}
    base = "AI-assisted perspective: 8.2/10 - The slow chord changes are useful."
    comment = matching.render_comment(base, document)
    matching.validate_comment(comment, document)
    if mutation == "label":
        comment = comment.replace("@Piano Archive 🎹", "@creator_a")
    elif mutation == "reason":
        comment = comment.replace("same jazz", "different rock")
    elif mutation == "prefix_handle":
        comment = "@unapproved " + comment
    elif mutation == "prefix_unicode":
        comment = "@🎸 " + comment
    elif mutation == "repeated_suffix":
        comment += "\n" + matching.mention_suffix(document)
    else:
        comment += " @unapproved"
    with pytest.raises(ValueError, match="mentions differ"):
        matching.validate_comment(comment, document)


def test_unselected_unicode_mentions_cannot_enter_base_draft():
    with pytest.raises(ValueError, match="base text"):
        matching.render_comment("A useful lesson from @🎹", {"matches": []})


def test_unapproved_mentions_and_changed_evidence_are_rejected(corpus):
    conn, run_id, root = corpus
    import_outcomes(corpus, outcomes(export(corpus)))
    base = HELPERS["draft_record"](conn, run_id, 4)
    base["draft_text"] += " @unapproved"
    with pytest.raises(engage.StageGateError, match="base text"):
        engage.import_draft_results(conn, run_id, write(corpus, [base], "drafts.jsonl"), actor="codex-drafter")
    HELPERS["draft"](conn, run_id, root, [1, 2, 3, 4, 5])
    source = HELPERS["post_row"](conn, run_id, 4)
    assert matching.publication_context(conn, run_id, "4", source["draft_text"])
    with pytest.raises(ValueError, match="mentions differ"):
        matching.publication_context(conn, run_id, "4", source["draft_text"] + " @unapproved")
    conn.execute("UPDATE engage_tiktok_posts SET evidence_hash='changed' WHERE run_id=? AND post_id='1'", (run_id,))
    with pytest.raises(ValueError, match="scope changed"):
        matching.publication_context(conn, run_id, "4", source["draft_text"])


def test_cli_matching_commands_are_offline():
    assert "export-creator-matches" not in engage.BROWSER_REVALIDATION_COMMANDS
    assert "import-creator-matches" not in engage.BROWSER_REVALIDATION_COMMANDS
    args = engage.parse_args(["export-creator-matches", "--run-id", "run", "--file", "queue.jsonl", "--max-mentions", "2"])
    assert args.max_mentions == 2
    imported = engage.parse_args([
        "import-creator-matches", "--run-id", "run", "--file", "results.jsonl",
        "--actor", "antigravity-gemini31-matcher", "--native-probe", "probe-a.json",
        "--native-probe", "probe-b.json",
    ])
    assert imported.native_probe == [Path("probe-a.json"), Path("probe-b.json")]


@pytest.mark.parametrize("corpus", ["live"], indirect=True)
def test_new_live_matches_cannot_freeze_before_native_rehearsal(corpus):
    conn, run_id, _ = corpus
    records = outcomes(export(corpus))
    with pytest.raises(engage.StageGateError, match="--native-probe"):
        engage.import_creator_matches(conn, run_id, write(corpus, records), actor="codex-matcher")
    assert matching.state(conn, run_id)["status"] == "pending"
    assert {row[0] for row in conn.execute("SELECT creator_mentions_json FROM engage_tiktok_posts")} == {"{}"}
    assert not conn.execute("SELECT 1 FROM engage_tiktok_events WHERE stage='creator_matching' AND event='imported'").fetchone()


@pytest.mark.parametrize("corpus", ["live"], indirect=True)
def test_live_import_binds_observed_labels_and_preserves_matching_input(corpus):
    conn, run_id, _ = corpus
    records = outcomes(export(corpus))
    source = write(corpus, records)
    before = source.read_bytes()
    probe = synthetic_native_probe(corpus, records)
    report = json.loads(probe.read_text(encoding="utf-8"))
    report["observed_mentions"][0]["mention_label"] = "@Creator A Guitar"
    for case in report["cases"]:
        case["text"] = case["text"].replace("@creator_a", "@Creator A Guitar")
    probe.write_text(json.dumps(report), encoding="utf-8")
    result = engage.import_creator_matches(conn, run_id, source, actor="antigravity-gemini31-matcher", native_probes=[probe])
    assert result["matched_posts"] == 1
    document = matching.post_context(conn, run_id, "4")
    assert document["matches"][0]["mention_label"] == "@Creator A Guitar"
    assert document["matches"][0]["creator_handle"] == "creator_a"
    assert source.read_bytes() == before
    assert engage.import_creator_matches(conn, run_id, source, actor="antigravity-gemini31-matcher", native_probes=[probe])["already_complete"]


@pytest.mark.parametrize("corpus", ["live"], indirect=True)
def test_live_no_match_needs_no_native_probe(corpus):
    records = outcomes(export(corpus))
    for record in records:
        record["matches"] = []
    result = engage.import_creator_matches(corpus[0], corpus[1], write(corpus, records), actor="codex-matcher")
    assert result["creator_mentions"] == 0


@pytest.mark.parametrize("corpus", ["live"], indirect=True)
def test_completed_historical_live_matches_are_not_rewritten(corpus):
    conn, run_id, _ = corpus
    records = outcomes(export(corpus))
    # Reconstruct a historical LIVE selection created before probe-bound imports.
    matching.import_matches(conn, run_id, records, "codex-matcher")
    before = {row["post_id"]: row["creator_mentions_json"] for row in conn.execute("SELECT * FROM engage_tiktok_posts")}
    result = engage.import_creator_matches(conn, run_id, write(corpus, records), actor="codex-matcher")
    assert result == {"applied": 0, "already_complete": True}
    assert {row["post_id"]: row["creator_mentions_json"] for row in conn.execute("SELECT * FROM engage_tiktok_posts")} == before


def test_legacy_database_without_matching_schema_is_readable():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        assert matching.publication_context(conn, "legacy", "1", "Legacy text") is None
    finally:
        conn.close()


def test_new_matches_use_inline_composition_and_preserve_legacy_rendering(corpus):
    records = outcomes(export(corpus))
    import_outcomes(corpus, records)
    conn, run_id, _ = corpus
    docs = matching.contexts(conn, run_id)
    assert all(doc['render_format'] == 'inline_v1' for doc in docs.values())
    selected_doc = docs['4']
    rendered = matching.render_comment('Specific post observation.', selected_doc)
    assert '\n' not in rendered
    matching.validate_comment(rendered, selected_doc)
    # Simulate a previously completed, correctly hashed v1 legacy document.
    for post_id, doc in docs.items():
        doc.pop('render_format')
        doc.pop('match_hash')
        doc['match_hash'] = matching.digest(doc)
        conn.execute('UPDATE engage_tiktok_posts SET creator_mentions_json=? WHERE run_id=? AND post_id=?',
                     (json.dumps(doc), run_id, post_id))
    legacy = matching.contexts(conn, run_id)
    assert all('render_format' not in doc for doc in legacy.values())
    legacy_text = matching.render_comment('Specific post observation.', legacy['4'])
    assert '\n@creator_a:' in legacy_text
    matching.validate_comment(legacy_text, legacy['4'])
    assert import_outcomes(corpus, records)['applied'] == 0
