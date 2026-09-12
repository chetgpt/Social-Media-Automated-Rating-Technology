"""Offline dossiers for agent-led Indonesian MUSIC DISCOVERY, not a collector.

Saved TikTok databases can be read query-only into an isolated review corpus.
No browser, network, source-database writes, downloads or external AI calls.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import uuid


WORKFLOW = "music_discovery"
MODE = "agent_led_research"
APPLICATION_ID = 0x4D445343
SCHEMA_VERSION = 1
META = {"workflow": WORKFLOW, "mode": MODE, "live_collectors_enabled": False}
DEFAULT_ROOT = Path(__file__).resolve().parent / "comments_data" / "music_discovery_runs"
RUN_PATTERN = re.compile(r"music_discovery_id_[a-z0-9_]{1,24}_\d{8}T\d{6}Z_[a-f0-9]{8}")
SECRET_QUERY = re.compile(r"token|signature|credential|authorization|password|secret|session|cookie|^auth(?:_|$)|^key$|api.?key|^x-amz-|^x-goog-|^policy$|expires?", re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _text(value, field, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{field}: expected {'nonempty ' if required else ''}text")
    if len(value) > 20000 or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError(f"{field}: invalid text")
    return value.strip()


def _object(value, keys, field):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ValueError(f"{field}: expected object with documented fields only")
    return value


def _list(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field}: expected list")
    return value


def _choice(value, choices, field):
    if value not in choices:
        raise ValueError(f"{field}: invalid value")
    return value


def _timestamp(value, field):
    value = _text(value, field, True)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise ValueError(f"{field}: expected timezone-aware ISO timestamp") from None


def _publication(value):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return _timestamp(value, "works.published_at")


def _url(value, field, non_spotify=False):
    value = _text(value, field, True)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or any(c.isspace() for c in value)
                or any(c in value for c in '\\<>"{}')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".lan"))
                    or host == "localhost" or not re.fullmatch(r"[a-z0-9.-]+", host)
                    or re.fullmatch(r"[0-9.]+", host)):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
        if any(SECRET_QUERY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError()
        if non_spotify and any(host == domain or host.endswith("." + domain)
                               for domain in ("spotify.com", "spotify.link", "spoti.fi")):
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != 443:
            authority += f":{port}"
        # Preserve identity-bearing parameters such as YouTube's v; drop fragments.
        return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise ValueError(f"{field}: expected public HTTPS URL without credentials or signed parameters"
                         + ("; Spotify is not a discovery source" if non_spotify else "")) from None


def _urls(value, field):
    return list(dict.fromkeys(_url(item, field) for item in _list(value, field)))


def candidate_template(tiktok=False):
    """Empty template only: no invented artist or presumed evidence."""
    result = {
        "name": "", "artist_type": "unknown", "primary_profile_url": "",
        "discovery_source": {"url": "", "observed_at": "", "note": ""},
        "indonesia": {"status": "uncertain", "summary": "", "sources": []},
        "original_music": {"status": "uncertain", "summary": "", "sources": []},
        "recognition": {"status": "uncertain", "summary": "", "sources": []},
        "works": [], "observations": [],
        "listening_review": {"status": "not_reviewed", "reviewer": "", "notes": "", "sources": []},
        "spotify_presence": "unknown", "rationale": "", "caveats": [],
    }
    if tiktok:
        from music_discovery_classification import classification_template
        result.update({"tiktok_evidence": [], "identity_links": {"aliases": [], "profile_urls": [], "stable_tiktok_ids": []},
                       "classification": classification_template()})
    return result


def normalize_candidate(payload):
    """Validate the documented schema; do not infer artist merits from metrics."""
    template = candidate_template()
    payload = _object(payload, set(template) | {"tiktok_evidence", "identity_links", "classification"}, "candidate")
    result = {"name": _text(payload.get("name"), "name", True),
              "artist_type": _choice(payload.get("artist_type", "unknown"),
                                     ("solo", "band", "group", "unknown"), "artist_type"),
              "primary_profile_url": _url(payload.get("primary_profile_url"), "primary_profile_url", True)}
    source = _object(payload.get("discovery_source"), ("url", "observed_at", "note"), "discovery_source")
    result["discovery_source"] = {"url": _url(source.get("url"), "discovery_source.url", True),
                                  "observed_at": _timestamp(source.get("observed_at"), "discovery_source.observed_at"),
                                  "note": _text(source.get("note", ""), "discovery_source.note")}
    for key in ("indonesia", "original_music", "recognition"):
        claim = _object(payload.get(key, template[key]), ("status", "summary", "sources"), key)
        choices = ("under_recognized", "uncertain", "established") if key == "recognition" else ("supported", "uncertain", "not_supported")
        status = _choice(claim.get("status", "uncertain"), choices, key + ".status")
        summary = _text(claim.get("summary", ""), key + ".summary")
        sources = _urls(claim.get("sources", []), key + ".sources")
        if status != "uncertain" and (not summary or not sources):
            raise ValueError(f"{key}: a supported conclusion needs summary and sources")
        result[key] = {"status": status, "summary": summary, "sources": sources}
    works = []
    for item in _list(payload.get("works", []), "works"):
        item = _object(item, ("title", "url", "published_at", "kind"), "works")
        works.append({"title": _text(item.get("title"), "works.title", True),
                      "url": _url(item.get("url"), "works.url"),
                      "published_at": _publication(item.get("published_at")),
                      "kind": _choice(item.get("kind", "other"), ("original", "performance", "other"), "works.kind")})
    result["works"] = works
    observations = []
    for item in _list(payload.get("observations", []), "observations"):
        item = _object(item, ("url", "observed_at", "metrics"), "observations")
        metrics = item.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError("observations.metrics: expected object")
        for key, number in metrics.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or SECRET_QUERY.search(key):
                raise ValueError("observations.metrics: invalid metric name")
            if isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0:
                raise ValueError("observations.metrics: expected finite nonnegative numbers")
            try:
                if not math.isfinite(number):
                    raise ValueError()
            except (OverflowError, ValueError):
                raise ValueError("observations.metrics: expected finite nonnegative numbers") from None
        observations.append({"url": _url(item.get("url"), "observations.url"),
                             "observed_at": _timestamp(item.get("observed_at"), "observations.observed_at"),
                             "metrics": dict(metrics)})
    result["observations"] = observations
    review = _object(payload.get("listening_review", template["listening_review"]),
                     ("status", "reviewer", "notes", "sources"), "listening_review")
    review = {"status": _choice(review.get("status", "not_reviewed"), ("not_reviewed", "reviewed"), "listening_review.status"),
              "reviewer": _text(review.get("reviewer", ""), "listening_review.reviewer"),
              "notes": _text(review.get("notes", ""), "listening_review.notes"),
              "sources": _urls(review.get("sources", []), "listening_review.sources")}
    if review["status"] == "reviewed" and not all((review["reviewer"], review["notes"], review["sources"])):
        raise ValueError("listening_review: reviewed requires reviewer, notes and sources")
    if review["status"] == "not_reviewed" and any((review["reviewer"], review["notes"], review["sources"])):
        raise ValueError("listening_review: keep unreviewed listening fields empty; put research notes in rationale")
    result["listening_review"] = review
    result["spotify_presence"] = _choice(payload.get("spotify_presence", "unknown"), ("present", "absent", "unknown"), "spotify_presence")
    result["rationale"] = _text(payload.get("rationale", ""), "rationale")
    result["caveats"] = [_text(item, "caveats", True) for item in _list(payload.get("caveats", []), "caveats")]
    # Optional extension preserves old dossier bytes and old report validation.
    if "tiktok_evidence" in payload:
        links = []
        for link in _list(payload["tiktok_evidence"], "tiktok_evidence"):
            link = _object(link, ("post_id", "observation_id", "role", "summary"), "tiktok_evidence")
            post_id = _text(link.get("post_id"), "tiktok_evidence.post_id", True)
            if not post_id.isdigit():
                raise ValueError("tiktok_evidence.post_id: expected numeric TikTok ID")
            links.append({"post_id": post_id, "observation_id": _text(link.get("observation_id"), "observation_id", True),
                          "role": _choice(link.get("role"), ("uploader_performer", "recording_artist", "mentioned_performer", "songwriter", "unresolved"), "tiktok_evidence.role"),
                          "summary": _text(link.get("summary"), "tiktok_evidence.summary", True)})
        result["tiktok_evidence"] = links
    if "identity_links" in payload:
        identity = _object(payload["identity_links"], ("aliases", "profile_urls", "stable_tiktok_ids"), "identity_links")
        result["identity_links"] = {"aliases": list(dict.fromkeys(_text(v, "alias", True) for v in _list(identity.get("aliases", []), "aliases"))),
                                    "profile_urls": _urls(identity.get("profile_urls", []), "identity_links.profile_urls"),
                                    "stable_tiktok_ids": list(dict.fromkeys(_text(v, "stable_tiktok_id", True) for v in _list(identity.get("stable_tiktok_ids", []), "stable_tiktok_ids")))}
    if "classification" in payload:
        from music_discovery_classification import normalize_classification
        result["classification"] = normalize_classification(payload["classification"])
    return result


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name}: expected positive integer")
    return value


def _artist_target(value):
    if isinstance(value, str) and value.casefold() == "all":
        return "all"
    return _positive(value, "artists")


def _safe_path(run_dir, filename):
    path = run_dir / filename
    if path.is_symlink() or path.resolve().parent != run_dir:
        raise ValueError("artifact path must remain inside the isolated run directory")
    return path


def init_run(*, artists, label="emerging", focus="", lookback_days=7, output_root=None, input_mode=None):
    if input_mode not in (None, "live_search"):
        raise ValueError("unsupported discovery input mode")
    artists = _artist_target(artists)
    lookback_days = _positive(lookback_days, "lookback_days")
    label = re.sub(r"[^a-z0-9]+", "_", _text(label, "label", True).lower()).strip("_")[:24].rstrip("_")
    if not label:
        raise ValueError("label: needs at least one ASCII letter or digit")
    now = _now()
    try:
        start = now - timedelta(days=lookback_days)
    except OverflowError:
        raise ValueError("lookback_days: outside supported date range") from None
    run_id = f"music_discovery_id_{label}_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    root = Path(output_root).expanduser().resolve() if output_root is not None else DEFAULT_ROOT
    run_dir = root / run_id
    # Leave room for SQLite's journal and atomic-report siblings on Windows.
    if len(str(run_dir / "discovery.sqlite-journal").encode("utf-16-le")) // 2 >= 240:
        raise ValueError("output path too long; choose a shorter output root or label")
    brief = {**META, "schema": "music-discovery-brief-v1", "run_id": run_id,
             "created_at": now.isoformat(), "country": "Indonesia", "country_code": "ID",
             "requested_artists": artists, "label": label, "focus": _text(focus, "focus"),
             "lookback_days": lookback_days,
             "freshness_window": {"start": start.isoformat(), "end": now.isoformat(),
                                  "date_only_comparison": "inclusive_calendar_dates_UTC"}}
    if input_mode == "live_search":
        brief.update(schema="music-discovery-brief-v2", input_mode="live_search", live_collectors_enabled=True)
    run_dir.mkdir(parents=True, exist_ok=False)
    with (run_dir / "brief.json").open("x", encoding="utf-8") as handle:
        json.dump(brief, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    with closing(sqlite3.connect(run_dir / "discovery.sqlite")) as db, db:
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.execute("CREATE TABLE discovery_run (run_id TEXT PRIMARY KEY, brief_json TEXT NOT NULL, brief_sha256 TEXT NOT NULL)")
        db.execute("CREATE TABLE candidate_revisions (candidate_id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, PRIMARY KEY(candidate_id, revision))")
        db.execute("INSERT INTO discovery_run VALUES (?,?,?)", (run_id, _json(brief), _hash(brief)))
    return status_run(run_dir)


def _open_run(run_dir, writable=False):
    run_dir = Path(run_dir).expanduser().resolve()
    brief_path, db_path = _safe_path(run_dir, "brief.json"), _safe_path(run_dir, "discovery.sqlite")
    if not brief_path.is_file() or not db_path.is_file():
        raise ValueError("not a MUSIC DISCOVERY run directory")
    try:
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        live = isinstance(brief, dict) and brief.get("schema") == "music-discovery-brief-v2" and brief.get("input_mode") == "live_search"
        expected_meta = {**META, "live_collectors_enabled": live}
        if (not isinstance(brief, dict) or brief.get("schema") != ("music-discovery-brief-v2" if live else "music-discovery-brief-v1")
                or any(brief.get(key) != value for key, value in expected_meta.items())
                or not RUN_PATTERN.fullmatch(brief.get("run_id", "")) or run_dir.name != brief["run_id"]
                or brief.get("country_code") != "ID" or brief.get("country") != "Indonesia"):
            raise ValueError("run brief identity mismatch")
        _artist_target(brief["requested_artists"])
        _positive(brief["lookback_days"], "lookback_days")
        created = _timestamp(brief["created_at"], "created_at")
        window = brief["freshness_window"]
        end = _timestamp(window["end"], "freshness_window.end")
        start = _timestamp(window["start"], "freshness_window.start")
        if created != end or datetime.fromisoformat(end) - datetime.fromisoformat(start) != timedelta(days=brief["lookback_days"]):
            raise ValueError("frozen freshness window mismatch")
    except (KeyError, TypeError, json.JSONDecodeError, OverflowError):
        raise ValueError("invalid MUSIC DISCOVERY brief") from None
    mode = "rw" if writable else "ro"
    db = sqlite3.connect(db_path.as_uri() + "?mode=" + mode, uri=True)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if not writable:
            db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("database is not a supported MUSIC DISCOVERY database")
        tables = {row[0]: row[1] for row in db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if tables != {"discovery_run": "table", "candidate_revisions": "table"}:
            raise ValueError("unexpected MUSIC DISCOVERY database schema")
        identity = db.execute("SELECT run_id,brief_json,brief_sha256 FROM discovery_run").fetchall()
        if identity != [(brief["run_id"], _json(brief), _hash(brief))]:
            raise ValueError("brief and database identity mismatch")
        return run_dir, brief, db
    except BaseException:
        db.close()
        raise


def _read_revisions(db):
    grouped = {}
    for candidate_id, revision, recorded_at, encoded, digest in db.execute(
            "SELECT candidate_id,revision,recorded_at,payload_json,payload_sha256 FROM candidate_revisions ORDER BY candidate_id,revision"):
        payload = json.loads(encoded)
        if normalize_candidate(payload) != payload or _hash(payload) != digest:
            raise ValueError("candidate revision validation failed")
        expected_id = "artist_" + hashlib.sha256(payload["primary_profile_url"].encode()).hexdigest()[:24]
        history = grouped.setdefault(candidate_id, [])
        if candidate_id != expected_id or revision != len(history) + 1:
            raise ValueError("candidate identity or revision sequence mismatch")
        _timestamp(recorded_at, "recorded_at")
        history.append({"revision": revision, "recorded_at": recorded_at, "payload": payload})
    return grouped


def record_candidate(run_dir, payload):
    candidate = normalize_candidate(payload)
    candidate_id = "artist_" + hashlib.sha256(candidate["primary_profile_url"].encode()).hexdigest()[:24]
    # Validate existing state query-only before opening its specifically identified DB for writing.
    _, _, check = _open_run(run_dir)
    with closing(check):
        existing = _read_revisions(check)
    if candidate.get("tiktok_evidence"):
        from music_discovery_corpus import validate_links
        validate_links(run_dir, [candidate])
    profiles = {candidate["primary_profile_url"], *candidate.get("identity_links", {}).get("profile_urls", [])}
    stable = set(candidate.get("identity_links", {}).get("stable_tiktok_ids", []))
    for known_id, history in existing.items():
        known = history[-1]["payload"]
        if known_id == candidate_id:
            continue
        known_profiles = {known["primary_profile_url"], *known.get("identity_links", {}).get("profile_urls", [])}
        if profiles & known_profiles or stable & set(known.get("identity_links", {}).get("stable_tiktok_ids", [])):
            raise ValueError("artist identity overlaps an existing dossier; revise its primary profile rather than double-count")
    from music_discovery_corpus import validate_review_links
    validate_review_links(run_dir, [history[-1]["payload"] for known_id, history in existing.items() if known_id != candidate_id] + [candidate])
    _, _, db = _open_run(run_dir, writable=True)
    with closing(db), db:
        db.execute("BEGIN IMMEDIATE")
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM candidate_revisions WHERE candidate_id=?", (candidate_id,)).fetchone()[0]
        db.execute("INSERT INTO candidate_revisions VALUES (?,?,?,?,?)", (candidate_id, revision, _now().isoformat(), _json(candidate), _hash(candidate)))
    return {**status_run(run_dir), "recorded_candidate_id": candidate_id, "recorded_revision": revision}


def _growth(history):
    series = {}
    for revision in history:
        for observation in revision["payload"]["observations"]:
            for metric, number in observation["metrics"].items():
                points = series.setdefault((observation["url"], metric), {})
                points.setdefault(observation["observed_at"], set()).add(number)
    result = []
    for (url, metric), points in sorted(series.items()):
        if any(len(values) != 1 for values in points.values()):
            result.append({"url": url, "metric": metric, "status": "conflicting_observations"})
            continue
        ordered = sorted(points.items(), key=lambda pair: datetime.fromisoformat(pair[0]))
        if len(ordered) < 2:
            continue
        first, last = ordered[0], ordered[-1]
        baseline, current = next(iter(first[1])), next(iter(last[1]))
        delta = current - baseline
        percent = (delta / baseline * 100) if baseline else None
        result.append({"url": url, "metric": metric, "status": "observed_change",
                       "from_observed_at": first[0], "to_observed_at": last[0],
                       "from_value": baseline, "to_value": current,
                       "absolute_change": delta if math.isfinite(delta) else None,
                       "percent_change": percent if percent is not None and math.isfinite(percent) else None,
                       "observation_count": len(points)})
    return result


def _assess(brief, payload):
    window = brief["freshness_window"]
    start, end = datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])
    fresh = []
    for work in payload["works"]:
        published = work["published_at"]
        if published is None:
            continue
        date_only = len(published) == 10
        inside = start.date() <= date.fromisoformat(published) <= end.date() if date_only else start <= datetime.fromisoformat(published) <= end
        if inside:
            fresh.append({"url": work["url"], "published_at": published,
                          "precision": "date" if date_only else "timestamp",
                          "comparison": "calendar_date_in_window" if date_only else "timestamp_in_window"})
    missing = []
    for key, expected in (("indonesia", "supported"), ("original_music", "supported"), ("recognition", "under_recognized")):
        if payload[key]["status"] != expected:
            missing.append(key)
    if not fresh:
        missing.append("recent_published_work")
    return {"qualified": not missing, "missing_criteria": missing, "freshness_evidence": fresh,
            "listening_reviewed": payload["listening_review"]["status"] == "reviewed"}


def _snapshot(run_dir, brief, grouped):
    candidates = []
    for candidate_id, history in sorted(grouped.items()):
        latest = history[-1]
        item = {"candidate_id": candidate_id, "revision": latest["revision"],
                           "recorded_at": latest["recorded_at"], "dossier": latest["payload"],
                           **_assess(brief, latest["payload"]), "metric_changes": _growth(history)}
        if latest["payload"].get("tiktok_evidence") and not any(link["role"] != "unresolved" for link in latest["payload"]["tiktok_evidence"]):
            item["qualified"] = False
            item["missing_criteria"].append("resolved_tiktok_artist_role")
        if "classification" in latest["payload"]:
            from music_discovery_classification import assess_classification
            from music_discovery_corpus import linked_observations
            observations = [observation for rev in history for observation in rev["payload"]["observations"]]
            observations += linked_observations(run_dir, latest["payload"]) if latest["payload"].get("tiktok_evidence") else []
            item["artist_classification"] = assess_classification(latest["payload"]["classification"],
                    as_of=latest["recorded_at"], observations=observations)
        candidates.append(item)
    qualified = sum(item["qualified"] for item in candidates)
    reviewed = sum(item["listening_reviewed"] for item in candidates)
    snapshot = {**{key: brief[key] for key in META}, "schema": "music-discovery-shortlist-v1", "run_id": brief["run_id"],
            "run_dir": str(run_dir), "brief": brief, "requested_artists": brief["requested_artists"],
            "candidate_count": len(candidates), "revision_count": sum(map(len, grouped.values())),
            "qualified_count": qualified, "listening_reviewed_count": reviewed,
            "qualified_listening_reviewed_count": sum(item["qualified"] and item["listening_reviewed"] for item in candidates),
            "status": "research_target_met" if isinstance(brief["requested_artists"], int) and qualified >= brief["requested_artists"] else "research_incomplete",
            "candidates": candidates,
            "limitations": ["Agent-recorded research; no live collection is performed by this helper.",
                            "Evidence qualification is not musical-quality scoring or Spotify approval.",
                            "Counts describe this shortlist, not exhaustive Indonesian artist coverage.",
                            "One observation cannot establish growth; observed metric changes do not establish musical quality.",
                            "Date-only publication evidence uses inclusive UTC calendar dates, not exact timestamp precision."]}
    from music_discovery_corpus import corpus_summary, validate_links, validate_review_links, review_gaps
    corpus = corpus_summary(run_dir)
    if corpus is not None:
        validate_links(run_dir, candidates)
        validate_review_links(run_dir, candidates)
        snapshot["tiktok_corpus"] = corpus
        snapshot["post_review_gaps"] = review_gaps(run_dir)
        snapshot["tiktok_linked_artists"] = sum(bool(c["dossier"].get("tiktok_evidence")) for c in candidates)
        snapshot["web_only_artists"] = len(candidates) - snapshot["tiktok_linked_artists"]
        snapshot["web_only_qualified_count"] = sum(c["qualified"] and not c["dossier"].get("tiktok_evidence") for c in candidates)
        snapshot["qualified_count"] = sum(c["qualified"] and bool(c["dossier"].get("tiktok_evidence")) for c in candidates)
        snapshot["qualified_listening_reviewed_count"] = sum(c["qualified"] and c["listening_reviewed"] and bool(c["dossier"].get("tiktok_evidence")) for c in candidates)
        snapshot["web_only_listening_reviewed_count"] = sum(c["listening_reviewed"] and not c["dossier"].get("tiktok_evidence") for c in candidates)
        snapshot["classification_counts"] = {"career_stage": {}, "momentum": {}, "recognition": {}}
        for candidate in candidates:
            for axis in ("career_stage", "momentum", "recognition"):
                label = (candidate["dossier"]["recognition"]["status"] if axis == "recognition" else
                         candidate.get("artist_classification", {}).get(axis, {}).get("label", "uncertain"))
                counts = snapshot["classification_counts"][axis]
                counts[label] = counts.get(label, 0) + 1
        if corpus["scan_status"] == "source_scan_complete" and not corpus["selected_posts"]:
            snapshot["status"] = "no_saved_posts"
        elif not corpus["coverage_complete"]:
            snapshot["status"] = "research_incomplete"
        elif brief["requested_artists"] == "all":
            gaps = corpus["unusable_posts"] or corpus["unresolved_posts"] or any(
                c["dossier"]["indonesia"]["status"] == "uncertain" or c["dossier"]["recognition"]["status"] == "uncertain" or
                c["dossier"]["original_music"]["status"] == "uncertain" or
                not (any(work["published_at"] for work in c["dossier"]["works"]) or
                     (c["dossier"].get("classification", {}).get("career_stage", {}).get("earliest_release", {}).get("basis") in ("documented_debut", "earliest_documented_release") and
                      c["dossier"].get("classification", {}).get("career_stage", {}).get("earliest_release", {}).get("published_at"))) or
                any(c.get("artist_classification", {}).get(axis, {}).get("label", "uncertain") == "uncertain" for axis in ("career_stage", "momentum"))
                for c in candidates)
            snapshot["status"] = "research_complete_with_gaps" if gaps else "research_complete"
        else:
            snapshot["status"] = "research_target_met" if snapshot["qualified_count"] >= brief["requested_artists"] else "research_incomplete"
        snapshot["limitations"].append("Source scan completion is not artist review completion. Web-only artists are not TikTok discoveries; career and momentum labels are researcher-supported assessments, not independently verified truth.")
    if brief.get("input_mode") == "live_search":
        snapshot["input_mode"] = "live_search"
        if (Path(run_dir) / "live_search.json").exists():
            from music_discovery_search import live_search_status
            snapshot["live_search"] = live_search_status(run_dir)
        live_state = snapshot.get("live_search") or {}
        if not live_state.get("all_imported"):
            snapshot["status"] = "live_collection_pending"
        elif corpus is None or not corpus["coverage_complete"]:
            snapshot["status"] = "research_incomplete"
        elif brief["requested_artists"] == "all":
            snapshot["status"] = ("batch_review_complete_with_gaps" if snapshot["status"] == "research_complete_with_gaps" else "batch_review_complete")
        snapshot["limitations"].append("Live topic batches collect category-directed candidates, not verified emerging artists. A completed batch or ALL review never proves all artists or all TikTok search results were found. Extend category-focused queries when more research is needed.")
    return snapshot


def status_run(run_dir):
    run_dir, brief, db = _open_run(run_dir)
    with closing(db):
        return _snapshot(run_dir, brief, _read_revisions(db))


def _md_text(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _markdown(snapshot):
    brief = snapshot["brief"]
    lines = ["# MUSIC DISCOVERY — Indonesia", "", f"Run: `{brief['run_id']}`", "",
             f"Status: {snapshot['status']}. Qualified artists: {snapshot['qualified_count']}/{snapshot['requested_artists']}. "
             f"Listening reviewed: {snapshot['listening_reviewed_count']}/{snapshot['candidate_count']} candidates.", "",
             f"Frozen publication window: {brief['freshness_window']['start']} to {brief['freshness_window']['end']}.", "",
             "Agent-led research only; this helper did not search, listen, or collect platform data.", "",
             "## Qualified shortlist", ""]
    if "tiktok_corpus" in snapshot:
        corpus = snapshot["tiktok_corpus"]
        lines[10:10] = ["## Saved TikTok coverage", "",
            f"Scan: {corpus['scan_status']}; selected {corpus['selected_posts']}; usable {corpus['usable_posts']}; unusable {corpus['unusable_posts']}.", "",
            f"Reviewed {corpus['reviewed_posts']}; pending {corpus['pending_posts']}; unresolved {corpus['unresolved_posts']}. TikTok-linked artists {snapshot['tiktok_linked_artists']}; web-only artists {snapshot['web_only_artists']}.", "",
            "The recent-work window is a shortlist filter, not a limit on source scanning or career-history research.", ""]
    if snapshot.get("input_mode") == "live_search":
        lines[10:10] = ["## Live category-focused collection", "",
            "Fresh candidate evidence is collected through the guarded TikTok MUSIC AUDIT engine; this report is the separate artist-research stage.", "",
            "Completing a working search batch is not proof of exhaustive category or Indonesia-wide coverage.", ""]
    for qualified in (True, False):
        if not qualified:
            lines.extend(["## Pending / not qualified", ""])
        selected = [item for item in snapshot["candidates"] if item["qualified"] == qualified]
        if not selected:
            lines.extend(["None recorded.", ""])
        for item in selected:
            dossier = item["dossier"]
            lines.extend([f"### {_md_text(dossier['name'])}", "",
                          f"Profile: <{dossier['primary_profile_url']}>", "",
                          f"Type: {dossier['artist_type']}; Spotify presence: {dossier['spotify_presence']}; "
                          f"listening: {dossier['listening_review']['status']}.", "",
                          f"Discovery: <{dossier['discovery_source']['url']}> — observed {dossier['discovery_source']['observed_at']}.", "",
                          _md_text(dossier["rationale"]) or "Rationale not yet recorded.", ""])
            if "tiktok_corpus" in snapshot:
                lines.extend(["Origin: " + ("saved TikTok evidence" if dossier.get("tiktok_evidence") else "web-only support; not a TikTok-origin discovery") + ".", ""])
            if dossier.get("tiktok_evidence"):
                lines.extend(["Saved TikTok evidence:", ""])
                for link in dossier["tiktok_evidence"]:
                    lines.append(f"- Post {link['post_id']}; observation {_md_text(link['observation_id'])}; role {link['role']}: {_md_text(link['summary'])}.")
                lines.append("")
            if "artist_classification" in item:
                lines.extend(["Artist classification (separate from recognition and listening):", "", "```json",
                              json.dumps(item["artist_classification"], ensure_ascii=False, indent=2), "```", ""])
            for key in ("indonesia", "original_music", "recognition"):
                claim = dossier[key]
                lines.extend([f"{key}: {claim['status']} — {_md_text(claim['summary'])}", ""])
                if claim["sources"]:
                    lines.extend(["Sources: " + ", ".join(f"<{url}>" for url in claim["sources"]), ""])
            if item["missing_criteria"]:
                lines.extend(["Missing criteria: " + ", ".join(item["missing_criteria"]), ""])
            lines.extend(["Works:", ""])
            for work in dossier["works"]:
                lines.append(f"- {_md_text(work['title'])}: <{work['url']}>; published {work['published_at'] or 'unknown'}; {work['kind']}.")
            if not dossier["works"]:
                lines.append("- No works recorded.")
            lines.append("")
            if item["metric_changes"]:
                lines.extend(["Observed metric changes (not a talent score):", ""])
                for change in item["metric_changes"]:
                    detail = (f"{change['from_value']} → {change['to_value']} between {change['from_observed_at']} and {change['to_observed_at']}"
                              if change["status"] == "observed_change" else "conflicting observations; no change asserted")
                    lines.append(f"- <{change['url']}> — {change['metric']}: {detail}.")
                lines.append("")
            else:
                lines.extend(["Growth: insufficient repeat observations; no growth claim.", ""])
            review = dossier["listening_review"]
            if item["listening_reviewed"]:
                lines.extend([f"Listening review by {_md_text(review['reviewer'])}: {_md_text(review['notes'])}", "",
                              "Listening sources: " + ", ".join(f"<{url}>" for url in review["sources"]), ""])
            else:
                lines.extend(["Listening review pending; no musical-quality judgment is recorded.", ""])
            if dossier["caveats"]:
                lines.extend(["Caveats:", "", *["- " + _md_text(note) for note in dossier["caveats"]], ""])
    if snapshot.get("post_review_gaps"):
        lines.extend(["## Unresolved / unusable source posts", ""])
        for gap in snapshot["post_review_gaps"]:
            detail = gap.get("note") or ", ".join(gap.get("reasons", []))
            lines.extend([f"- Post {_md_text(gap['post_id'])}; {gap['disposition']}: {_md_text(detail)}."])
        lines.append("")
    lines.extend(["## Limitations", "", *["- " + item for item in snapshot["limitations"]], ""])
    return "\n".join(lines)


def report_run(run_dir):
    snapshot = status_run(run_dir)
    run_dir = Path(snapshot["run_dir"])
    payload = {**snapshot, "report_sha256": _hash(snapshot)}
    json_path, md_path = _safe_path(run_dir, "shortlist.json"), _safe_path(run_dir, "shortlist.md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(snapshot), encoding="utf-8")
    return {**snapshot, "report_json": str(json_path), "report_markdown": str(md_path), "report_sha256": payload["report_sha256"]}


def validate_run(run_dir):
    run_dir, brief, db = _open_run(run_dir)
    with closing(db):
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("database integrity check failed")
        snapshot = _snapshot(run_dir, brief, _read_revisions(db))
    from music_discovery_corpus import validate_corpus
    validate_corpus(run_dir)
    present = [(name, _safe_path(run_dir, name)) for name in ("shortlist.json", "shortlist.md")]
    if any(path.exists() for _, path in present):
        if not all(path.is_file() for _, path in present):
            raise ValueError("report artifacts incomplete; run report again")
        report = json.loads(present[0][1].read_text(encoding="utf-8"))
        if report != {**snapshot, "report_sha256": _hash(snapshot)} or present[1][1].read_text(encoding="utf-8") != _markdown(snapshot):
            raise ValueError("report artifacts stale or altered; run report again")
    return {**snapshot, "validation_result": "pass", "validated": True,
            "reports_present": all(path.is_file() for _, path in present)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an empty offline research brief and isolated database")
    init.add_argument("--artists", type=lambda v: "all" if v.lower() == "all" else int(v), required=True)
    init.add_argument("--label", default="emerging")
    init.add_argument("--focus", default="")
    init.add_argument("--lookback-days", type=int, default=7)
    init.add_argument("--output-root", type=Path)
    template = commands.add_parser("candidate-template", help="Print an empty candidate JSON template")
    template.add_argument("--tiktok", action="store_true", help="Include source links, identity aliases and career/momentum assessments")
    source = commands.add_parser("from-tiktok", help="Read ALL selected saved TikTok posts into a new isolated review corpus")
    source.add_argument("--database", action="append", help="Repeat for master/project sources; default is workspace master")
    source.add_argument("--run-id", action="append", default=[])
    source.add_argument("--project", action="append", default=[])
    source.add_argument("--topic", action="append", default=[])
    source.add_argument("--creator")
    source.add_argument("--artists", type=lambda v: "all" if v.lower() == "all" else int(v), default="all")
    source.add_argument("--label", default="tiktok_saved")
    source.add_argument("--focus", default="Indonesian artists from saved TikTok evidence")
    source.add_argument("--lookback-days", type=int, default=7)
    source.add_argument("--output-root", type=Path)
    for name in ("resume-source", "export-review", "review-post", "inspect-post"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", type=Path, required=True)
        if name == "export-review":
            sub.add_argument("--posts", type=int, default=20, help="Packet size, never a total scan/review ceiling")
        if name in ("review-post", "inspect-post"):
            sub.add_argument("--post-id", required=True)
        if name == "review-post":
            sub.add_argument("--disposition", choices=("reviewed", "not_artist", "unresolved"), required=True)
            sub.add_argument("--note", required=True)
    for command in ("record", "status", "report", "validate"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--run-dir", type=Path, required=True)
        if command == "record":
            subparser.add_argument("--file", required=True, help="Candidate JSON path, or - for standard input")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_run(artists=args.artists, label=args.label, focus=args.focus,
                              lookback_days=args.lookback_days, output_root=args.output_root)
        elif args.command == "candidate-template":
            result = candidate_template(tiktok=args.tiktok)
        elif args.command == "from-tiktok":
            from music_discovery_corpus import create_corpus
            result = init_run(artists=args.artists, label=args.label, focus=args.focus,
                              lookback_days=args.lookback_days, output_root=args.output_root)
            print(json.dumps({"discovery_run_dir": result["run_dir"], "next_step": "source_scan"}), file=sys.stderr, flush=True)
            sources = args.database or [str(DEFAULT_ROOT.parent / "tiktok_master" / "state" / "tiktok_master.sqlite")]
            create_corpus(result["run_dir"], sources, run_ids=args.run_id, projects=args.project, topics=args.topic, creator=args.creator)
            result = status_run(result["run_dir"])
        elif args.command in ("resume-source", "export-review", "review-post", "inspect-post"):
            from music_discovery_corpus import resume_source, export_review, review_post, post_packet
            if args.command == "resume-source":
                result = resume_source(args.run_dir)
            elif args.command == "export-review":
                result = export_review(args.run_dir, posts=args.posts)
            elif args.command == "inspect-post":
                result = post_packet(args.run_dir, args.post_id)
            else:
                result = review_post(args.run_dir, post_id=args.post_id, disposition=args.disposition, note=args.note)
        elif args.command == "record":
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
            result = record_candidate(args.run_dir, json.loads(text))
        else:
            result = {"status": status_run, "report": report_run, "validate": validate_run}[args.command](args.run_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, OverflowError) as exc:
        # Do not echo arbitrary payload values, SQL or filesystem error text.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "offline operation failed; check input JSON, run identity and filesystem access"
        print(json.dumps({**META, "status": "error", "error": message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
