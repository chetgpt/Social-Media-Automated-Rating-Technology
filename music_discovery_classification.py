"""Pure, offline evidence checks for MUSIC DISCOVERY career and momentum claims.

These checks validate the supplied research structure, not the truth of a page.
They neither fetch citations nor infer musical quality, identity, or recognition.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import ipaddress
import math
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit


RUBRIC_VERSION = "music-discovery-classification-v1"
_SECRET_QUERY = re.compile(
    r"token|signature|credential|authorization|password|secret|session|cookie|"
    r"^auth(?:_|$)|^key$|api.?key|^x-amz-|^x-goog-|^policy$|expires?", re.I)


def _text(value, field):
    if (not isinstance(value, str) or len(value) > 20000
            or any(ord(char) < 32 and char not in "\n\r\t" for char in value)):
        raise ValueError(f"classification.{field}: expected bounded text")
    return value.strip()


def _object(value, allowed, field):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"classification.{field}: expected documented object fields")
    return value


def _choice(value, choices, field):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"classification.{field}: invalid value")
    return value


def _url(value):
    value = _text(value, "sources")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or any(char.isspace() for char in value)
                or any(char in value for char in '\\<>"{}')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".lan"))
                    or not re.fullmatch(r"[a-z0-9.-]+", host)
                    or re.fullmatch(r"[0-9.]+", host)):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
        if any(_SECRET_QUERY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != 443:
            authority += f":{port}"
        return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise ValueError("classification.sources: expected public HTTPS URL without credentials or signed parameters") from None


def _sources(value):
    if not isinstance(value, list):
        raise ValueError("classification.sources: expected list")
    return list(dict.fromkeys(_url(item) for item in value))


def _date(value, field):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"classification.{field}: expected ISO calendar date or null") from None


def _timestamp(value, field):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError, OverflowError):
        raise ValueError(f"classification.{field}: expected timezone-aware ISO timestamp") from None


def _publication(value):
    if value is None or isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return _date(value, "earliest_release.published_at")
    return _timestamp(value, "earliest_release.published_at").isoformat()


def classification_template():
    """Unclaimed assessments; absent evidence must not become inferred truth.

    ``assessed_on`` is the date the research conclusion applies to; an optional
    ``applies_through`` explicitly extends that applicability, without a hidden
    age cutoff. Citation sources are supporting evidence, not discovery seeds.
    """
    return {
        "rubric_version": RUBRIC_VERSION,
        "career_stage": {
            "claim": "uncertain", "assessed_on": None, "applies_through": None,
            "rationale": "", "sources": [],
            "earliest_release": {"published_at": None, "basis": "unknown", "sources": []},
            "prior_history": {"status": "not_reviewed", "rationale": "", "sources": []},
            "career_context": {"status": "uncertain", "rationale": "", "sources": []},
        },
        "momentum": {
            "claim": "uncertain", "assessed_on": None, "applies_through": None,
            "rationale": "", "sources": [],
            "corroboration": {"status": "uncertain", "rationale": "", "sources": []},
        },
    }


def _research_claim(value, choices, field, default="uncertain"):
    value = _object(value, ("status", "rationale", "sources"), field)
    return {"status": _choice(value.get("status", default), choices, field + ".status"),
            "rationale": _text(value.get("rationale", ""), field + ".rationale"),
            "sources": _sources(value.get("sources", []))}


def normalize_classification(payload):
    """Normalize structure without rejecting a merely incomplete research claim."""
    template = classification_template()
    payload = _object({} if payload is None else payload, template, "root")
    if payload.get("rubric_version", RUBRIC_VERSION) != RUBRIC_VERSION:
        raise ValueError("classification.rubric_version: unsupported rubric")
    result = {"rubric_version": RUBRIC_VERSION}
    for axis, choices in (("career_stage", ("new_artist", "new_project", "active", "uncertain")),
                          ("momentum", ("emerging", "not_established", "uncertain"))):
        raw = _object(payload.get(axis, {}), template[axis], axis)
        normalized = {"claim": _choice(raw.get("claim", "uncertain"), choices, axis + ".claim"),
                      "assessed_on": _date(raw.get("assessed_on"), axis + ".assessed_on"),
                      "applies_through": _date(raw.get("applies_through"), axis + ".applies_through"),
                      "rationale": _text(raw.get("rationale", ""), axis + ".rationale"),
                      "sources": _sources(raw.get("sources", []))}
        if axis == "career_stage":
            release = _object(raw.get("earliest_release", {}), template[axis]["earliest_release"], "earliest_release")
            normalized["earliest_release"] = {
                "published_at": _publication(release.get("published_at")),
                "basis": _choice(release.get("basis", "unknown"),
                                 ("documented_debut", "earliest_documented_release", "first_seen_in_dataset", "unknown"),
                                 "earliest_release.basis"),
                "sources": _sources(release.get("sources", [])),
            }
            normalized["prior_history"] = _research_claim(
                raw.get("prior_history", {}),
                ("not_reviewed", "none_found", "prior_music_career", "name_change", "new_project", "uncertain"),
                "prior_history", "not_reviewed")
            normalized["career_context"] = _research_claim(
                raw.get("career_context", {}), ("early_career", "ongoing_career", "uncertain"), "career_context")
        else:
            normalized["corroboration"] = _research_claim(
                raw.get("corroboration", {}), ("broader_attention", "single_work_only", "uncertain"), "corroboration")
        result[axis] = normalized
    return result


def _dated_requirements(assessment, as_of):
    missing = []
    if not assessment["rationale"] or not assessment["sources"]:
        missing.append("cited_assessment_rationale")
    assessed = assessment["assessed_on"]
    until = assessment["applies_through"]
    if assessed is None:
        missing.append("assessment_date")
    elif date.fromisoformat(assessed) > as_of.date():
        missing.append("assessment_date_in_future")
    elif until is not None and until < assessed:
        missing.append("invalid_assessment_window")
    elif as_of.date().isoformat() > (until or assessed):
        missing.append("assessment_not_applicable_as_of")
    return missing


def _has_research(claim):
    return bool(claim["rationale"] and claim["sources"])


def _axis_result(assessment, missing):
    supported = assessment["claim"] != "uncertain" and not missing
    return {"claim": assessment["claim"],
            "label": assessment["claim"] if supported else "uncertain",
            "confidence": "supported" if supported else "uncertain",
            "status": "supported" if supported else "pending",
            "assessed_on": assessment["assessed_on"],
            "applies_through": assessment["applies_through"] or assessment["assessed_on"],
            "missing_evidence": list(dict.fromkeys(missing))}


def _metric_changes(observations, as_of):
    """Deduplicate observations by canonical resource, metric, and actual time."""
    series, problems = {}, []
    for ordinal, observation in enumerate(observations, 1):
        try:
            if not isinstance(observation, dict):
                raise ValueError()
            url = _url(observation.get("url"))
            observed = _timestamp(observation.get("observed_at"), "observed_at")
            metrics = observation.get("metrics")
            if not isinstance(metrics, dict):
                raise ValueError()
            if observed > as_of:
                problems.append({"observation": ordinal, "reason": "future_observation"})
                continue
            for metric, number in metrics.items():
                if (not isinstance(metric, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", metric)
                        or _SECRET_QUERY.search(metric) or isinstance(number, bool)
                        or not isinstance(number, (int, float)) or number < 0 or not math.isfinite(number)):
                    raise ValueError()
            for metric, number in metrics.items():
                points = series.setdefault((url, metric), {})
                points.setdefault(observed.isoformat(), set()).add(number)
        except (ValueError, OverflowError, TypeError):
            problems.append({"observation": ordinal, "reason": "invalid_observation"})
    result = []
    for (url, metric), points in sorted(series.items()):
        if any(len(values) != 1 for values in points.values()):
            result.append({"url": url, "metric": metric, "status": "conflicting_observations",
                           "observation_count": len(points)})
            continue
        ordered = sorted(points.items())
        if len(ordered) < 2:
            result.append({"url": url, "metric": metric, "status": "single_observation",
                           "observation_count": len(points)})
            continue
        baseline, current = next(iter(ordered[0][1])), next(iter(ordered[-1][1]))
        delta = current - baseline
        try:
            percent = delta / baseline * 100 if baseline else None
        except OverflowError:
            percent = None
        if percent is not None and not math.isfinite(percent):
            percent = None
        result.append({"url": url, "metric": metric, "status": "observed_change",
                       "from_observed_at": ordered[0][0], "to_observed_at": ordered[-1][0],
                       "from_value": baseline, "to_value": current,
                       "absolute_change": delta, "percent_change": percent,
                       "observation_count": len(points)})
    return result, problems


def assess_classification(payload, *, as_of, observations=()):
    """Check cited assessments against their explicit dates and metric evidence.

    No calendar cutoff or follower ceiling defines a new artist. That claim
    needs a documented debut, a cited prior-history review, and a separate
    early-career explanation. ``not_established`` means momentum is not
    established by this research; it does not label the artist unestablished.
    """
    as_of = _timestamp(as_of, "as_of")
    payload = normalize_classification(payload)
    career, momentum = payload["career_stage"], payload["momentum"]
    career_missing = _dated_requirements(career, as_of) if career["claim"] != "uncertain" else ["career_stage_assessment"]
    if career["claim"] in ("new_artist", "new_project"):
        release, prior, context = career["earliest_release"], career["prior_history"], career["career_context"]
        if release["basis"] != "documented_debut" or not release["published_at"] or not release["sources"]:
            career_missing.append("documented_debut_not_dataset_first_seen")
        if release["published_at"]:
            published = release["published_at"]
            future = (date.fromisoformat(published) > as_of.date() if len(published) == 10
                      else _timestamp(published, "earliest_release.published_at") > as_of)
            if future:
                career_missing.append("debut_in_future")
            if career["assessed_on"] and published[:10] > career["assessed_on"]:
                career_missing.append("debut_after_assessment")
        if not _has_research(prior) or prior["status"] in ("not_reviewed", "uncertain"):
            career_missing.append("cited_prior_history_review")
        required_prior = "none_found" if career["claim"] == "new_artist" else "new_project"
        if prior["status"] != required_prior:
            career_missing.append("no_prior_music_career" if career["claim"] == "new_artist" else "distinct_new_project_not_rename")
        if context["status"] != "early_career" or not _has_research(context):
            career_missing.append("cited_early_career_context")
    changes, observation_problems = _metric_changes(observations, as_of)
    momentum_missing = _dated_requirements(momentum, as_of) if momentum["claim"] != "uncertain" else ["momentum_assessment"]
    if momentum["claim"] == "emerging":
        if not any(change["status"] == "observed_change" and change["absolute_change"] > 0 for change in changes):
            momentum_missing.append("positive_comparable_repeat_observations")
        if observation_problems:
            momentum_missing.append("invalid_or_future_observations")
        corroboration = momentum["corroboration"]
        if corroboration["status"] != "broader_attention" or not _has_research(corroboration):
            momentum_missing.append("cited_attention_beyond_one_work")
    return {"rubric_version": RUBRIC_VERSION, "as_of": as_of.isoformat(),
            "career_stage": _axis_result(career, career_missing),
            "momentum": _axis_result(momentum, momentum_missing),
            "metric_changes": changes, "observation_problems": observation_problems,
            "limitations": [
                "Supported means the supplied research meets this rubric; citations were not fetched or verified by this helper.",
                "Career stage, audience momentum, recognition and listening quality are separate assessments.",
                "No default debut-age or follower threshold is applied; assessment applicability is explicit.",
            ]}
