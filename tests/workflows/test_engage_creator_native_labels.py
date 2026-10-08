"""Offline probe binding validates scope and never changes workflow state."""
from copy import deepcopy
import json
import sqlite3

import pytest

import engage_creator_matching as matching
from engage_creator_native_labels import bind_native_labels


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE engage_tiktok_runs (run_id TEXT PRIMARY KEY, expected_account TEXT);
        CREATE TABLE engage_tiktok_posts (
            run_id TEXT, post_id TEXT, url TEXT, evidence_ready INTEGER,
            evidence_json TEXT, evidence_hash TEXT, analysis_json TEXT, analysis_hash TEXT,
            PRIMARY KEY(run_id, post_id));
    """)
    conn.executemany("INSERT INTO engage_tiktok_runs VALUES (?,?)", [("run", "@OPERATOR"), ("other", "operator")])
    for run_id, post_id, handle, ready in [
        ("run", "1", "creator_a", 1), ("run", "2", "creator_b", 1),
        ("run", "3", "creator_c", 1), ("other", "4", "outside", 1),
        ("run", "5", "unfinished", 0),
    ]:
        evidence = {"creator": handle, "caption": "Guitar tutorial"}
        analysis = {"response_type": "positive_support"}
        evidence_hash = matching.digest(evidence)
        conn.execute("INSERT INTO engage_tiktok_posts VALUES (?,?,?,?,?,?,?,?)", (
            run_id, post_id, f"https://www.tiktok.com/@{handle}/video/{post_id}", ready,
            matching.canonical(evidence), evidence_hash, matching.canonical(analysis),
            matching.digest({"evidence_hash": evidence_hash, "analysis": analysis}),
        ))
    conn.commit()
    conn.close()
    # The helper works on a genuinely read-only connection, including SQLite's
    # own denial of any accidental UPDATE, schema change, or transaction write.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn, tmp_path
    conn.close()


def records(two=False):
    return [{"post_id": "1", "run_id": "run", "matches": [
        {"post_id": "2", "similarities": [{"nested": ["unchanged"]}]},
        *([{"post_id": "3"}] if two else []),
    ]}]


def report(two=False):
    handles = ["creator_b", *(["creator_c"] if two else [])]
    labels = ["@Ben Guitar 🎸", *(["@C Guitar"] if two else [])]
    return {
        "schema_version": "engage-mentions-probe-v1", "status": "passed",
        "publication_enabled": False, "blocked_publish_requests": 0,
        "editor_cleared": True, "temporary_tab_closed": True,
        "expected_account": "operator", "observed_account": "operator",
        "url": "https://www.tiktok.com/@creator_a/video/1", "handles": handles,
        "observed_mentions": [{"creator_handle": handle, "mention_label": label}
                              for handle, label in zip(handles, labels)],
        "cases": [{"case": index, "status": "passed", "text": " ".join(
            f"{label}: Unpublished creator mention verification." for label in labels)}
            for index in (1, 2)],
    }


def save(corpus, data, name="probe.json"):
    path = corpus[1] / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("two", [False, True])
def test_binds_one_or_two_exact_labels_without_mutating_inputs_or_database(corpus, two):
    conn, _ = corpus
    original = records(two)
    before = deepcopy(original)
    database = list(conn.iterdump())
    queries = []
    conn.set_trace_callback(queries.append)
    result = bind_native_labels(conn, "run", original, [save(corpus, report(two))])
    conn.set_trace_callback(None)
    assert result[0]["matches"][0]["mention_label"] == "@Ben Guitar 🎸"
    if two:
        assert result[0]["matches"][1]["mention_label"] == "@C Guitar"
    assert all(query.startswith("SELECT ") for query in queries)
    assert conn.total_changes == 0
    assert list(conn.iterdump()) == database
    assert original == before
    result[0]["matches"][0]["similarities"][0]["nested"].append("copy only")
    assert original == before


def test_reuses_label_observed_on_another_evidence_ready_source_in_same_run(corpus):
    data = report()
    data["url"] = "https://www.tiktok.com/@creator_c/video/3"
    result = bind_native_labels(corpus[0], "run", records(), [save(corpus, data)])
    assert result[0]["matches"][0]["mention_label"] == "@Ben Guitar 🎸"


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", "wrong"), ("status", "blocked"), ("status", "running"),
    ("publication_enabled", True), ("publication_enabled", 0),
    ("blocked_publish_requests", 1), ("blocked_publish_requests", False),
    ("blocked_publish_requests", 0.0), ("blocked_publish_requests", "0"),
    ("editor_cleared", False), ("editor_cleared", 1), ("temporary_tab_closed", False),
    ("expected_account", "outsider"), ("observed_account", "outsider"),
    ("expected_account", "@operator"), ("observed_account", "OPERATOR"),
    ("url", "https://www.tiktok.com/@outside/video/4"),
    ("url", "https://www.tiktok.com/@unfinished/video/5"),
    ("url", "https://www.tiktok.com/@creator_a/video/1?token=secret"),
    ("url", "https://www.tiktok.com/@wrong_owner/video/1"),
    ("handles", []), ("handles", ["creator_b", "creator_c", "creator_a"]),
    ("handles", ["creator_b", "creator_b"]), ("handles", ["@creator_b"]),
    ("handles", ["CREATOR_B"]), ("handles", ["invalid handle"]),
    ("observed_mentions", []),
    ("observed_mentions", [{"creator_handle": "creator_c", "mention_label": "@C"}]),
    ("observed_mentions", [{"creator_handle": "creator_b", "mention_label": "@B\nsecret"}]),
    ("observed_mentions", [{"creator_handle": "creator_b", "mention_label": "@B@C"}]),
    ("observed_mentions", [{"creator_handle": "creator_b", "mention_label": "no-at-sign"}]),
    ("cases", []), ("cases", [{"case": 1, "status": "passed", "text": "@Ben Guitar 🎸"}]),
])
def test_rejects_failed_incomplete_or_out_of_scope_report(corpus, field, value):
    data = report()
    data[field] = value
    original = records()
    before = deepcopy(original)
    with pytest.raises(ValueError) as error:
        bind_native_labels(corpus[0], "run", original, [save(corpus, data)])
    assert "secret" not in str(error.value)
    assert len(str(error.value)) < 250
    assert original == before
    assert corpus[0].total_changes == 0


@pytest.mark.parametrize("field", [
    "schema_version", "status", "publication_enabled", "blocked_publish_requests",
    "editor_cleared", "temporary_tab_closed", "expected_account", "observed_account",
    "url", "handles", "observed_mentions", "cases",
])
def test_missing_required_report_field_fails_closed(corpus, field):
    data = report()
    del data[field]
    with pytest.raises(ValueError):
        bind_native_labels(corpus[0], "run", records(), [save(corpus, data)])


@pytest.mark.parametrize(("field", "value"), [
    ("status", "failed"), ("case", 2), ("case", True), ("case", "1"),
    ("text", "No actual label"), ("text", "@Ben Guitar 🎸 repeated @Ben Guitar 🎸"),
    ("text", "@Ben Guitar 🎸\nSecond paragraph"),
    ("text", "@Ben Guitar 🎸\u2028Second paragraph"), ("text", None),
])
def test_rejects_invalid_composition_rehearsal(corpus, field, value):
    data = report()
    data["cases"][0][field] = value
    with pytest.raises(ValueError):
        bind_native_labels(corpus[0], "run", records(), [save(corpus, data)])


def test_two_observations_cannot_repeat_one_handle(corpus):
    data = report(True)
    data["observed_mentions"][1] = deepcopy(data["observed_mentions"][0])
    with pytest.raises(ValueError, match="observed creators"):
        bind_native_labels(corpus[0], "run", records(True), [save(corpus, data)])


def test_each_selected_creator_requires_report_binding(corpus):
    with pytest.raises(ValueError, match="missing a selected creator"):
        bind_native_labels(corpus[0], "run", records(True), [save(corpus, report())])


def test_multiple_reports_bind_choices_and_identical_labels_can_repeat(corpus):
    first = save(corpus, report(), "first.json")
    both = save(corpus, report(True), "both.json")
    result = bind_native_labels(corpus[0], "run", records(True), [first, both])
    assert [match["mention_label"] for match in result[0]["matches"]] == ["@Ben Guitar 🎸", "@C Guitar"]


def test_conflicting_report_labels_fail(corpus):
    first = save(corpus, report(), "first.json")
    data = report()
    data["observed_mentions"][0]["mention_label"] = "@Changed label"
    for case in data["cases"]:
        case["text"] = case["text"].replace("@Ben Guitar 🎸", "@Changed label")
    with pytest.raises(ValueError, match="conflicting labels"):
        bind_native_labels(corpus[0], "run", records(), [first, save(corpus, data, "second.json")])


def test_existing_exact_label_is_preserved_but_conflicting_supplied_label_fails(corpus):
    original = records()
    original[0]["matches"][0]["mention_label"] = "@Ben Guitar 🎸"
    path = save(corpus, report())
    assert bind_native_labels(corpus[0], "run", original, [path]) == original
    original[0]["matches"][0]["mention_label"] = "@Wrong"
    with pytest.raises(ValueError, match="supplied creator label|Supplied creator label"):
        bind_native_labels(corpus[0], "run", original, [path])


@pytest.mark.parametrize(("scope", "field", "value"), [
    ("source", "run_id", "other"), ("source", "post_id", "4"),
    ("source", "post_id", "5"), ("target", "post_id", "4"),
    ("target", "post_id", "5"), ("target", "post_id", "1"),
    ("target", "creator_handle", "wrong_owner"),
])
def test_choices_require_same_run_ready_posts_and_exact_nonself_owner(corpus, scope, field, value):
    original = records()
    target = original[0] if scope == "source" else original[0]["matches"][0]
    target[field] = value
    with pytest.raises(ValueError):
        bind_native_labels(corpus[0], "run", original, [save(corpus, report())])


def test_duplicate_candidate_owner_fails(corpus):
    original = records()
    original[0]["matches"].append(deepcopy(original[0]["matches"][0]))
    with pytest.raises(ValueError, match="duplicate creators"):
        bind_native_labels(corpus[0], "run", original, [save(corpus, report())])


@pytest.mark.parametrize("payload", ["not json SECRET", '{"status":"passed","status":"SECRET"}', "[]", "[" * 2000])
def test_invalid_report_errors_never_echo_file_contents(corpus, payload):
    path = corpus[1] / "report.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        bind_native_labels(corpus[0], "run", records(), [path])
    assert "SECRET" not in str(error.value)
    assert len(str(error.value)) < 250


def test_missing_report_and_unknown_run_fail(corpus):
    with pytest.raises(ValueError, match="unreadable"):
        bind_native_labels(corpus[0], "run", records(), [corpus[1] / "missing.json"])
    with pytest.raises(ValueError, match="existing run"):
        bind_native_labels(corpus[0], "missing", records(), [save(corpus, report())])
    with pytest.raises(ValueError, match="at least one"):
        bind_native_labels(corpus[0], "run", records(), [])


def test_no_match_records_remain_unchanged(corpus):
    original = [{"post_id": "1", "matches": [], "no_match_reason": "No supported match."}]
    assert bind_native_labels(corpus[0], "run", original, [save(corpus, report())]) == original


@pytest.mark.parametrize(("post_id", "field", "value", "message"), [
    ("1", "evidence_hash", "changed", "evidence hash changed"),
    ("2", "analysis_hash", "changed", "analysis hash changed"),
    ("2", "url", "https://www.tiktok.com/@wrong_owner/video/2", "owner does not match"),
    ("2", "evidence_json", "not JSON SECRET", "source JSON is invalid"),
])
def test_stored_source_and_candidate_integrity_is_checked(corpus, post_id, field, value, message):
    # Corrupt a synthetic fixture through a separate connection; the helper
    # itself remains read-only and must reject the now-inconsistent evidence.
    with sqlite3.connect(corpus[1] / "workflow.sqlite3") as writer:
        writer.execute(f"UPDATE engage_tiktok_posts SET {field}=? WHERE run_id='run' AND post_id=?", (value, post_id))
    with pytest.raises(ValueError, match=message) as error:
        bind_native_labels(corpus[0], "run", records(), [save(corpus, report())])
    assert "SECRET" not in str(error.value)
    assert corpus[0].total_changes == 0


def test_nonnumeric_stored_url_is_not_accepted_as_a_probe_source(corpus):
    data = report()
    data["url"] = "https://www.tiktok.com/@creator_c/video/invalid"
    with sqlite3.connect(corpus[1] / "workflow.sqlite3") as writer:
        writer.execute("UPDATE engage_tiktok_posts SET post_id='invalid',url=? WHERE post_id='3'", (data["url"],))
    with pytest.raises(ValueError, match="numeric TikTok post"):
        bind_native_labels(corpus[0], "run", records(), [save(corpus, data)])


def test_malformed_path_fails_with_bounded_error(corpus):
    with pytest.raises(ValueError, match="report path is invalid"):
        bind_native_labels(corpus[0], "run", records(), [None])


@pytest.mark.parametrize("labels", [
    ["@Ben", "@Ben Guitar"], ["@Ben Guitar", "@Ben"], ["@Ben Guitar", "@Ben Guitar"],
])
@pytest.mark.parametrize("reverse_observations", [False, True])
def test_overlapping_or_identical_labels_follow_handle_order(corpus, labels, reverse_observations):
    data = report(True)
    for mention, label in zip(data["observed_mentions"], labels):
        mention["mention_label"] = label
    if reverse_observations:
        data["observed_mentions"].reverse()
    for case in data["cases"]:
        case["text"] = "Composer rehearsal: " + " ".join(
            f"{label}: Unpublished creator connection verification." for label in labels
        )
    result = bind_native_labels(corpus[0], "run", records(True), [save(corpus, data)])
    assert [match["mention_label"] for match in result[0]["matches"]] == labels


@pytest.mark.parametrize("text", [
    "@outsider: Extra mention before @Ben Guitar 🎸: Native label rehearsal.",
    "@Ben Guitar 🎸: Native label rehearsal. @outsider: Extra mention after.",
    "@Ben Guitar 🎸: Native label rehearsal. contact@outsider",
    "@Ben Guitar 🎸 Extra: Label prefix with unexpected suffix.",
    "@Ben Guitar 🎸: Native label rehearsal. @Ben Guitar 🎸: Duplicate mention.",
])
def test_rehearsal_rejects_extra_unselected_or_prefix_only_mentions(corpus, text):
    data = report()
    data["cases"][0]["text"] = text
    with pytest.raises(ValueError, match="exactly the requested labels in order"):
        bind_native_labels(corpus[0], "run", records(), [save(corpus, data)])


@pytest.mark.parametrize("text", [
    "@C Guitar: Rehearsal. @Ben Guitar 🎸: Rehearsal.",
    "@Ben Guitar 🎸: Rehearsal. @outsider: Unselected. @C Guitar: Rehearsal.",
    "@Ben Guitar 🎸: Rehearsal with missing second creator.",
])
def test_rehearsal_rejects_reordered_missing_or_interleaved_mentions(corpus, text):
    data = report(True)
    data["cases"][0]["text"] = text
    with pytest.raises(ValueError, match="exactly the requested labels in order"):
        bind_native_labels(corpus[0], "run", records(True), [save(corpus, data)])
