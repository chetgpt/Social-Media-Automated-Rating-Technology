# LinkedIn Page Collection Workflow

`linkedin_workflow.py` is a separate official-API collector for evidence from
authorized LinkedIn organization Pages. It does not extend the canonical
TikTok shortcuts, TikTok master registry, Profile 7 browser workflow, or the
platform-neutral social-music importer.

## Closed capability boundary

LinkedIn supports two durable state labels, both with the same current stopping
boundary:

| Stored workflow | Current behavior |
| --- | --- |
| `listen` | collect and retain authorized organization evidence, then stop |
| `engage` | collect and retain authorized organization evidence, then stop |

Both report `publication_enabled=false` and `external_ai_enabled=false`.
LinkedIn `engage` does not analyze, draft, review, approve, authorize, or
publish. The CLI has no export or publication command.

Exactly one source is allowed per run:

- `organization`: one administered Page identified by an exact
  `urn:li:organization:<numeric-id>`, with a positive finite `--posts N`;
- `post`: one exact authorized organization-authored
  `urn:li:share:<numeric-id>` or `urn:li:ugcPost:<numeric-id>`, bound to its
  organization URN and `--posts 1`.

The official API must verify that each returned post is authored by the frozen
organization. Owner mismatch, person/member authorship, or an invalid resource
URN fails closed before evidence is checkpointed.

Unsupported surfaces are deliberately absent: topic or hashtag discovery,
arbitrary-member/profile/feed collection, person-authored posts, `ALL`, URL
sources, browser scraping, cross-platform export, external or built-in AI,
drafting, review, approval, authorization, and publication. Unqualified
`LISTEN:` and `ENGAGE:` chat requests remain TikTok-only; there is no LinkedIn
chat shortcut in this release.

## Authorization and isolation

Use only LinkedIn's official API and obtain access through a token already
authorized for the requested organization and API products. The runner reads
that token only from the process environment variable
`LINKEDIN_ACCESS_TOKEN`. Never pass it as a CLI argument or put it in a source
file, `.env` committed to Git, database, output, log, or error report.

Setup requires an approved LinkedIn Community Management app/product tier and
3-legged OAuth for an authorized organization administrator. Organization-post
reads require `r_organization_social`; organization comment/reply reads require
`r_organization_social_feed`. A token by itself does not grant these products,
scopes, Page roles, or resource access. The default LinkedIn API version is
`202608`; `--api-version YYYYMM` is configurable so the operator can select an
available monthly version within the app's approved contract.

The collector must not start or attach to Microsoft Edge Profile 7, another
browser profile, Playwright, or the in-app browser. It stores state only in its
isolated SQLite database, which defaults to:

```text
comments_data/linkedin/linkedin_collection.sqlite3
```

That database must never be merged with TikTok project/master state or
`social_music_audit.py` state. Runtime databases and collected payloads stay
outside Git.

## Exact new-only collection and resume

Run creation hash-binds the project, workflow, source, organization URN, exact
post URN when applicable, requested count, LinkedIn API version, comment cap,
retention settings, and disabled publication/AI flags.

For an organization source, the adapter inventories that authorized Page,
deduplicates resource URNs, excludes posts already known in the isolated
LinkedIn database, and freezes the remaining ordered inventory and its hash
before hydration. For an exact-post source, the frozen inventory is that one
URN. A known exact post does not count and cannot be replaced.

Only a post at its frozen ordinal, with the exact organization binding and a
terminal official-API collection outcome, may become evidence-ready. The run
stops as `collection_complete` only at exactly `N/N`. Exhaustion or terminal
failures produce `collection_incomplete: X/N`; an underfilled set never becomes
complete and never unlocks another stage.

`resume --run-id` may reopen an incomplete run to retry failed work, but it
reuses the saved source, organization, count, API version, comment cap,
inventory order, and inventory hash. It cannot rediscover, add, remove, reorder,
or substitute posts. A complete run remains terminal; invoking CLI `resume` on
it returns its status without reopening it.

## Retention

Retention is enforced by state access and can also be run explicitly:

- accessible member comment and reply payloads expire 48 hours after their
  observation; message text, actor data, metrics, and hashes are deleted, while
  only the run/post/comment resource URNs and purge time remain as a deletion
  tombstone;
- organization-post payloads expire 180 days after observation; commentary,
  content, aggregate metrics, collection details, canonical URL/timestamps,
  and evidence hashes are scrubbed, leaving the resource-URN identity needed
  for tombstones and the durable `new_only` fence.

Purging does not make a previously known post new again and does not authorize
export or publication.

Backups and restores remain subject to the same TTLs. Any backup process must
preserve expiry metadata and purge expired payloads from backup generations and
restored copies; a backup is not permission to retain comment or post evidence
beyond 48 hours or 180 days.

## CLI

On this workstation, use the required Python 3.11 interpreter. Do not use bare
`python` or `py -3`.

```powershell
$EngagePython = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
$LinkedInDb = "comments_data\linkedin\linkedin_collection.sqlite3"
```

Inspect the closed capability contract without a token or network request:

```powershell
& $EngagePython .\linkedin_workflow.py capabilities
```

Place the token in the process environment only, then collect a finite new-only
set from one administered Page:

```powershell
$env:LINKEDIN_ACCESS_TOKEN = "<runtime OAuth access token>"

& $EngagePython .\linkedin_workflow.py --database $LinkedInDb collect `
  --workflow listen `
  --source organization `
  --organization-urn "urn:li:organization:123456" `
  --posts 25 `
  --comments-per-post 100 `
  --run-id "linkedin-listen-page-001"
```

Collect one exact authorized organization post under the collection-only
`engage` label:

```powershell
& $EngagePython .\linkedin_workflow.py --database $LinkedInDb collect `
  --workflow engage `
  --source post `
  --organization-urn "urn:li:organization:123456" `
  --post-urn "urn:li:share:7300000000000000001" `
  --posts 1 `
  --run-id "linkedin-engage-post-001"
```

Resume only the immutable saved selection:

```powershell
& $EngagePython .\linkedin_workflow.py --database $LinkedInDb resume `
  --run-id "linkedin-listen-page-001"
```

Read status or explicitly purge expired payloads without a token, browser, or
network access:

```powershell
& $EngagePython .\linkedin_workflow.py --database $LinkedInDb status `
  --run-id "linkedin-listen-page-001"
& $EngagePython .\linkedin_workflow.py --database $LinkedInDb purge-expired
```

The global `--database` option must precede the subcommand. `collect` and
`resume` return a nonzero incomplete outcome when the frozen authorized set
cannot reach the requested exact count; inspect the emitted JSON status and use
the same run ID for any permitted retry.

## Official references

- [Community Management app review](https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review?view=li-lms-2026-03)
- [LinkedIn Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-05)
- [LinkedIn Comments API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/comments-api?view=li-lms-2026-05)
- [Social metadata API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/social-metadata-api?view=li-lms-2026-01)
- [Authorization code flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [Marketing API versioning](https://learn.microsoft.com/en-us/linkedin/marketing/versioning?view=li-lms-2026-07)
- [LinkedIn data storage requirements](https://learn.microsoft.com/en-us/linkedin/marketing/data-storage-requirements?view=li-lms-2026-07)
