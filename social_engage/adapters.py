"""Official API adapters. No browser, media acquisition, or automatic retries.

All writes require a caller-supplied final guard. The CLI supplies the durable
authorization/duplicate/freshness guard, immediately before each HTTP write.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import quote, urlsplit

import requests


class AdapterError(RuntimeError):
    """Closed error code only; never expose provider payloads or credentials."""


def numeric(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,39}", value):
        raise AdapterError("invalid_numeric_api_id")
    return value


def username(value):
    if not isinstance(value, str):
        raise AdapterError("invalid_username")
    value = str(value).lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_.]{1,64}", value):
        raise AdapterError("invalid_username")
    return value


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def unsupported_blocks():
    return {key: {"status": "unsupported", "reason": "not_exposed_by_this_official_adapter"}
            for key in ("music", "transcript", "subtitles", "catalog")}


class HTTP:
    def __init__(self, token, base, *, session=None, headers=None):
        if not isinstance(token, str) or not token or token != token.strip() or any(ord(c) < 32 for c in token):
            raise AdapterError("runtime_access_token_required")
        self._token, self.base = token, base
        self.session = session or requests.Session()
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json", **(headers or {})}

    def text(self, value, limit=20000):
        if not isinstance(value, str):
            return ""
        return "".join(c for c in value.replace(self._token, "[redacted]") if c >= " " or c == "\n")[:limit]

    def request(self, method, path, *, params=None, data=None, json=None, guard=None):
        if method not in {"GET", "POST"} or not path.startswith("/") or "?" in path or ".." in path:
            raise AdapterError("invalid_request")
        if method == "POST":
            if guard is None:
                raise AdapterError("publication_guard_required")
            guard()
        try:
            response = self.session.request(method, self.base + path, params=params,
                data=data, json=json, headers=self.headers, timeout=30, allow_redirects=False)
            if response.status_code in (401, 403):
                raise AdapterError("permission_denied")
            if response.status_code == 429:
                raise AdapterError("rate_limited")
            if not 200 <= response.status_code < 300:
                raise AdapterError("api_request_failed")
            payload = response.json()
            if not isinstance(payload, dict) or "error" in payload:
                raise AdapterError("invalid_api_response")
            return payload
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("transport_or_response_failure") from None


class ThreadsAdapter:
    platform = "threads"
    max_text = 500
    FIELDS = "id,username,text,timestamp,permalink,media_type,shortcode,has_replies,is_reply"
    REPLY_FIELDS = FIELDS + ",replied_to,root_post,is_reply_owned_by_me"

    def __init__(self, token, *, session=None):
        self.http = HTTP(token, "https://graph.threads.net/v1.0", session=session)

    def identity(self, expected):
        raw = self.http.request("GET", "/me", params={"fields": "id,username"})
        result = {"id": numeric(raw.get("id")), "username": username(raw.get("username"))}
        if result["username"] != username(expected):
            raise AdapterError("account_mismatch")
        return result

    def _post(self, raw):
        if not isinstance(raw, dict):
            raise AdapterError("invalid_post")
        post_id = numeric(raw.get("id"))
        owner = username(raw.get("username"))
        url = str(raw.get("permalink") or "")
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}
                or parts.username or parts.password or parts.port not in (None, 443)
                or not re.fullmatch(r"/@[A-Za-z0-9_.]+/post/[A-Za-z0-9_-]+/?", parts.path)
                or parts.path.split("/")[1].lstrip("@").lower() != owner):
            raise AdapterError("invalid_post_permalink")
        parent = raw.get("replied_to") or {}
        root = raw.get("root_post") or {}
        return {"id": post_id, "author": owner, "url": "https://www.threads.com" + parts.path.rstrip("/"),
                "text": self.http.text(raw.get("text")), "published_at": self.http.text(raw.get("timestamp"), 80),
                "media_type": self.http.text(raw.get("media_type"), 40),
                "has_replies": raw.get("has_replies") if isinstance(raw.get("has_replies"), bool) else None,
                "parent_id": numeric(parent["id"]) if isinstance(parent, dict) and parent.get("id") else "",
                "root_id": numeric(root["id"]) if isinstance(root, dict) and root.get("id") else ""}

    def _pages(self, path, params, limit):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise AdapterError("invalid_page_limit")
        output, ids, cursors = [], set(), set()
        params = dict(params)
        for _ in range(40):
            params["limit"] = min(100, limit - len(output))
            payload = self.http.request("GET", path, params=params)
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise AdapterError("invalid_page")
            for raw in rows:
                post = self._post(raw)
                if post["id"] not in ids:
                    ids.add(post["id"])
                    output.append(post)
            paging = payload.get("paging") or {}
            if not isinstance(paging, dict) or not isinstance(paging.get("cursors") or {}, dict):
                raise AdapterError("invalid_paging")
            after = (paging.get("cursors") or {}).get("after")
            if not rows or not after:
                return output[:limit], len(output) <= limit and not paging.get("next")
            if not isinstance(after, str) or len(after) > 4096 or after in cursors:
                return output[:limit], False
            if len(output) >= limit:
                return output[:limit], False
            cursors.add(after)
            params["after"] = after  # Never follow next URLs containing credentials.
        return output[:limit], False

    def discover(self, scope):
        source, target = scope["source"], scope["target"]
        params = {"fields": self.FIELDS}
        if source == "post":
            return [numeric(target)]
        if source == "own":
            path = "/me/threads"
        elif source == "creator":
            path, params["username"] = "/profile_posts", username(target)
        elif source == "topic":
            path = "/keyword_search"
            params.update(q=target, search_type="RECENT", search_mode="KEYWORD")
        else:
            raise AdapterError("unsupported_source")
        if scope.get("since"):
            params.update(since=scope["since"], until=scope["until"])
        posts, _ = self._pages(path, params, scope["candidate_limit"])
        if source == "creator" and any(p["author"] != username(target) for p in posts):
            raise AdapterError("creator_mismatch")
        return [p["id"] for p in posts if eligible_time(p, scope)]

    def conversation(self, post_id, limit):
        return self._pages("/" + numeric(post_id) + "/conversation", {"fields": self.REPLY_FIELDS, "reverse": "false"}, limit)

    def collect(self, post_id, scope, actor):
        post = self._post(self.http.request("GET", "/" + numeric(post_id), params={"fields": self.FIELDS}))
        if post["id"] != post_id:
            raise AdapterError("post_mismatch")
        expected = actor["username"] if scope["source"] == "own" else username(scope["target"]) if scope["source"] == "creator" else None
        if expected is not None and post["author"] != expected:
            raise AdapterError("creator_mismatch")
        if not eligible_time(post, scope):
            raise AdapterError("publication_time_outside_scope")
        comments, complete, reason = [], False, "available"
        try:
            comments, complete = self.conversation(post_id, scope["comments"])
        except AdapterError as exc:
            reason = str(exc)
        metrics, metrics_status = {}, "unsupported_other_account_insights"
        if post["author"] == actor["username"]:
            try:
                payload = self.http.request("GET", "/" + post_id + "/insights", params={"metric": "views,likes,replies,reposts,quotes"})
                for row in payload.get("data", []):
                    if isinstance(row, dict) and row.get("name") in {"views", "likes", "replies", "reposts", "quotes"}:
                        values = row.get("values") or []
                        value = values[0].get("value") if values and isinstance(values[0], dict) else None
                        if type(value) is int and value >= 0:
                            metrics[row["name"]] = value
                metrics_status = "available" if metrics else "not_provided"
            except AdapterError as exc:
                metrics_status = str(exc)
        return {**post, "platform": self.platform, "authority": "threads_official_api", "observed_at": now_iso(),
                "comments": comments, "comments_complete": complete, "comments_status": reason,
                "metrics": metrics, "metrics_status": metrics_status, **unsupported_blocks(),
                "acoustic_verification": {"status": "not_attempted", "verified": False}, "lyrics": {"status": "not_attempted"}}

    def preflight(self, evidence, scope, actor):
        if self.identity(scope["account"]) != actor:
            raise AdapterError("account_mismatch")
        current = self._post(self.http.request("GET", "/" + evidence["id"], params={"fields": self.FIELDS}))
        if any(current[k] != evidence[k] for k in ("id", "author", "text", "url", "media_type", "published_at")):
            raise AdapterError("target_changed_recollect_required")
        comments, complete = self.conversation(evidence["id"], 1000)
        if not complete:
            raise AdapterError("duplicate_check_incomplete")
        if any(c["author"] == actor["username"] for c in comments):
            raise AdapterError("account_already_replied")

    def publish(self, evidence, actor, text, guard):
        payload = self.http.request("POST", "/" + numeric(actor["id"]) + "/threads",
            data={"media_type": "TEXT", "text": text, "reply_to_id": evidence["id"]}, guard=guard)
        container = numeric(payload.get("id"))
        payload = self.http.request("POST", "/" + actor["id"] + "/threads_publish", data={"creation_id": container}, guard=guard)
        published_id = numeric(payload.get("id"))
        receipt = self._post(self.http.request("GET", "/" + published_id, params={"fields": self.REPLY_FIELDS}))
        if (receipt["id"] != published_id or receipt["author"] != actor["username"] or receipt["text"] != text
                or receipt["parent_id"] != evidence["id"]):
            raise AdapterError("publication_receipt_unverified")
        return {"id": published_id, "url": receipt["url"], "verified": True}


def eligible_time(post, scope):
    if not scope.get("since"):
        return True
    try:
        when = dt.datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
        return when.tzinfo is not None and scope["since"] <= when.timestamp() < scope["until"]
    except (ValueError, TypeError, KeyError):
        return False


class LinkedInAdapter:
    platform = "linkedin"
    max_text = 1250

    def __init__(self, token, *, version="202608", session=None, reader=None):
        from tiktok_scraper.linkedin_api import LinkedInAPIClient
        if not re.fullmatch(r"20[0-9]{2}(0[1-9]|1[0-2])", version):
            raise AdapterError("invalid_api_version")
        self.reader = reader or LinkedInAPIClient(token, api_version=version)
        self.http = HTTP(token, "https://api.linkedin.com/rest", session=session,
                         headers={"Linkedin-Version": version, "X-Restli-Protocol-Version": "2.0.0"})

    def identity(self, expected):
        from tiktok_scraper.linkedin_api import validate_organization_urn
        organization = validate_organization_urn(expected)
        for start in range(0, 1000, 100):
            payload = self.http.request("GET", "/organizationAcls", params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED", "start": start, "count": 100})
            rows = payload.get("elements")
            if not isinstance(rows, list):
                raise AdapterError("invalid_account_response")
            for row in rows:
                if row.get("organization") == organization and row.get("state") == "APPROVED" and row.get("role") == "ADMINISTRATOR":
                    member = row.get("roleAssignee")
                    if not isinstance(member, str) or not re.fullmatch(r"urn:li:person:[A-Za-z0-9_-]+", member):
                        raise AdapterError("invalid_account_response")
                    return {"id": organization, "username": organization, "operator_id": member}
            if len(rows) < 100:
                break
        raise AdapterError("organization_admin_not_verified")

    def discover(self, scope):
        from tiktok_scraper.linkedin_api import validate_post_urn
        if scope["source"] == "post":
            return [validate_post_urn(scope["target"])]
        return self.reader.list_author_post_urns(scope["account"], limit=scope["candidate_limit"])

    def collect(self, post_id, scope, actor):
        raw = self.reader.collect_organization_post(post_id, scope["account"], comments_limit=scope["comments"])
        if raw["post_urn"] != post_id or raw["organization_urn"] != actor["id"]:
            raise AdapterError("post_or_owner_mismatch")
        comments = [{"id": c["comment_urn"], "parent_id": c["parent_comment_urn"], "author": c["actor_urn"],
                     "text": self.http.text(c["message"]), "published_at": c["created_at"], "likes": c["likes"]} for c in raw["comments"]]
        reported = raw["metrics"].get("comments")
        complete = type(reported) is int and len(comments) >= reported and len(comments) < scope["comments"]
        return {"platform": self.platform, "id": post_id, "author": actor["id"], "url": raw["canonical_url"],
                "text": self.http.text(raw["commentary"]), "published_at": raw["published_at"], "media_type": "organization_post",
                "observed_at": raw["observed_at"], "authority": "linkedin_official_api", "comments": comments,
                "comments_complete": complete, "comments_status": "available", "metrics": raw["metrics"],
                "metrics_status": "available", **unsupported_blocks(),
                "acoustic_verification": {"status": "not_attempted", "verified": False}, "lyrics": {"status": "not_attempted"}}

    def preflight(self, evidence, scope, actor):
        if self.identity(scope["account"]) != actor:
            raise AdapterError("account_mismatch")
        current = self.collect(evidence["id"], {**scope, "comments": 1000}, actor)
        if any(current[k] != evidence[k] for k in ("id", "author", "text", "published_at", "url")):
            raise AdapterError("target_changed_recollect_required")
        if not current["comments_complete"]:
            raise AdapterError("duplicate_check_incomplete")
        if any(c["author"] == actor["id"] for c in current["comments"]):
            raise AdapterError("account_already_commented")

    def publish(self, evidence, actor, text, guard):
        from tiktok_scraper.linkedin_api import validate_post_urn
        post_id = validate_post_urn(evidence["id"])
        path = "/socialActions/" + quote(post_id, safe="") + "/comments"
        result = self.http.request("POST", path, json={"actor": actor["id"], "object": post_id, "message": {"text": text}}, guard=guard)
        comment_id = numeric(str(result.get("id") or ""))
        receipt = self.http.request("GET", path + "/" + comment_id)
        if receipt.get("actor") != actor["id"] or (receipt.get("message") or {}).get("text") != text or str(receipt.get("id")) != comment_id:
            raise AdapterError("publication_receipt_unverified")
        return {"id": comment_id, "url": evidence["url"], "verified": True}
