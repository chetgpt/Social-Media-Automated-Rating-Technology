"""Offline publication gates for optional same-run creator mentions."""

from copy import deepcopy
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

import tiktok_publication_adapter as adapter


TEXT = "AI-assisted: @other_creator has a related example."
MENTIONS = {
    "schema_version": "test-creator-mentions-v1",
    "run_id": "run-1",
    "post_id": "source-post",
    "matches": [{"post_id": "target-post", "creator": "other_creator"}],
}


@pytest.fixture
def matching_api(monkeypatch):
    """Keep these adapter tests independent of the matching policy itself."""
    module = ModuleType("engage_creator_matching")
    state = SimpleNamespace(document=deepcopy(MENTIONS), error=None, calls=[])

    def publication_context(conn, run_id, post_id, final_text):
        state.calls.append((conn, run_id, post_id, final_text))
        if state.error is not None:
            raise ValueError(state.error)
        return deepcopy(state.document)

    module.publication_context = publication_context
    monkeypatch.setitem(sys.modules, "engage_creator_matching", module)
    return state


def record():
    return {
        "engage_run_id": "run-1",
        "engage_post_id": "source-post",
        "draft_text": TEXT,
    }


def test_validated_mentions_must_match_the_reviewed_decision_and_source(matching_api):
    conn = object()
    adapter._validate_creator_mention_binding(
        conn,
        record(),
        decision={"creator_mentions": deepcopy(MENTIONS)},
        source_post={"creator_mentions_json": json.dumps(MENTIONS)},
    )
    assert matching_api.calls == [(conn, "run-1", "source-post", TEXT)]


@pytest.mark.parametrize("mutation", ["decision", "source", "missing_decision"])
def test_rewritten_mention_attestations_are_rejected(matching_api, mutation):
    decision = {"creator_mentions": deepcopy(MENTIONS)}
    stored = deepcopy(MENTIONS)
    if mutation == "decision":
        decision["creator_mentions"]["matches"][0]["creator"] = "changed_creator"
    elif mutation == "source":
        stored["matches"][0]["post_id"] = "another-run-post"
    else:
        decision.clear()
    with pytest.raises(RuntimeError, match="creator mention"):
        adapter._validate_creator_mention_binding(
            object(),
            record(),
            decision=decision,
            source_post={"creator_mentions_json": json.dumps(stored)},
        )


def test_legacy_run_needs_no_mention_column_or_extra_source_query(matching_api):
    matching_api.document = None
    # object() has no execute method: disabled legacy scope must not query
    # optional columns that may not exist in an old publication database.
    adapter._validate_creator_mention_binding(object(), record(), decision={})
    with pytest.raises(RuntimeError, match="no enabled run scope"):
        adapter._validate_creator_mention_binding(
            object(), record(), decision={"creator_mentions": MENTIONS}
        )


def submit_fixture(tmp_path, monkeypatch, matching_api, *, legacy=False):
    database = tmp_path / "publication.sqlite"
    document = None if legacy else deepcopy(MENTIONS)
    matching_api.document = document
    decision = {} if legacy else {"creator_mentions": document}
    text_hash = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE publication_queue (
                publication_id TEXT PRIMARY KEY, status TEXT,
                master_attempt_id TEXT, engage_run_id TEXT, engage_post_id TEXT,
                draft_text TEXT, draft_hash TEXT, decision_json TEXT, valid_until TEXT
            );
            CREATE TABLE engage_tiktok_posts (
                run_id TEXT, post_id TEXT, creator_mentions_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO publication_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "publication-1", "publishing", "attempt-1", "run-1",
                "source-post", TEXT, text_hash, json.dumps(decision), "2099-01-01T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO engage_tiktok_posts VALUES (?, ?, ?)",
            ("run-1", "source-post", json.dumps(document or {})),
        )
    monkeypatch.setattr(adapter, "ensure_analysis_schema", lambda _conn: None)
    monkeypatch.setattr(adapter, "attach_master_database", lambda *_args: "main")
    master_intents = []
    monkeypatch.setattr(
        adapter,
        "mark_master_submit_intent",
        lambda _conn, _schema, attempt_id: master_intents.append(attempt_id),
    )
    publication = {
        "publication_id": "publication-1",
        "master_attempt_id": "attempt-1",
        "final_text": TEXT,
        "final_text_hash": text_hash,
        "decision_json": json.dumps(decision),
        "valid_until": "2099-01-01T00:00:00Z",
    }
    return database, publication, master_intents


@pytest.mark.parametrize("deadline", [None, "", "not-a-date", "2035-01-01T11:59:59Z", "2035-01-01T12:00:00Z"])
def test_submit_intent_rejects_expired_or_invalid_deadline(tmp_path, monkeypatch, matching_api, deadline):
    database, publication, intents = submit_fixture(tmp_path, monkeypatch, matching_api)
    publication["valid_until"] = deadline
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE publication_queue SET valid_until=?", (deadline,))

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2035, 1, 1, 12, tzinfo=dt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(adapter.dt, "datetime", Clock)
    with pytest.raises(RuntimeError, match="freshness deadline expired before submit intent"):
        adapter.mark_publication_submit_intent(database, publication, master_database=tmp_path / "master.sqlite")
    assert intents == []
    assert "_submit_intent" not in publication
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT status, valid_until FROM publication_queue").fetchone() == ("publishing", deadline)


def test_deadline_extension_after_claim_is_rejected(tmp_path, monkeypatch, matching_api):
    database, publication, intents = submit_fixture(tmp_path, monkeypatch, matching_api)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE publication_queue SET valid_until='2099-01-02T00:00:00Z'")
    with pytest.raises(RuntimeError, match="freshness deadline changed before submit intent"):
        adapter.mark_publication_submit_intent(database, publication, master_database=tmp_path / "master.sqlite")
    assert intents == []
    assert "_submit_intent" not in publication


@pytest.mark.parametrize("legacy", [False, True])
def test_submit_intent_preserves_valid_mentions_and_legacy_path(
    tmp_path, monkeypatch, matching_api, legacy
):
    database, publication, intents = submit_fixture(
        tmp_path, monkeypatch, matching_api, legacy=legacy
    )
    adapter.mark_publication_submit_intent(
        database, publication, master_database=tmp_path / "unused-master.sqlite"
    )
    assert intents == ["attempt-1"]
    assert publication["_submit_intent"] is True
    assert matching_api.calls[-1][1:] == ("run-1", "source-post", TEXT)


@pytest.mark.parametrize(
    "mutation", ["stale_target", "changed_decision", "changed_source", "changed_text"]
)
def test_changed_mentions_after_claim_block_before_any_master_submit_intent(
    tmp_path, monkeypatch, matching_api, mutation
):
    database, publication, intents = submit_fixture(tmp_path, monkeypatch, matching_api)
    if mutation == "stale_target":
        matching_api.error = "same-run target evidence hash changed"
    elif mutation == "changed_text":
        publication["final_text"] += " @unapproved_creator"
    else:
        changed = deepcopy(MENTIONS)
        changed["matches"][0]["creator"] = "unapproved_creator"
        with sqlite3.connect(database) as conn:
            if mutation == "changed_decision":
                conn.execute(
                    "UPDATE publication_queue SET decision_json=?",
                    (json.dumps({"creator_mentions": changed}),),
                )
            else:
                conn.execute(
                    "UPDATE engage_tiktok_posts SET creator_mentions_json=?",
                    (json.dumps(changed),),
                )
    with pytest.raises(RuntimeError, match="creator mention"):
        adapter.mark_publication_submit_intent(
            database, publication, master_database=tmp_path / "unused-master.sqlite"
        )
    assert intents == []
    assert "_submit_intent" not in publication
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT status FROM publication_queue").fetchone()[0] == "publishing"
