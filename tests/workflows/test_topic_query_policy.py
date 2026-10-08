import asyncio
import json
import sqlite3

import pytest

from engage_tiktok import (
    StageGateError,
    collect_exact,
    create_run,
    ensure_schema,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


def test_new_topic_run_freezes_exact_query_policy():
    connection = _connection()
    ensure_schema(connection)

    run_id = create_run(
        connection,
        project="exact-topic-policy",
        topic="mr diy",
        requested_count=1,
        max_comments=20,
        max_pages=4,
        workflow="listen",
    )

    run = connection.execute(
        "SELECT topic_query_policy FROM engage_tiktok_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    created = connection.execute(
        """
        SELECT json_extract(payload_json, '$.topic_query_policy')
        FROM engage_tiktok_events
        WHERE run_id=? AND stage='run' AND event='created'
        """,
        (run_id,),
    ).fetchone()

    assert run["topic_query_policy"] == "exact"
    assert created[0] == "exact"


def test_legacy_policy_migration_preserves_only_topic_expansion():
    connection = _connection()
    connection.execute(
        """
        CREATE TABLE engage_tiktok_runs (
            run_id TEXT PRIMARY KEY,
            source_mode TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO engage_tiktok_runs VALUES (?, ?)",
        (("topic-run", "topic"), ("creator-run", "creator"), ("url-run", "url")),
    )

    ensure_schema(connection)

    policies = dict(
        connection.execute(
            "SELECT run_id, topic_query_policy FROM engage_tiktok_runs"
        ).fetchall()
    )
    assert policies == {
        "topic-run": "related_variants_v1",
        "creator-run": "exact",
        "url-run": "exact",
    }


def test_query_policy_drift_fails_before_browser_preflight():
    connection = _connection()
    ensure_schema(connection)
    run_id = create_run(
        connection,
        project="query-policy-drift",
        topic="mr diy",
        requested_count=1,
        max_comments=20,
        max_pages=4,
        workflow="listen",
    )
    connection.execute(
        "UPDATE engage_tiktok_runs SET topic_query_policy='related_variants_v1' "
        "WHERE run_id=?",
        (run_id,),
    )
    connection.commit()

    class Preflight:
        async def ensure_ready(self):
            raise AssertionError("policy drift must fail before browser preflight")

    class Collector:
        async def collect(self, **kwargs):
            raise AssertionError("policy drift must fail before collection")

    with pytest.raises(StageGateError, match="topic-query policy binding"):
        asyncio.run(
            collect_exact(
                connection,
                run_id=run_id,
                preflight=Preflight(),
                collector=Collector(),
            )
        )


def test_legacy_policy_is_forwarded_to_the_production_collector_path():
    connection = _connection()
    ensure_schema(connection)
    run_id = create_run(
        connection,
        project="legacy-query-policy",
        topic="mr diy",
        requested_count=1,
        max_comments=20,
        max_pages=4,
        workflow="listen",
    )
    created = connection.execute(
        """
        SELECT event_id, payload_json
        FROM engage_tiktok_events
        WHERE run_id=? AND stage='run' AND event='created'
        """,
        (run_id,),
    ).fetchone()
    payload = json.loads(created["payload_json"])
    payload.pop("topic_query_policy")
    connection.execute(
        "UPDATE engage_tiktok_events SET payload_json=? WHERE event_id=?",
        (json.dumps(payload), created["event_id"]),
    )
    connection.execute(
        "UPDATE engage_tiktok_runs SET topic_query_policy='related_variants_v1' "
        "WHERE run_id=?",
        (run_id,),
    )
    connection.commit()

    calls = []

    class Preflight:
        async def ensure_ready(self):
            calls.append("preflight")
            return {
                "reachable": True,
                "tiktok_authenticated": True,
                "observed_account": "creator",
            }

    class Collector:
        async def collect(
            self,
            *,
            topic,
            requested_count,
            max_comments,
            max_pages,
            topic_query_policy,
        ):
            del topic, requested_count, max_comments, max_pages
            calls.append(topic_query_policy)
            raise RuntimeError("stop after observing the saved policy")

    with pytest.raises(RuntimeError, match="saved policy"):
        asyncio.run(
            collect_exact(
                connection,
                run_id=run_id,
                preflight=Preflight(),
                collector=Collector(),
            )
        )

    assert calls == ["preflight", "related_variants_v1"]
