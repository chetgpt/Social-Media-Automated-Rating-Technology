"""Parsers for profile data loaded by authenticated platform web clients.

These adapters do not bypass authentication or manufacture request signatures.
They parse public or account-visible profile responses already loaded by the
dedicated social browser, plus embedded page data used by those same clients.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from tiktok_scraper.profile_contract import normalize_username, optional_int, text_value


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    stack = [value]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def decode_json_payloads(raw: Any) -> list[Any]:
    """Decode ordinary JSON, Facebook's guard prefix, or JSON-lines payloads."""

    if isinstance(raw, (dict, list)):
        return [raw]
    text = text_value(raw)
    if not text:
        return []
    if text.startswith("for (;;);"):
        text = text[len("for (;;);") :].lstrip()
    try:
        return [json.loads(text)]
    except (TypeError, ValueError):
        pass

    payloads: list[Any] = []
    for line in text.splitlines():
        candidate = line.strip()
        if candidate.startswith("for (;;);"):
            candidate = candidate[len("for (;;);") :].lstrip()
        if not candidate or candidate[0] not in "[{":
            continue
        try:
            payloads.append(json.loads(candidate))
        except (TypeError, ValueError):
            continue
    return payloads


def profile_payload_shape_hints(
    payload: Any,
    identity: Any,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return key-only diagnostics for containers around an exact identity."""

    needle = text_value(identity)
    if not needle:
        return []
    hints: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(value: Any, path: tuple[str, ...]) -> bool:
        if len(hints) >= limit:
            return False
        if isinstance(value, list):
            matched = False
            for index, item in enumerate(value):
                if visit(item, (*path, f"[{index}]")):
                    matched = True
            return matched
        if not isinstance(value, dict):
            return False
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        direct = any(
            (
                text_value(item) == needle
                or (
                    isinstance(item, str)
                    and f"/{needle}" in item
                )
            )
            for item in value.values()
            if isinstance(item, (str, int))
        )
        child_match = False
        for key, child in value.items():
            if isinstance(child, (dict, list)) and visit(child, (*path, str(key))):
                child_match = True
        if direct or child_match:
            hints.append(
                {
                    "path": ".".join(path[-8:]),
                    "keys": sorted(text_value(key) for key in value)[:60],
                    "typename": text_value(
                        value.get("__typename") or value.get("@type")
                    ),
                    "direct_identity_match": direct,
                }
            )
        return direct or child_match

    visit(payload, ())
    return hints[:limit]


def profile_response_url_matches(platform: str, url: str) -> bool:
    lowered = text_value(url).casefold()
    platform = text_value(platform).casefold()
    if platform == "tiktok":
        return "/api/user/detail/" in lowered
    if platform == "instagram":
        return "/api/v1/users/web_profile_info/" in lowered
    if platform == "x":
        return (
            "/i/api/graphql/" in lowered
            and any(
                operation in lowered
                for operation in (
                    "/userbyscreenname",
                    "/userbyrestid",
                    "/profile",
                )
            )
        )
    if platform == "youtube":
        return "/youtubei/v1/browse" in lowered
    if platform == "facebook":
        return "/api/graphql" in lowered
    return False


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _first(*values: Any) -> Any:
    for value in values:
        if _present(value):
            return value
    return None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _nested(root: Any, *keys: str) -> Any:
    current = root
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _image_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip() if value.startswith(("http://", "https://")) else ""
    candidates: list[tuple[int, str]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                candidates.append((0, item))
    for node in walk_dicts(value):
        url = text_value(_first(node.get("url"), node.get("uri"), node.get("src")))
        if not url.startswith(("http://", "https://")):
            continue
        width = optional_int(node.get("width")) or 0
        candidates.append((width, url))
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1]


def _count(value: Any) -> int | None:
    direct = optional_int(value)
    if direct is not None:
        return direct
    text = text_value(value).replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kmb]?)", text, re.I)
    if not match:
        return None
    return optional_int(f"{match.group(1)}{match.group(2)}")


def _url_handle(platform: str, value: Any) -> str:
    url = text_value(value)
    if not url:
        return ""
    try:
        parts = [part for part in urlparse(url).path.split("/") if part]
    except ValueError:
        return ""
    if not parts:
        return ""
    if platform == "youtube":
        return parts[0][1:] if parts[0].startswith("@") else ""
    return parts[0].lstrip("@")


def _identity_match(
    platform: str,
    record: dict[str, Any],
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> bool:
    expected_username = normalize_username(platform, expected_username).casefold()
    username = normalize_username(platform, record.get("username")).casefold()
    expected_user_id = text_value(expected_user_id)
    user_id = text_value(record.get("user_id"))
    if expected_user_id and user_id and expected_user_id == user_id:
        return True
    if expected_username and username and expected_username == username:
        return True
    profile_handle = _url_handle(platform, record.get("profile_url")).casefold()
    return bool(expected_username and profile_handle and expected_username == profile_handle)


def _record_score(
    platform: str,
    record: dict[str, Any],
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> int:
    score = 100 if _identity_match(
        platform,
        record,
        expected_username=expected_username,
        expected_user_id=expected_user_id,
    ) else 0
    score += sum(
        1
        for key in (
            "user_id",
            "username",
            "display_name",
            "bio",
            "website",
            "avatar_url",
            "location",
            "country",
            "country_code",
            "follower_count",
            "following_count",
            "content_count",
            "likes_count",
            "pronouns",
            "birthdate",
        )
        if _present(record.get(key))
    )
    return score


def _best_record(
    platform: str,
    records: list[dict[str, Any]],
    *,
    expected_username: str = "",
    expected_user_id: str = "",
    require_identity_when_ambiguous: bool = False,
) -> dict[str, Any]:
    if not records:
        return {}
    best = max(
        records,
        key=lambda record: _record_score(
            platform,
            record,
            expected_username=expected_username,
            expected_user_id=expected_user_id,
        ),
    )
    if (
        require_identity_when_ambiguous
        and len(records) > 1
        and (expected_username or expected_user_id)
        and not _identity_match(
            platform,
            best,
            expected_username=expected_username,
            expected_user_id=expected_user_id,
        )
    ):
        return {}
    return best


def _as_pronouns(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = _first(item.get("text"), item.get("value"), item.get("name"))
        text = text_value(item)
        if text and text not in output:
            output.append(text)
    return output


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return text_value(value)
    for key in ("text", "value", "name", "label", "title"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            nested = _plain_text(candidate)
            if nested:
                return nested
    return ""


def _birthdate(value: Any) -> str:
    if not isinstance(value, dict):
        return text_value(value)
    parts = [
        text_value(value.get("year")),
        text_value(value.get("month")).zfill(2),
        text_value(value.get("day")).zfill(2),
    ]
    return "-".join(part for part in parts if part and part != "00")


def parse_tiktok_profile_payload(
    payload: Any,
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in walk_dicts(payload):
        info = _dict(node.get("userInfo"))
        user = _dict(info.get("user"))
        stats = _dict(info.get("stats") or info.get("statsV2"))
        if not user:
            possible = _dict(node.get("user"))
            if possible and _first(
                possible.get("uniqueId"),
                possible.get("unique_id"),
                possible.get("nickname"),
            ):
                user = possible
                stats = _dict(node.get("stats") or node.get("statsV2"))
        if not user and _first(node.get("uniqueId"), node.get("unique_id")):
            user = node
        if not user:
            continue

        username = text_value(_first(user.get("uniqueId"), user.get("unique_id")))
        user_id = text_value(_first(user.get("id"), user.get("uid"), user.get("userId")))
        marker = (user_id, username.casefold())
        if marker in seen or not any(marker):
            continue
        seen.add(marker)
        bio_link = _dict(user.get("bioLink") or user.get("bio_link"))
        commerce = _dict(user.get("commerceUserInfo") or user.get("commerce_user_info"))
        region = text_value(_first(user.get("region"), user.get("countryCode")))
        country_code = region.upper() if re.fullmatch(r"[A-Za-z]{2}", region) else ""
        record = {
            "platform": "tiktok",
            "user_id": user_id,
            "username": username,
            "display_name": _first(user.get("nickname"), user.get("displayName")),
            "bio": _first(user.get("signature"), user.get("bioDescription")),
            "website": _first(bio_link.get("link"), user.get("bioUrl")),
            "avatar_url": _first(
                user.get("avatarLarger"),
                user.get("avatarMedium"),
                user.get("avatarThumb"),
            ),
            "verified": _first(user.get("verified"), user.get("isVerified")),
            "protected": _first(user.get("privateAccount"), user.get("isPrivate")),
            "account_type": (
                "business"
                if _first(user.get("commerceUser"), commerce.get("commerceUser"))
                else "personal_or_creator"
            ),
            "category": _first(
                commerce.get("category"),
                commerce.get("categoryLabel"),
                user.get("category"),
            ),
            "created_at": _first(user.get("createTime"), user.get("createdAt")),
            "follower_count": _first(stats.get("followerCount"), stats.get("follower_count")),
            "following_count": _first(stats.get("followingCount"), stats.get("following_count")),
            "content_count": _first(stats.get("videoCount"), stats.get("video_count")),
            "likes_count": _first(
                stats.get("heartCount"),
                stats.get("heart"),
                stats.get("likesCount"),
            ),
            "country_code": country_code,
            "region": "" if country_code else region,
            "default_language": _first(user.get("language"), user.get("lang")),
            "pronouns": _as_pronouns(user.get("pronouns")),
            "geography_basis": "platform_account_region" if region else "",
            "_method": "tiktok_web_user_detail_api",
        }
        records.append(record)
    return _best_record(
        "tiktok",
        records,
        expected_username=expected_username,
        expected_user_id=expected_user_id,
    )


def _instagram_location(user: dict[str, Any]) -> dict[str, Any]:
    address = user.get("business_address_json")
    if isinstance(address, str):
        try:
            address = json.loads(address)
        except (TypeError, ValueError):
            address = {}
    address = _dict(address)
    city = text_value(_first(user.get("city_name"), address.get("city_name"), address.get("city")))
    region = text_value(_first(address.get("region_name"), address.get("region")))
    country = text_value(_first(address.get("country_name"), address.get("country")))
    street = text_value(_first(user.get("address_street"), address.get("street_address")))
    postal = text_value(_first(user.get("zip"), address.get("zip_code"), address.get("postal_code")))
    raw = ", ".join(part for part in (street, city, region, postal, country) if part)
    return {
        "raw": raw,
        "city": city,
        "region": region,
        "country": country,
        "latitude": _first(address.get("latitude"), address.get("lat")),
        "longitude": _first(address.get("longitude"), address.get("lng")),
    }


def parse_instagram_profile_payload(
    payload: Any,
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in walk_dicts(payload):
        user = _dict(node.get("user"))
        if not user and _first(node.get("username"), node.get("biography")):
            user = node
        username = text_value(user.get("username"))
        user_id = text_value(_first(user.get("id"), user.get("pk"), user.get("pk_id")))
        if not user or not (username or user_id):
            continue
        marker = (user_id, username.casefold())
        if marker in seen:
            continue
        seen.add(marker)
        followed = _dict(user.get("edge_followed_by"))
        follows = _dict(user.get("edge_follow"))
        media = _dict(user.get("edge_owner_to_timeline_media"))
        hd_picture = _dict(user.get("hd_profile_pic_url_info"))
        location = _instagram_location(user)
        professional = any(
            bool(user.get(key))
            for key in (
                "is_business_account",
                "is_professional_account",
                "professional_account",
            )
        )
        records.append(
            {
                "platform": "instagram",
                "user_id": user_id,
                "username": username,
                "display_name": user.get("full_name"),
                "bio": user.get("biography"),
                "website": _first(user.get("external_url"), user.get("website")),
                "avatar_url": _first(
                    user.get("profile_pic_url_hd"),
                    hd_picture.get("url"),
                    user.get("profile_pic_url"),
                ),
                "verified": user.get("is_verified"),
                "protected": user.get("is_private"),
                "account_type": (
                    text_value(user.get("account_type"))
                    or ("professional" if professional else "personal_or_creator")
                ),
                "category": _first(
                    user.get("category_name"),
                    user.get("business_category_name"),
                ),
                "follower_count": _first(user.get("follower_count"), followed.get("count")),
                "following_count": _first(user.get("following_count"), follows.get("count")),
                "content_count": _first(user.get("media_count"), media.get("count")),
                "location": location if any(location.values()) else {},
                "pronouns": _as_pronouns(user.get("pronouns")),
                "geography_basis": (
                    "public_business_address" if any(location.values()) else ""
                ),
                "_method": "instagram_web_profile_api",
            }
        )
    return _best_record(
        "instagram",
        records,
        expected_username=expected_username,
        expected_user_id=expected_user_id,
    )


def _x_website(user: dict[str, Any], legacy: dict[str, Any]) -> str:
    entities = _dict(legacy.get("entities"))
    url_entity = _dict(entities.get("url"))
    urls = _list(url_entity.get("urls"))
    if urls and isinstance(urls[0], dict):
        return text_value(
            _first(urls[0].get("expanded_url"), urls[0].get("display_url"), urls[0].get("url"))
        )
    return text_value(_first(user.get("website"), legacy.get("url")))


def parse_x_profile_payload(
    payload: Any,
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in walk_dicts(payload):
        typename = text_value(node.get("__typename"))
        legacy = _dict(node.get("legacy"))
        core = _dict(node.get("core"))
        username = text_value(
            _first(core.get("screen_name"), legacy.get("screen_name"), node.get("screen_name"))
        )
        user_id = text_value(_first(node.get("rest_id"), node.get("id_str"), node.get("id")))
        looks_like_user = (
            typename.casefold().startswith("user")
            or bool(username and _first(core.get("name"), legacy.get("name"), node.get("name")))
        )
        if not looks_like_user or not (username or user_id):
            continue
        marker = (user_id, username.casefold())
        if marker in seen:
            continue
        seen.add(marker)
        professional = _dict(node.get("professional"))
        categories = [
            text_value(_first(item.get("name"), item.get("label")))
            for item in _list(professional.get("category"))
            if isinstance(item, dict)
        ]
        records.append(
            {
                "platform": "x",
                "user_id": user_id,
                "username": username,
                "display_name": _first(core.get("name"), legacy.get("name"), node.get("name")),
                "bio": _first(
                    legacy.get("description"),
                    node.get("description"),
                    core.get("description"),
                ),
                "website": _x_website(node, legacy),
                "avatar_url": _first(
                    _nested(node, "avatar", "image_url"),
                    _nested(node, "avatar", "url"),
                    legacy.get("profile_image_url_https"),
                    node.get("profile_image_url"),
                ),
                "verified": (
                    True
                    if any(
                        value is True
                        for value in (
                            node.get("is_blue_verified"),
                            legacy.get("verified"),
                            node.get("verified"),
                        )
                    )
                    else _first(
                        node.get("is_blue_verified"),
                        legacy.get("verified"),
                        node.get("verified"),
                    )
                ),
                "protected": _first(legacy.get("protected"), node.get("protected")),
                "account_type": _first(
                    professional.get("professional_type"),
                    node.get("account_type"),
                ),
                "category": "|".join(category for category in categories if category),
                "created_at": _first(core.get("created_at"), legacy.get("created_at")),
                "follower_count": legacy.get("followers_count"),
                "following_count": legacy.get("friends_count"),
                "content_count": _first(
                    legacy.get("statuses_count"),
                    legacy.get("media_count"),
                ),
                "likes_count": legacy.get("favourites_count"),
                "location": legacy.get("location"),
                "default_language": legacy.get("lang"),
                "birthdate": _birthdate(_first(node.get("birthdate"), legacy.get("birthdate"))),
                "geography_basis": (
                    "self_declared_profile_location" if legacy.get("location") else ""
                ),
                "_method": "x_web_user_graphql",
            }
        )
    return _best_record(
        "x",
        records,
        expected_username=expected_username,
        expected_user_id=expected_user_id,
        require_identity_when_ambiguous=True,
    )


def _yt_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    runs = value.get("runs")
    if isinstance(runs, list):
        joined = "".join(
            text_value(item.get("text"))
            for item in runs
            if isinstance(item, dict)
        ).strip()
        if joined:
            return joined
    for key in ("simpleText", "content", "text", "label"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            nested = _yt_text(candidate)
            if nested:
                return nested
    for key in ("dynamicTextViewModel", "attributedDescription"):
        nested = _yt_text(value.get(key))
        if nested:
            return nested
    return ""


def _all_strings(value: Any) -> list[str]:
    output: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            stripped = current.strip()
            if stripped:
                output.append(stripped)
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return output


def _find_channel_id(value: Any) -> str:
    for node in walk_dicts(value):
        for key in ("externalId", "channelId", "browseId"):
            candidate = text_value(node.get(key))
            if candidate.startswith("UC"):
                return candidate
    return ""


def _yt_count_from_strings(strings: list[str], marker: str) -> int | None:
    for value in strings:
        if marker in value.casefold():
            parsed = _count(value)
            if parsed is not None:
                return parsed
    return None


def parse_youtube_profile_payload(
    payload: Any,
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    header: dict[str, Any] = {}
    page_header: dict[str, Any] = {}
    about: dict[str, Any] = {}
    microformat: dict[str, Any] = {}
    for node in walk_dicts(payload):
        metadata = metadata or _dict(node.get("channelMetadataRenderer"))
        header = header or _dict(node.get("c4TabbedHeaderRenderer"))
        page_header = page_header or _dict(node.get("pageHeaderViewModel"))
        about = about or _dict(node.get("aboutChannelViewModel"))
        microformat = microformat or _dict(node.get("microformatDataRenderer"))

    if not any((metadata, header, page_header, about, microformat)):
        return {}
    strings = _all_strings([header, page_header, about])
    user_id = _first(
        metadata.get("externalId"),
        header.get("channelId"),
        _find_channel_id(payload),
        expected_user_id,
    )
    canonical_url = text_value(
        _first(
            metadata.get("vanityChannelUrl"),
            metadata.get("channelUrl"),
            about.get("canonicalChannelUrl"),
            microformat.get("urlCanonical"),
        )
    )
    username = (
        normalize_username("youtube", expected_username)
        or _url_handle("youtube", canonical_url)
    )
    subscriber_count = _first(
        _count(_yt_text(header.get("subscriberCountText"))),
        _count(_yt_text(about.get("subscriberCountText"))),
        _yt_count_from_strings(strings, "subscriber"),
    )
    content_count = _first(
        _count(_yt_text(header.get("videosCountText"))),
        _count(_yt_text(about.get("videoCountText"))),
        _yt_count_from_strings(strings, "video"),
    )
    view_count = _first(
        _count(_yt_text(about.get("viewCountText"))),
        _yt_count_from_strings(strings, "view"),
    )
    country = text_value(_first(about.get("country"), header.get("country")))
    record = {
        "platform": "youtube",
        "user_id": user_id,
        "username": username,
        "display_name": _first(
            metadata.get("title"),
            header.get("title"),
            _yt_text(page_header.get("title")),
            microformat.get("title"),
        ),
        "bio": _first(
            metadata.get("description"),
            about.get("description"),
            microformat.get("description"),
        ),
        "profile_url": canonical_url,
        "avatar_url": _first(
            _image_url(metadata.get("avatar")),
            _image_url(header.get("avatar")),
            _image_url(page_header.get("image")),
            _image_url(microformat.get("thumbnail")),
        ),
        "verified": bool(header.get("badges")) if header.get("badges") is not None else None,
        "account_type": "youtube_channel",
        "created_at": _yt_text(about.get("joinedDateText")),
        "follower_count": subscriber_count,
        "content_count": content_count,
        "view_count": view_count,
        "country": country,
        "default_language": _first(
            metadata.get("defaultLanguage"),
            microformat.get("language"),
        ),
        "geography_basis": "channel_country" if country else "",
        "_method": "youtube_web_innertube_profile",
    }
    if not any(
        _present(record.get(key))
        for key in ("user_id", "username", "display_name", "bio", "avatar_url")
    ):
        return {}
    return record


def _facebook_picture(node: dict[str, Any]) -> str:
    return _first(
        _image_url(node.get("profile_picture")),
        _image_url(node.get("picture")),
        _image_url(node.get("image")),
        _image_url(node.get("profilePicLarge")),
        _image_url(node.get("profilePicMedium")),
        _image_url(node.get("profilePhoto")),
        _image_url(node.get("user_avatar")),
    ) or ""


def _facebook_location(node: dict[str, Any]) -> tuple[Any, str]:
    location = _first(
        node.get("location"),
        node.get("address"),
        node.get("current_city"),
        node.get("hometown"),
    )
    if isinstance(location, dict):
        city = _first(
            location.get("city"),
            location.get("city_name"),
            _nested(location, "city", "name"),
            location.get("name"),
        )
        region = _first(location.get("region"), location.get("region_name"), location.get("state"))
        country = _first(location.get("country"), location.get("country_name"))
        raw = _first(location.get("full_address"), location.get("single_line_address"))
        if not raw:
            raw = ", ".join(text_value(part) for part in (city, region, country) if text_value(part))
        return (
            {
                "raw": raw,
                "city": city,
                "region": region,
                "country": country,
                "country_code": location.get("country_code"),
                "latitude": _first(location.get("latitude"), location.get("lat")),
                "longitude": _first(location.get("longitude"), location.get("lng")),
            },
            "public_business_address"
            if text_value(node.get("__typename")) == "Page"
            or text_value(node.get("@type")) in {"Organization", "LocalBusiness"}
            else "self_declared_profile_location",
        )
    return (location, "self_declared_profile_location" if location else "")


def _facebook_geography_evidence(
    node: dict[str, Any],
    *,
    source_surface: str = "facebook_profile_graphql",
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    fields = (
        ("current_city", "current_city", "self_declared_profile_location"),
        ("hometown", "hometown", "self_declared_profile_location"),
        ("address", "business_address", "public_business_address"),
        ("location", "profile_location", ""),
    )
    seen: set[tuple[str, str, str, str, str]] = set()
    for field, kind, default_basis in fields:
        value = node.get(field)
        if not _present(value):
            continue
        location, detected_basis = _facebook_location({field: value})
        if not _present(location):
            continue
        basis = default_basis or detected_basis
        location_dict = _dict(location)
        marker = (
            kind,
            text_value(
                location_dict.get("raw")
                if location_dict
                else location
            ),
            text_value(location_dict.get("city")),
            text_value(location_dict.get("country")),
            basis,
        )
        if marker in seen:
            continue
        seen.add(marker)
        evidence.append(
            {
                "kind": kind,
                "location": location,
                "geography_basis": basis,
                "source_surface": source_surface,
            }
        )
    return evidence


_FB_VISIBILITY_LABELS = {
    "public",
    "friends",
    "only me",
    "custom",
    "publik",
    "teman",
    "hanya saya",
    "khusus",
}

_FB_FIELD_LABELS = {
    "current city": "current_city",
    "current town/city": "current_city",
    "lives in": "current_city",
    "kota saat ini": "current_city",
    "tempat tinggal": "current_city",
    "tinggal di": "current_city",
    "hometown": "hometown",
    "home town": "hometown",
    "from": "hometown",
    "kota asal": "hometown",
    "berasal dari": "hometown",
    "address": "business_address",
    "business address": "business_address",
    "alamat": "business_address",
    "alamat bisnis": "business_address",
    "pronouns": "pronouns",
    "pronoun": "pronouns",
    "kata ganti": "pronouns",
    "birthday": "birthdate",
    "birth date": "birthdate",
    "date of birth": "birthdate",
    "tanggal lahir": "birthdate",
    "gender": "gender",
    "jenis kelamin": "gender",
}


def _facebook_visible_value(lines: list[str], index: int) -> str:
    for value in lines[index + 1 :]:
        normalized = value.casefold().strip(" :")
        if normalized in _FB_VISIBILITY_LABELS:
            continue
        if normalized in _FB_FIELD_LABELS:
            return ""
        return value.strip()
    return ""


def parse_facebook_about_text_blocks(
    blocks: Iterable[Any],
    *,
    section: str,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    """Parse only explicitly labelled, visibly rendered Facebook About fields."""

    geography_evidence: list[dict[str, Any]] = []
    pronouns: list[str] = []
    birthdate = ""
    gender_statement = ""
    seen_geo: set[tuple[str, str]] = set()

    def add_geography(kind: str, value: str) -> None:
        cleaned = value.strip(" \t:.-")
        if not cleaned:
            return
        marker = (kind, cleaned.casefold())
        if marker in seen_geo:
            return
        seen_geo.add(marker)
        geography_evidence.append(
            {
                "kind": kind,
                "location": cleaned,
                "geography_basis": (
                    "public_business_address"
                    if kind == "business_address"
                    else "self_declared_profile_location"
                ),
                "source_surface": f"facebook_{section}_visible",
            }
        )

    prefix_patterns = (
        ("current_city", re.compile(r"^(?:lives in|tinggal di)\s+(.+)$", re.I)),
        ("hometown", re.compile(r"^(?:from|berasal dari)\s+(.+)$", re.I)),
    )
    for block in blocks:
        if not isinstance(block, str):
            continue
        lines = [
            line.strip()
            for line in block.replace("\r", "\n").split("\n")
            if line.strip()
        ]
        for index, line in enumerate(lines):
            for kind, pattern in prefix_patterns:
                match = pattern.match(line)
                if match:
                    add_geography(kind, match.group(1))
            normalized = line.casefold().strip(" :")
            field = _FB_FIELD_LABELS.get(normalized)
            if not field:
                continue
            value = _facebook_visible_value(lines, index)
            if not value:
                continue
            if field in {"current_city", "hometown", "business_address"}:
                add_geography(field, value)
            elif field == "pronouns":
                for pronoun in _as_pronouns(value):
                    if pronoun not in pronouns:
                        pronouns.append(pronoun)
            elif field == "birthdate" and not birthdate:
                birthdate = value
            elif field == "gender" and not gender_statement:
                gender_statement = value

    if not any((geography_evidence, pronouns, birthdate, gender_statement)):
        return {}
    primary = geography_evidence[0].get("location") if geography_evidence else None
    primary_basis = (
        geography_evidence[0].get("geography_basis")
        if geography_evidence
        else ""
    )
    return {
        "platform": "facebook",
        "user_id": expected_user_id,
        "username": expected_username,
        "location": primary,
        "location_kind": (
            geography_evidence[0].get("kind") if geography_evidence else ""
        ),
        "location_source_surface": (
            geography_evidence[0].get("source_surface")
            if geography_evidence
            else ""
        ),
        "geography_basis": primary_basis,
        "geography_evidence": geography_evidence,
        "pronouns": pronouns,
        "birthdate": birthdate,
        "gender_statement": gender_statement,
        "_method": "facebook_public_about_dom",
    }


def _facebook_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for node in walk_dicts(payload):
        json_type = node.get("@type")
        if json_type == "ProfilePage" and isinstance(node.get("mainEntity"), dict):
            candidates.append(dict(node["mainEntity"]))
            continue
        typename = text_value(_first(node.get("__typename"), json_type))
        if typename in {
            "Page",
            "User",
            "Profile",
            "Person",
            "Organization",
            "LocalBusiness",
            "PublicFigure",
        } and _first(node.get("name"), node.get("title")):
            candidates.append(node)
    return candidates


def parse_facebook_profile_payload(
    payload: Any,
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    candidates = _facebook_candidates(payload)
    for node in walk_dicts(payload):
        node_id = text_value(_first(node.get("id"), node.get("profile_id")))
        node_username = normalize_username(
            "facebook",
            _first(
                node.get("username"),
                node.get("username_for_profile"),
                node.get("vanity"),
                _url_handle("facebook", _first(node.get("url"), node.get("profile_url"))),
            ),
        )
        if (
            expected_user_id
            and node_id == text_value(expected_user_id)
            and _first(node.get("name"), node.get("title"))
        ) or (
            expected_username
            and node_username.casefold()
            == normalize_username("facebook", expected_username).casefold()
            and _first(node.get("name"), node.get("title"))
        ):
            candidates.append(node)
    for node in candidates:
        profile_url = text_value(_first(node.get("url"), node.get("profile_url")))
        username = text_value(
            _first(
                node.get("username"),
                node.get("username_for_profile"),
                node.get("vanity"),
                _url_handle("facebook", profile_url),
            )
        )
        user_id = text_value(_first(node.get("id"), node.get("profile_id")))
        display_name = text_value(_first(node.get("name"), node.get("title")))
        marker = (user_id, username.casefold(), display_name.casefold())
        if marker in seen:
            continue
        seen.add(marker)
        intro = _dict(node.get("profile_intro_card"))
        geography_evidence = _facebook_geography_evidence(node)
        geography_evidence.extend(
            _facebook_geography_evidence(
                intro,
                source_surface="facebook_profile_intro_graphql",
            )
        )
        deduped_geography: list[dict[str, Any]] = []
        seen_geography: set[tuple[str, str, str]] = set()
        for evidence in geography_evidence:
            value = evidence.get("location")
            location_dict = _dict(value)
            marker = (
                text_value(evidence.get("kind")),
                text_value(
                    location_dict.get("raw")
                    or location_dict.get("city")
                    or value
                ).casefold(),
                text_value(evidence.get("geography_basis")),
            )
            if marker in seen_geography:
                continue
            seen_geography.add(marker)
            deduped_geography.append(evidence)
        geography_evidence = deduped_geography
        if geography_evidence:
            location = geography_evidence[0].get("location")
            geography_basis = text_value(
                geography_evidence[0].get("geography_basis")
            )
        else:
            location, geography_basis = _facebook_location(node)
            if not location and intro:
                location, geography_basis = _facebook_location(intro)
        typename = text_value(_first(node.get("__typename"), node.get("@type")))
        records.append(
            {
                "platform": "facebook",
                "user_id": user_id,
                "username": username,
                "display_name": display_name,
                "profile_url": profile_url,
                "bio": _first(
                    node.get("about"),
                    node.get("description"),
                    node.get("bio"),
                    node.get("short_description"),
                    _plain_text(node.get("profile_status")),
                    _plain_text(intro.get("profile_status")),
                    intro.get("description"),
                ),
                "website": _first(node.get("website"), node.get("external_url")),
                "avatar_url": _facebook_picture(node),
                "verified": (
                    True
                    if any(
                        value is True
                        for value in (
                            node.get("is_verified"),
                            node.get("verified"),
                            node.get("show_verified_badge_on_profile"),
                        )
                    )
                    else _first(
                        node.get("is_verified"),
                        node.get("verified"),
                        node.get("show_verified_badge_on_profile"),
                    )
                ),
                "protected": _first(node.get("is_private"), node.get("protected")),
                "account_type": typename,
                "category": _first(node.get("category_name"), node.get("category")),
                "created_at": _first(node.get("creation_time"), node.get("created_time")),
                "follower_count": _first(
                    node.get("followers_count"),
                    node.get("follower_count"),
                    node.get("fan_count"),
                ),
                "following_count": node.get("following_count"),
                "content_count": _first(node.get("posts_count"), node.get("media_count")),
                "location": location,
                "location_kind": (
                    geography_evidence[0].get("kind")
                    if geography_evidence
                    else "profile_location"
                ),
                "geography_evidence": geography_evidence,
                "pronouns": _as_pronouns(
                    _first(node.get("pronouns"), intro.get("pronouns"))
                ),
                "gender_statement": _plain_text(
                    _first(
                        node.get("gender_text"),
                        node.get("public_gender"),
                        intro.get("gender_text"),
                    )
                ),
                "birthdate": _birthdate(
                    _first(
                        node.get("public_birthdate"),
                        node.get("birthdate_text"),
                        node.get("birthday_text"),
                        intro.get("public_birthdate"),
                        intro.get("birthdate_text"),
                        intro.get("birthday_text"),
                    )
                ),
                "geography_basis": geography_basis,
                "_method": "facebook_web_profile_graphql_or_structured_data",
            }
        )
    return _best_record(
        "facebook",
        records,
        expected_username=expected_username,
        expected_user_id=expected_user_id,
        require_identity_when_ambiguous=True,
    )


PARSERS = {
    "tiktok": parse_tiktok_profile_payload,
    "instagram": parse_instagram_profile_payload,
    "x": parse_x_profile_payload,
    "youtube": parse_youtube_profile_payload,
    "facebook": parse_facebook_profile_payload,
}


def parse_profile_payload(
    platform: str,
    payload: Any,
    *,
    expected_username: str = "",
    expected_user_id: str = "",
) -> dict[str, Any]:
    parser = PARSERS.get(text_value(platform).casefold())
    if parser is None:
        return {}
    return parser(
        payload,
        expected_username=expected_username,
        expected_user_id=expected_user_id,
    )


def merge_profile_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Merge records in priority order, retaining falsy but meaningful values."""

    merged: dict[str, Any] = {}
    methods: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not record:
            continue
        method = text_value(record.get("_method"))
        if method and method not in methods:
            methods.append(method)
        for key, value in record.items():
            if key == "_method" or not _present(value):
                continue
            if isinstance(value, dict):
                current = _dict(merged.get(key))
                for nested_key, nested_value in value.items():
                    if _present(nested_value) and not _present(current.get(nested_key)):
                        current[nested_key] = nested_value
                if current:
                    merged[key] = current
                continue
            if key in {"geography_evidence", "profile_geography_evidence"}:
                existing_values = merged.get(key)
                current_values = (
                    list(existing_values)
                    if isinstance(existing_values, list)
                    else []
                )
                seen_values = {
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    for item in current_values
                    if isinstance(item, dict)
                }
                for item in value if isinstance(value, list) else []:
                    if not isinstance(item, dict):
                        continue
                    marker = json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if marker in seen_values:
                        continue
                    seen_values.add(marker)
                    current_values.append(item)
                if current_values:
                    merged[key] = current_values
                continue
            if key == "pronouns":
                current_values = _as_pronouns(merged.get(key))
                for pronoun in _as_pronouns(value):
                    if pronoun not in current_values:
                        current_values.append(pronoun)
                if current_values:
                    merged[key] = current_values
                continue
            if not _present(merged.get(key)):
                merged[key] = value
    if methods:
        merged["_method"] = methods[0]
        merged["_supporting_methods"] = methods[1:]
    return merged
