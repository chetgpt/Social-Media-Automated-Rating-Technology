"""Query-only, stdlib readers for already-collected TikTok discovery evidence.

Filters select posts by saved run membership; all observations of each selected
post in the explicitly opened database are retained. Stored source paths are
provenance only and are never opened. Nothing in this module collects data.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = "music-discovery-tiktok-source-v1"
_POST_URL = re.compile(r"/@([A-Za-z0-9_.]{1,64})/(video|photo)/([0-9]+)/*$")
_HANDLE = re.compile(r"[A-Za-z0-9_.]{1,64}$")
_GOOD_POST_STATUSES = frozenset({
    "collected", "analyzed", "skipped", "drafted", "reviewed", "review_rejected",
    "stored", "authorized", "handed_off", "published", "publication_failed",
    "publication_uncertain",
})
_METRICS = ("views", "likes", "reported_comments", "shares", "saves", "followers")
# Closed recursive metadata vocabulary: no URLs, transport objects, user/contact
# profiles, cookies, headers, payloads, lyrics or media can enter the projection.
_MUSIC_KEYS = frozenset("""
schema_version status reason source provider observed_at music_observed_at
music_evidence_hash result_hash enrichment_hash resolution_hash platform_music
platform_contained_recording platform_audio tt2dsp_resolution tt2dsp catalogs
musicbrainz apple_itunes_lookup identity acoustic_verification verified lyrics
configured_catalogs music_id title author artist album album_title album_id
recording_id recording_mbid artist_mbid artist_id name sort_name disambiguation
join_phrase artist_credit artists is_original original isrc isrcs duration_ms
post_duration_ms music_duration_ms fields field_provenance provenance availability
input_basis source_field source_path declared_by terminal attempted not_attempted
title_status author_status artist_status album_status music_id_status
first_release_date release_date releases release_mbid release_id release_title
country date track_id apple_track_id spotify_track_id storefront platform
platform_id dsp_id provider_id requested_id requested_track_id resolved_track_id
match matched selected_candidate_rank selected_recording_mbid candidates rank
provider_score match_reasons decision top_score runner_up_score score_margin
leading_candidate_rank missing_fields query type candidate_limit match_policy
cache_hit circuit_open catalog_identity_status identity_status source_status
provider_total_count provider_returned_count retained_candidate_count
invalid_candidate_count provider_results_truncated code http_status
retry_after_seconds version method configured_providers recording title_match
artist_match duration_match isrc_match score reasons release_group_mbid
result selected input_links dsp_links song_id provider_track_id collection_id
genre explicitness identification_basis relationship acoustic_recognition_performed
field_availability limitations
""".split())


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    """Keep semantic scalar text, never serialize arbitrary nested payloads."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    result = str(value).strip()
    def clean_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group())
            host = (parsed.hostname or "").casefold()
            if parsed.username or parsed.password or any(word in host for word in (
                "tiktokcdn", "byteoversea", "ibytedtos", "muscdn",
            )) or re.search(r"(?:token|signature|authorization|cookie|x-expires|x-amz)",
                            parsed.query, re.I):
                return "[transport URL removed]"
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            return "[invalid URL removed]"
    result = re.sub(r"https?://[^\s<>\"']+", clean_url, result)
    result = re.sub(r"(?i)\b(?:bearer\s+|(?:access_token|refresh_token|authorization|cookie)\s*[:=]\s*)[^\s,;]+",
                    "[credential removed]", result)
    return "".join(char for char in result if char in "\n\t" or ord(char) >= 32)


def _metadata(value: Any, depth: int = 0) -> Any:
    if depth > 16:
        return None
    if isinstance(value, dict):
        return {key: _metadata(item, depth + 1) for key, item in value.items()
                if key in _MUSIC_KEYS}
    if isinstance(value, list):
        return [_metadata(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _time(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        if re.fullmatch(r"[0-9]{9,13}", raw):
            instant = datetime.fromtimestamp(float(raw) / (1000 if len(raw) == 13 else 1), timezone.utc)
        else:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return ""  # Unknown timezone cannot establish a dated observation.
        return instant.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def _creator(value: Any) -> str:
    raw = _text(value).lstrip("@").casefold()
    if raw.startswith("https://"):
        parsed = urlsplit(raw)
        if parsed.hostname not in {"tiktok.com", "www.tiktok.com"}:
            return ""
        raw = parsed.path.strip("/").lstrip("@").casefold()
    return raw if _HANDLE.fullmatch(raw) else ""


def _canonical_url(value: Any, post_id: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(value or ""))
        matched = _POST_URL.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not matched or matched[3] != post_id):
            return "", ""
        owner = matched[1].casefold()
        return f"https://www.tiktok.com/@{owner}/{matched[2]}/{post_id}", owner
    except (TypeError, ValueError):
        return "", ""


def _comments(value: Any, parent: str = "", depth: int = 0) -> list[dict[str, str]]:
    if not isinstance(value, list) or depth > 32:
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        identifier = _text(item.get("id") or item.get("cid") or item.get("comment_id"))
        text = _text(item.get("text") or item.get("comment_text"))
        parent_id = _text(item.get("parent_id") or item.get("reply_id") or parent)
        if parent_id == "0":
            parent_id = parent
        if text:
            result.append({"id": identifier, "text": text, "parent_id": parent_id})
        result.extend(_comments(item.get("replies") or item.get("reply_comment"), identifier, depth + 1))
    return result


def _projection(evidence: dict[str, Any], url: str, creator: str,
                fallback: dict[str, Any]) -> dict[str, Any]:
    identity = evidence.get("creator_identity")
    identity = identity if isinstance(identity, dict) else {}
    raw_metrics = evidence.get("metrics")
    raw_metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    availability = evidence.get("metric_availability")
    availability = availability if isinstance(availability, dict) else {}
    metrics, statuses = {}, {}
    for key in _METRICS:
        value = raw_metrics.get(key)
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0)
        status = _text(availability.get(key))
        # Never promote a zero placeholder whose source says it is unavailable.
        statuses[key] = status or ("available" if valid else "missing_from_source")
        metrics[key] = value if valid and statuses[key] == "available" else None
        if statuses[key] == "available" and not valid:
            statuses[key] = "invalid_or_missing_value"
    comments_status = evidence.get("comments_status")
    comments_status = comments_status if isinstance(comments_status, dict) else {}
    return {
        "post_id": _text(evidence.get("post_id")), "url": url, "creator": creator,
        "creator_identity": {
            "id": _text(identity.get("id") or fallback.get("creator_user_id")),
            "sec_uid": _text(identity.get("sec_uid") or fallback.get("creator_sec_uid")),
        },
        "creator_display_name": _text(evidence.get("creator_display_name")),
        "caption": _text(evidence.get("caption")),
        "caption_status": _text(evidence.get("caption_status")) or "unknown",
        "transcript": _text(evidence.get("transcript")),
        "transcript_status": _text(evidence.get("transcript_status")) or "unknown",
        "transcript_language": _text(evidence.get("transcript_language")),
        "metrics": metrics, "metric_availability": statuses,
        "comments": _comments(evidence.get("comments")),
        "comments_status": {key: (value if isinstance(value, bool) else _text(value))
                            for key, value in comments_status.items()
                            if key in {"ok", "complete", "exhausted", "limit_reached", "has_more", "status", "source"}},
        "music_evidence": _metadata(evidence.get("music_evidence", {})),
    }


def _decode(raw: Any) -> dict[str, Any] | None:
    def finite_float(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        return value
    try:
        value = json.loads(raw or "{}", parse_float=finite_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def _add_observation(record: dict[str, Any], row: dict[str, Any], reference: dict[str, Any]) -> None:
    evidence = _decode(row.get("evidence_json"))
    problems = []
    if not evidence:
        problems.append("empty_or_invalid_evidence")
    digest = _hash(evidence) if evidence is not None else ""
    stored_hash = str(row.get("evidence_hash") or "").strip().casefold()
    reference["evidence_hash"] = digest
    reference["stored_evidence_hash"] = stored_hash
    if stored_hash and stored_hash != digest:
        problems.append("evidence_hash_mismatch")
    if row.get("evidence_ready", 1) != 1:
        problems.append("row_not_evidence_ready")
    status = row.get("status")
    if status is not None and status not in _GOOD_POST_STATUSES:
        problems.append("incompatible_post_status")
    evidence = evidence or {}
    post_id = str(record["post_id"])
    if not post_id.isdigit() or _text(evidence.get("post_id")) != post_id:
        problems.append("post_identity_mismatch")
    if evidence.get("platform", "tiktok") != "tiktok":
        problems.append("unsupported_platform")
    if evidence.get("evidence_ready") is not True:
        problems.append("evidence_not_ready")
    url, owner = _canonical_url(evidence.get("url"), post_id)
    creator = _creator(evidence.get("creator"))
    if not url or not creator or owner != creator:
        problems.append("invalid_canonical_identity")
    row_url = row.get("url") or row.get("canonical_url")
    if row_url:
        bound_url, bound_owner = _canonical_url(row_url, post_id)
        if not bound_url or bound_owner != owner:
            problems.append("row_identity_conflict")
    observed_at = _time(evidence.get("observed_at"))
    row_time = _time(row.get("observed_at"))
    if row_time and observed_at and row_time != observed_at:
        problems.append("observation_time_conflict")
    observed_at = observed_at or row_time
    if not observed_at:
        problems.append("missing_observation_time")
    reference["observed_at"] = observed_at
    reference["integrity"] = "invalid" if problems else "verified" if stored_hash else "computed_unstored_hash"
    reference["reasons"] = problems
    record["references"].append(reference)
    record["reasons"].extend(problems)
    if problems:
        return
    # The raw packet contains the observation timestamp. Copying it into a new
    # project/run is another reference, not a new audience measurement.
    if any(item["evidence_hash"] == digest for item in record["observations"]):
        return
    published = evidence.get("published_at")
    published_at = _time(published) or (_text(published) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", _text(published)) else "")
    record["observations"].append({
        "observation_id": _hash([post_id, digest, observed_at]),
        "observed_at": observed_at, "published_at": published_at,
        "evidence_hash": digest,
        "projection": _projection(evidence, url, creator, row),
    })


def _table_columns(conn: sqlite3.Connection, name: str, required: Sequence[str] = (),
                   *, optional: bool = False) -> set[str]:
    found = conn.execute("SELECT type, sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    if found is None and optional:
        return set()
    if found is None or found[0] != "table" or "CREATE VIRTUAL TABLE" in str(found[1]).upper():
        raise ValueError("Unsupported TikTok source schema: expected ordinary tables")
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
    if not set(required).issubset(columns):
        raise ValueError("Unsupported TikTok source schema: required columns missing")
    return columns


def _values(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{name} must be a sequence of exact filter values")
    normalized = tuple(dict.fromkeys(str(value).strip().casefold() for value in values))
    if any(not value for value in normalized):
        raise ValueError(f"{name} cannot contain an empty filter")
    return normalized


def _in_filter(expression: str, values: Sequence[str], args: list[str]) -> str:
    args.extend(values)
    return f"lower(trim({expression})) IN ({','.join('?' for _ in values)})"


def _run_filters(alias: str, columns: set[str], run_ids: Sequence[str],
                 projects: Sequence[str], topics: Sequence[str], args: list[str], *, master: bool) -> list[str]:
    conditions = []
    for values, column in ((run_ids, "local_run_id" if master else "run_id"),
                           (projects, "project"), (topics, "topic")):
        if not values:
            continue
        if column not in columns:
            raise ValueError("Unsupported filter for this TikTok source schema")
        clause = _in_filter(f"{alias}.{column}", values, args)
        if values is run_ids and master:
            clause = "(" + clause + " OR " + _in_filter(f"{alias}.master_run_id", values, args) + ")"
        conditions.append(clause)
    return conditions


def _blank(post_id: Any) -> dict[str, Any]:
    return {"post_id": str(post_id), "status": "unusable", "reasons": [],
            "references": [], "observations": [], "music_backfills": []}


def _reference(path: Path, kind: str, run_id: Any, snapshot_id: Any = "") -> dict[str, Any]:
    return {"source_database": str(path), "source_kind": kind,
            "run_id": _text(run_id), "snapshot_id": _text(snapshot_id)}


def _backfills(conn: sqlite3.Connection, record: dict[str, Any], path: Path) -> None:
    for sqlite_row in conn.execute("SELECT * FROM tiktok_master_music_backfill_observations WHERE post_id=? ORDER BY music_observed_at, observation_id", (record["post_id"],)):
        row = dict(sqlite_row)
        raw = _decode(row["music_evidence_json"])
        problems = []
        body = dict(raw or {})
        embedded_hash = body.pop("music_evidence_hash", "")
        digest = _hash(body) if raw else ""
        if not raw or digest != str(row["music_evidence_hash"]).casefold() or (embedded_hash and embedded_hash != digest):
            problems.append("music_hash_mismatch")
        base = next((ref for ref in record["references"] if
                     ref["snapshot_id"] == row["base_snapshot_id"] and
                     ref["integrity"] != "invalid" and
                     ref["evidence_hash"] == row["base_evidence_hash"] and
                     ref["observed_at"] == _time(row["base_observed_at"])), None)
        if base is None:
            problems.append("music_base_binding_mismatch")
        music_time = _time(row["music_observed_at"])
        if not music_time or (base and music_time < base["observed_at"]):
            problems.append("music_observation_time_invalid")
        if row["status"] not in {"completed", "unavailable"}:
            problems.append("music_observation_not_terminal")
        if row.get("observation_hash"):
            hash_body = {key: row.get(key, "") for key in (
                "run_id", "post_id", "base_snapshot_id", "base_evidence_hash", "base_observed_at",
                "music_observed_at", "music_evidence_hash", "status", "error",
            )}
            hash_body["schema_version"] = "tiktok-music-backfill-observation-v1"
            if _hash(hash_body) != row["observation_hash"]:
                problems.append("music_observation_hash_mismatch")
        item = {key: _text(row[key]) for key in (
            "observation_id", "run_id", "base_snapshot_id", "base_evidence_hash",
            "base_observed_at", "music_observed_at", "music_evidence_hash", "status",
        )}
        item.update({"source_database": str(path), "integrity": "invalid" if problems else "verified",
                     "reasons": problems, "music_evidence": _metadata(raw) if not problems else {}})
        record["music_backfills"].append(item)
        record["reasons"].extend(problems)


def _iter_master(conn: sqlite3.Connection, path: Path, run_ids: Sequence[str],
                 projects: Sequence[str], topics: Sequence[str]) -> Iterator[dict[str, Any]]:
    _table_columns(conn, "tiktok_master_posts", ("post_id", "latest_evidence_json"))
    snapshots = _table_columns(conn, "tiktok_master_snapshots",
        ("post_id", "snapshot_id", "evidence_json", "evidence_hash", "observed_at"), optional=True)
    sources = _table_columns(conn, "tiktok_master_sources", ("source_id", "database_path"), optional=True)
    backfills = _table_columns(conn, "tiktok_master_music_backfill_observations", (
        "observation_id", "run_id", "post_id", "base_snapshot_id", "base_evidence_hash",
        "base_observed_at", "music_observed_at", "music_evidence_json", "music_evidence_hash", "status",
    ), optional=True)
    args: list[str] = []
    where = []
    if run_ids or projects:
        run_columns = _table_columns(conn, "tiktok_master_runs", ("master_run_id",))
        _table_columns(conn, "tiktok_master_run_posts", ("post_id", "master_run_id"))
        filters = _run_filters("r", run_columns, run_ids, projects, topics, args, master=True)
        where.append("EXISTS (SELECT 1 FROM tiktok_master_run_posts rp JOIN tiktok_master_runs r ON r.master_run_id=rp.master_run_id WHERE rp.post_id=p.post_id AND " + " AND ".join(filters) + ")")
    elif topics:
        _table_columns(conn, "tiktok_master_post_topics", ("post_id", "topic"))
        where.append("EXISTS (SELECT 1 FROM tiktok_master_post_topics t WHERE t.post_id=p.post_id AND " + _in_filter("t.topic", topics, args) + ")")
    query = "SELECT p.* FROM tiktok_master_posts p" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.post_id"
    for sqlite_post in conn.execute(query, args):
        post = dict(sqlite_post)
        record = _blank(post["post_id"])
        if snapshots:
            for sqlite_snapshot in conn.execute("SELECT * FROM tiktok_master_snapshots WHERE post_id=? ORDER BY observed_at, snapshot_id", (post["post_id"],)):
                snapshot = dict(sqlite_snapshot)
                ref = _reference(path, "master", snapshot.get("local_run_id", ""), snapshot["snapshot_id"])
                ref["master_run_id"] = _text(snapshot.get("master_run_id"))
                if sources and snapshot.get("source_id"):
                    source = conn.execute("SELECT database_path FROM tiktok_master_sources WHERE source_id=?", (snapshot["source_id"],)).fetchone()
                    if source:
                        ref["origin_database"] = _text(source[0])
                _add_observation(record, snapshot, ref)
        # Some restored/legacy masters retain only the latest materialized packet.
        latest = dict(post, evidence_json=post["latest_evidence_json"],
                      evidence_hash=post.get("latest_evidence_hash", ""))
        ref = _reference(path, "master", post.get("last_run_id", ""), post.get("latest_snapshot_id", ""))
        ref["materialized_latest"] = True
        ref["registry_last_seen_at"] = _time(post.get("last_seen_at"))
        _add_observation(record, latest, ref)
        if backfills:
            _backfills(conn, record, path)
        yield record


def _iter_project(conn: sqlite3.Connection, path: Path, run_ids: Sequence[str],
                  projects: Sequence[str], topics: Sequence[str]) -> Iterator[dict[str, Any]]:
    _table_columns(conn, "engage_tiktok_posts", ("run_id", "post_id", "evidence_json", "evidence_ready"))
    args: list[str] = []
    where = ""
    if run_ids or projects or topics:
        columns = _table_columns(conn, "engage_tiktok_runs", ("run_id",))
        filters = _run_filters("r", columns, run_ids, projects, topics, args, master=False)
        where = " WHERE EXISTS (SELECT 1 FROM engage_tiktok_posts selected JOIN engage_tiktok_runs r ON r.run_id=selected.run_id WHERE selected.post_id=p.post_id AND " + " AND ".join(filters) + ")"
    cursor = conn.execute("SELECT p.* FROM engage_tiktok_posts p" + where + " ORDER BY p.post_id, p.run_id", args)
    for post_id, group in itertools.groupby(cursor, key=lambda row: row["post_id"]):
        record = _blank(post_id)
        for sqlite_row in group:
            row = dict(sqlite_row)
            _add_observation(record, row, _reference(path, "project", row["run_id"]))
        yield record


def iter_tiktok_records(database: Path, *, run_ids: Sequence[str] = (),
                        projects: Sequence[str] = (), topics: Sequence[str] = (),
                        creator: str | None = None) -> Iterator[dict[str, Any]]:
    """Stream every selected saved post, including unusable rows with reasons.

    A read transaction freezes this database's selection/history until iteration
    ends. Close the generator if stopping early. Filters are exact, case-insensitive
    metadata matches (OR within one filter type, AND across filter types). A creator
    filter matches saved owner evidence, never sound credits or arbitrary mentions.
    """
    runs, project_values, topic_values = (_values(run_ids, "run_ids"),
        _values(projects, "projects"), _values(topics, "topics"))
    owner = _creator(creator) if creator is not None else None
    if creator is not None and not owner:
        raise ValueError("creator must be an exact TikTok handle or profile URL")
    try:
        path = Path(database).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("TikTok source must be an existing local SQLite file")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error):
        raise ValueError("Cannot open the explicitly selected TikTok source database read-only") from None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if "tiktok_master_posts" in tables:
            iterator = _iter_master(connection, path, runs, project_values, topic_values)
        elif "engage_tiktok_posts" in tables:
            iterator = _iter_project(connection, path, runs, project_values, topic_values)
        else:
            raise ValueError("Unsupported TikTok source schema")
        for record in iterator:
            if owner is not None:
                owners = {item["projection"]["creator"] for item in record["observations"]}
                if not owners:
                    # Keep malformed rows belonging to the selected saved owner in
                    # the denominator without trusting their contents as evidence.
                    if "tiktok_master_posts" in tables:
                        rows = connection.execute("SELECT * FROM tiktok_master_posts WHERE post_id=?", (record["post_id"],))
                        for row in rows:
                            row = dict(row)
                            owners.add(_creator(row.get("creator_handle")))
                            document = _decode(row.get("latest_evidence_json")) or {}
                            owners.add(_creator(document.get("creator")))
                    else:
                        for row in connection.execute("SELECT evidence_json FROM engage_tiktok_posts WHERE post_id=?", (record["post_id"],)):
                            owners.add(_creator((_decode(row[0]) or {}).get("creator")))
                if owner not in owners:
                    continue
            record["observations"].sort(key=lambda item: (item["observed_at"], item["observation_id"]))
            record["reasons"] = sorted(set(record["reasons"]))
            record["status"] = "usable" if record["observations"] else "unusable"
            if not record["observations"] and not record["reasons"]:
                record["reasons"] = ["no_usable_saved_observation"]
            yield record
    except (sqlite3.Error, RecursionError, OverflowError):
        raise ValueError("Could not read compatible TikTok evidence from the selected source") from None
    finally:
        connection.close()
