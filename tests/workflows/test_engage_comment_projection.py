"""Source AI queues must retain the discussion represented by stored aliases."""
from copy import deepcopy

import pytest

import engage_tiktok as engage
import engage_creator_matching as matching


@pytest.mark.parametrize("raw_children,normalized_children,expected_ids", [
    ([], [{"id": "a", "text": "Explain this chord transition."}], ["a"]),
    ([{"cid": "a", "text": "Explain this chord transition."}],
     [{"id": "b", "text": "How should I practice the rhythm?"}], ["a", "b"]),
    ([{"id": "a", "text": "Explain this chord transition."}],
     [{"id": "a", "text": "Explain this chord transition."}], ["a"]),
    ([{"id": "a", "text": "First stored version of this comment."}],
     [{"id": "a", "text": "Different stored version of this comment."}], ["a", "a"]),
])
def test_source_and_matcher_preserve_both_reply_branches(raw_children, normalized_children, expected_ids):
    packet = {"comments": [{"cid": "parent", "text": "Learning these guitar chords.",
                             "reply_comment": raw_children, "replies": normalized_children}]}
    original = deepcopy(packet)
    source = engage.compact_ai_evidence_projection(packet, evidence_hash="source-hash")
    matcher = matching.context_evidence(packet)
    for context in (source, matcher):
        replies = context["comments"][0]["replies"]
        assert [reply["comment_id"] for reply in replies] == expected_ids
        assert all(reply["parent_comment_id"] == "parent" for reply in replies)
    assert source["comment_and_reply_count"] == 1 + len(expected_ids)
    assert source["source_evidence_hash"] == "source-hash"
    assert packet == original


def test_nested_normalized_replies_retain_text_and_thread_signals_without_transport():
    packet = {"comments": [{"comment_id": "parent", "text": "Which frets should I use?",
        "reply_comment": [], "replies": [{"comment_id": "answer", "text": "Use the third fret first.",
            "author_handle": "creator", "likes": 0, "creator_liked": True, "creator_pinned": True,
            "reported_reply_count": 1, "avatar": "https://transport.invalid/avatar",
            "session_token": "transport-secret", "reply_comment": [],
            "replies": [{"comment_id": "thanks", "text": "The third fret explanation helped.",
                         "reply_to_comment_id": "answer", "author_handle": "learner"}]}]}]}
    source = engage.compact_ai_evidence_projection(packet, evidence_hash="source-hash")
    reply = source["comments"][0]["replies"][0]
    assert reply["author_handle"] == "creator"
    assert reply["likes"] == 0 and reply["reported_reply_count"] == 1
    assert reply["creator_liked"] is True and reply["creator_pinned"] is True
    nested = reply["replies"][0]
    assert nested["text"] == "The third fret explanation helped."
    assert nested["parent_comment_id"] == nested["reply_to_comment_id"] == "answer"
    assert source["comment_and_reply_count"] == 3
    assert "transport-secret" not in engage.canonical_json(source)
    assert "transport.invalid" not in engage.canonical_json(source)
