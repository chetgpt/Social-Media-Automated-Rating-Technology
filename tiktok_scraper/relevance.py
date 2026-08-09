"""Reusable metadata relevance scoring for scrape candidates.

The scorer is intentionally deterministic and cheap. It runs before comment
fetching so noisy candidates can be rejected before expensive scraping starts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_ACCEPT_THRESHOLD = 70
DEFAULT_REVIEW_THRESHOLD = 40
TOKEN_STOPWORDS = {
    "a",
    "an",
    "and",
    "atau",
    "by",
    "dan",
    "di",
    "for",
    "from",
    "in",
    "ini",
    "ke",
    "of",
    "on",
    "or",
    "the",
    "to",
    "untuk",
    "with",
    "yang",
}
SHORT_TOPIC_TOKENS = {"ai", "ar", "bi", "hr", "it", "ml", "ui", "ux", "vr"}
PROFILE_TERM_FIELDS = (
    "core_terms",
    "anchor_terms",
    "ambiguous_terms",
    "token_terms",
    "hashtags",
    "aliases",
    "entities",
    "activation_terms",
    "context_terms",
    "generic_terms",
    "exclusion_terms",
    "trusted_sources",
)


def split_terms(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_terms = value
    elif isinstance(value, str):
        raw_terms = re.split(r"[\n,;|]+", value)
    else:
        raw_terms = []

    terms: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        text = str(term or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            terms.append(text)
    return terms


def normalize_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9#@._-]+", " ", str(value or "").lower()).strip()


def compact_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def extract_topic_tokens(values: list[str]) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            if token in TOKEN_STOPWORDS:
                continue
            if len(token) < 3 and token not in SHORT_TOPIC_TOKENS:
                continue
            if token not in seen:
                seen.add(token)
                tokens.append(token)
    return tokens


def default_anchor_terms(
    core_terms: list[str],
    hashtags: list[str] | None = None,
    aliases: list[str] | None = None,
    ambiguous_terms: list[str] | None = None,
) -> list[str]:
    """Infer terms that are specific enough to establish relevance on their own."""
    ambiguous = {compact_text(term) for term in (ambiguous_terms or []) if compact_text(term)}
    output: list[str] = []
    seen: set[str] = set()
    for term in [*core_terms, *(hashtags or []), *(aliases or [])]:
        compact = compact_text(term)
        if not compact or compact in ambiguous:
            continue
        tokens = re.findall(r"[a-z0-9]+", str(term or "").lower())
        # Three-character acronyms collide frequently. Campaigns can explicitly
        # opt them back in through anchor_terms when they really are unique.
        if len(tokens) == 1 and len(tokens[0]) <= 3:
            continue
        if compact not in seen:
            seen.add(compact)
            output.append(term)
    return output


def term_matches(term: str, normalized: str, compacted: str) -> bool:
    clean = normalize_text(term)
    compact = compact_text(term)
    if not clean and not compact:
        return False

    text_match = False
    if clean:
        if " " in clean:
            text_match = clean in normalized
        else:
            text_match = bool(re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", normalized))

    term_tokens = re.findall(r"[a-z0-9]+", str(term or "").lower())
    # Compact matching bridges phrases and hashtag/camel-case forms. Applying it
    # to a single word makes short tokens such as "pesta" match "pestapora".
    compact_match = bool(
        compact
        and len(compact) >= 4
        and len(term_tokens) >= 2
        and compact in compacted
    )
    return text_match or compact_match


def candidate_field(candidate: dict[str, Any], *names: str) -> str:
    for name in names:
        value = candidate.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def candidate_metadata(candidate: dict[str, Any]) -> dict[str, str]:
    text_parts: list[str] = []
    seen_parts: set[str] = set()
    for name in ("title", "caption", "desc", "description", "text"):
        value = candidate.get(name)
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen_parts:
            seen_parts.add(key)
            text_parts.append(text)
    for name in ("hashtags", "keywords"):
        value = candidate.get(name)
        values = value if isinstance(value, list) else []
        text = " ".join(str(item or "").strip() for item in values if str(item or "").strip())
        key = text.casefold()
        if text and key not in seen_parts:
            seen_parts.add(key)
            text_parts.append(text)

    title = text_parts[0] if text_parts else ""
    content_text = " ".join(text_parts)
    source = candidate_field(candidate, "username", "content_creator", "creator", "author")
    url = candidate_field(candidate, "url", "video_url", "permalink")
    matched_keywords = " ".join(split_terms(candidate.get("matched_keywords")))
    return {
        "title": title,
        "content_text": content_text,
        "source": source,
        "url": url,
        "matched_keywords": matched_keywords,
        # Content relevance must not be inferred from an account name or URL.
        "all_text": content_text,
    }


def is_weak_metadata(metadata: dict[str, str]) -> bool:
    content = normalize_text(metadata.get("content_text") or metadata.get("title"))
    if content in {"", "instagram", "tiktok", "youtube", "x", "twitter", "unknown", "direct url", "direct post"}:
        return True
    collapsed = re.sub(r"\s+", " ", content).strip()
    if re.fullmatch(r"(?:top liked\s+)?\d+(?:[.,]\d+)?[kmb]?", collapsed, flags=re.IGNORECASE):
        return True
    lexical_tokens = [token for token in re.findall(r"[a-z]+", collapsed) if token not in {"top", "liked"}]
    return not lexical_tokens


def build_auto_profile(topic: str = "", keywords: list[str] | None = None) -> dict[str, Any]:
    keywords = keywords or []
    core_terms = split_terms(topic)
    hashtags: list[str] = []
    aliases: list[str] = []
    for keyword in keywords:
        clean = keyword.strip()
        if not clean:
            continue
        if clean.startswith("#"):
            hashtags.append(clean.lstrip("#"))
        elif clean.casefold() not in {term.casefold() for term in core_terms}:
            aliases.append(clean)

    if not core_terms and aliases:
        core_terms = [aliases.pop(0)]

    return {
        "name": topic or (core_terms[0] if core_terms else "topic"),
        "core_terms": core_terms,
        "anchor_terms": default_anchor_terms(core_terms, hashtags, aliases),
        "ambiguous_terms": [],
        "token_terms": extract_topic_tokens([topic] + keywords),
        "hashtags": hashtags,
        "aliases": aliases,
        "entities": [],
        "activation_terms": [],
        "context_terms": [],
        "generic_terms": [],
        "exclusion_terms": [],
        "trusted_sources": [],
        "require_anchor": True,
        "accept_threshold": DEFAULT_ACCEPT_THRESHOLD,
        "review_threshold": DEFAULT_REVIEW_THRESHOLD,
    }


def load_topic_profile(path: str = "", topic: str = "", keywords: list[str] | None = None) -> dict[str, Any]:
    if path:
        with open(path, "r", encoding="utf-8-sig") as handle:
            profile = json.load(handle)
    else:
        profile = build_auto_profile(topic, keywords)

    profile = dict(profile)
    for key in PROFILE_TERM_FIELDS:
        profile[key] = split_terms(profile.get(key))
    if not profile["anchor_terms"]:
        profile["anchor_terms"] = default_anchor_terms(
            profile["core_terms"],
            profile["hashtags"],
            profile["aliases"],
            profile["ambiguous_terms"],
        )
    if not profile["token_terms"]:
        profile["token_terms"] = extract_topic_tokens(profile["anchor_terms"])
    profile["require_anchor"] = bool(profile.get("require_anchor", True))
    profile["accept_threshold"] = int(profile.get("accept_threshold") or DEFAULT_ACCEPT_THRESHOLD)
    profile["review_threshold"] = int(profile.get("review_threshold") or DEFAULT_REVIEW_THRESHOLD)
    return profile


def profile_from_env() -> tuple[dict[str, Any] | None, str, str]:
    mode = os.environ.get("SCRAPER_RELEVANCE_MODE", "off").strip().lower() or "off"
    review_action = os.environ.get("SCRAPER_RELEVANCE_REVIEW_ACTION", "skip").strip().lower() or "skip"
    raw_profile = os.environ.get("SCRAPER_RELEVANCE_PROFILE_JSON", "").strip()
    if not raw_profile or mode == "off":
        return None, mode, review_action
    try:
        profile = json.loads(raw_profile)
    except json.JSONDecodeError:
        return None, mode, review_action
    return load_topic_profile_from_dict(profile), mode, review_action


def load_topic_profile_from_dict(profile: dict[str, Any]) -> dict[str, Any]:
    pathless = dict(profile)
    for key in PROFILE_TERM_FIELDS:
        pathless[key] = split_terms(pathless.get(key))
    if not pathless["anchor_terms"]:
        pathless["anchor_terms"] = default_anchor_terms(
            pathless["core_terms"],
            pathless["hashtags"],
            pathless["aliases"],
            pathless["ambiguous_terms"],
        )
    if not pathless["token_terms"]:
        pathless["token_terms"] = extract_topic_tokens(pathless["anchor_terms"])
    pathless["require_anchor"] = bool(pathless.get("require_anchor", True))
    pathless["accept_threshold"] = int(pathless.get("accept_threshold") or DEFAULT_ACCEPT_THRESHOLD)
    pathless["review_threshold"] = int(pathless.get("review_threshold") or DEFAULT_REVIEW_THRESHOLD)
    return pathless


def score_candidate(candidate: dict[str, Any], profile: dict[str, Any], platform: str = "") -> dict[str, Any]:
    metadata = candidate_metadata(candidate)
    normalized = normalize_text(metadata["content_text"])
    compacted = compact_text(metadata["content_text"])
    query_normalized = normalize_text(metadata["matched_keywords"])
    query_compacted = compact_text(metadata["matched_keywords"])
    source_normalized = normalize_text(metadata["source"])
    source_compacted = compact_text(metadata["source"])
    url_normalized = normalize_text(metadata["url"])
    url_compacted = compact_text(metadata["url"])

    score = 0
    reasons: list[str] = []
    matched: dict[str, list[str]] = {}

    def matching_terms(terms: list[str], text: str = normalized, compact: str = compacted) -> list[str]:
        return [term for term in terms if term_matches(term, text, compact)]

    def add_matches(
        label: str,
        terms: list[str],
        points: int,
        max_points: int | None = None,
        *,
        text: str = normalized,
        compact: str = compacted,
    ) -> int:
        nonlocal score
        found = matching_terms(terms, text, compact)
        if not found:
            return 0
        gained = points * len(found)
        if max_points is not None:
            gained = min(gained, max_points)
        score += gained
        matched[label] = found
        reasons.append(f"{label}:{','.join(found[:5])} +{gained}")
        return len(found)

    ambiguous_compact = {
        compact_text(term)
        for term in profile.get("ambiguous_terms", [])
        if compact_text(term)
    }
    strong_core_terms = [
        term for term in profile.get("core_terms", [])
        if compact_text(term) not in ambiguous_compact
    ]
    ambiguous_core_terms = [
        term for term in profile.get("core_terms", [])
        if compact_text(term) in ambiguous_compact
    ]

    core_count = add_matches("core", strong_core_terms, 60, 90)
    ambiguous_count = add_matches("ambiguous", ambiguous_core_terms, 10, 20)
    hashtag_count = add_matches("hashtag", profile.get("hashtags", []), 55, 85)
    alias_count = add_matches("alias", profile.get("aliases", []), 30, 60)
    token_count = add_matches("topic_token", profile.get("token_terms", []), 20, 60)
    if token_count >= 2:
        score += 20
        reasons.append("topic_token_combo +20")
    entity_count = add_matches("entity", profile.get("entities", []), 15, 30)
    activation_count = add_matches("activation", profile.get("activation_terms", []), 10, 20)
    context_count = add_matches("context", profile.get("context_terms", []), 20, 40)
    generic_count = len(matching_terms(profile.get("generic_terms", [])))
    if generic_count:
        matched["generic"] = matching_terms(profile.get("generic_terms", []))

    trusted_terms = profile.get("trusted_sources", [])
    trusted_found = [
        term
        for term in trusted_terms
        if term_matches(term, source_normalized + " " + url_normalized, source_compacted + url_compacted)
    ]
    if trusted_found:
        score += 35
        matched["trusted_source"] = trusted_found
        reasons.append(f"trusted_source:{','.join(trusted_found[:5])} +35")

    query_terms = (
        profile.get("core_terms", [])
        + profile.get("hashtags", [])
        + profile.get("aliases", [])
    )
    query_found = [
        term
        for term in query_terms
        if term_matches(term, query_normalized, query_compacted)
    ]
    if query_found:
        score += 15
        matched["query"] = query_found
        reasons.append(f"query:{','.join(query_found[:5])} +15")

    generic_found = matching_terms(profile.get("generic_terms", []))
    if generic_found and not (
        core_count
        or hashtag_count
        or alias_count
        or token_count
        or entity_count
        or activation_count
        or context_count
        or trusted_found
    ):
        score -= 40
        matched["generic_only"] = generic_found
        reasons.append(f"generic_only:{','.join(generic_found[:5])} -40")

    weak_metadata = is_weak_metadata(metadata)
    if weak_metadata and not trusted_found:
        score -= 20
        reasons.append("weak_metadata -20")

    exclusion_found = [term for term in profile.get("exclusion_terms", []) if term_matches(term, normalized, compacted)]
    hard_exclusion = bool(exclusion_found and not (core_count or hashtag_count))
    if exclusion_found:
        penalty = min(120, 60 * len(exclusion_found))
        score -= penalty
        matched["exclusion"] = exclusion_found
        reasons.append(f"exclusion:{','.join(exclusion_found[:5])} -{penalty}")

    accept_threshold = int(profile.get("accept_threshold") or DEFAULT_ACCEPT_THRESHOLD)
    review_threshold = int(profile.get("review_threshold") or DEFAULT_REVIEW_THRESHOLD)

    anchor_found = matching_terms(profile.get("anchor_terms", []))
    if anchor_found:
        matched["anchor"] = anchor_found
    has_metadata_anchor = bool(anchor_found or hashtag_count)
    corroborated = bool(
        (
            entity_count
            and (
                context_count >= 2
                or (context_count and token_count)
                or (activation_count and context_count)
                or token_count >= 2
            )
        )
        or (activation_count and entity_count and (context_count or token_count))
        or (context_count and token_count >= 2)
        or (ambiguous_count and entity_count and (context_count or token_count))
    )
    require_anchor = bool(profile.get("require_anchor", True))
    eligible_for_accept = not require_anchor or has_metadata_anchor or corroborated
    needs_metadata_hydration = bool(trusted_found and (weak_metadata or not (has_metadata_anchor or corroborated)))

    if hard_exclusion:
        decision = "reject"
    elif score >= accept_threshold and eligible_for_accept:
        decision = "accept"
    elif score >= review_threshold or needs_metadata_hydration:
        decision = "review"
    else:
        decision = "reject"

    if score >= accept_threshold and not eligible_for_accept:
        reasons.append("anchor_required -> review")
    if needs_metadata_hydration:
        reasons.append("trusted_source_metadata_required -> review")

    return {
        "platform": platform,
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "matched": matched,
        "weak_metadata": weak_metadata,
        "needs_metadata_hydration": needs_metadata_hydration,
        "evidence": {
            "metadata_anchor": has_metadata_anchor,
            "corroborated": corroborated,
            "query_only": bool(query_found and not (has_metadata_anchor or corroborated)),
            "trusted_source_only": bool(trusted_found and not (has_metadata_anchor or corroborated)),
            "require_anchor": require_anchor,
        },
        "hard_exclusion": hard_exclusion,
        "candidate": {
            "video_id": candidate.get("video_id") or candidate.get("id") or candidate.get("aweme_id"),
            "url": candidate.get("url") or candidate.get("video_url"),
            "title": metadata["title"],
            "source": metadata["source"],
            "matched_keywords": split_terms(candidate.get("matched_keywords")),
        },
    }


def filter_candidates(
    candidates: list[dict[str, Any]],
    profile: dict[str, Any] | None,
    platform: str,
    mode: str = "off",
    review_action: str = "skip",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mode = (mode or "off").lower()
    review_action = (review_action or "skip").lower()
    if mode == "off" or not profile:
        return candidates, [], [], []

    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    scored: list[dict[str, Any]] = []

    for candidate in candidates:
        result = score_candidate(candidate, profile, platform=platform)
        candidate["_relevance"] = {
            key: value
            for key, value in result.items()
            if key != "candidate"
        }
        scored.append(result)
        decision = result["decision"]
        if decision == "accept":
            accepted.append(candidate)
        elif decision == "review":
            review.append(candidate)
            if mode == "filter" and review_action in {"scrape", "accept"}:
                accepted.append(candidate)
        else:
            rejected.append(candidate)

        if mode == "audit" and decision != "accept":
            accepted.append(candidate)

    return accepted, review, rejected, scored


def write_relevance_audit(folder: str | Path, platform: str, scored: list[dict[str, Any]], profile: dict[str, Any]) -> str:
    output_dir = Path(folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"relevance_{platform}_candidates.json"
    summary = {"accept": 0, "review": 0, "reject": 0}
    for item in scored:
        summary[item["decision"]] = summary.get(item["decision"], 0) + 1
    payload = {
        "platform": platform,
        "profile_name": profile.get("name", ""),
        "summary": summary,
        "candidates": scored,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return str(path)
