# TikTok One Top Content Link Discovery

## Purpose

`tiktok_one_top_content.py` is a narrow, read-only discovery bridge for the
TikTok One for Partners **Discover top content** page:

```text
https://ads.tiktok.com/creative/forpartners/creator/top-content?region=row
```

Its discovery stage collects canonical TikTok video links. It does not collect
post evidence. A separate bridge stage may pass those frozen links, one at a
time, to the existing canonical direct-URL MUSIC AUDIT command. MUSIC AUDIT is
the complete LISTEN profile implemented by `engage_tiktok.py`; this feature
does not add another canonical LISTEN source mode.

No chat shortcut is introduced in v1. Use the explicit CLI commands described
below.

## Closed source scope

The only accepted discovery page is HTTPS host `ads.tiktok.com` with exact
path `/creative/forpartners/creator/top-content`. The optional `region` query
is normalized as source scope. Credentials, fragments, unrelated query
parameters, and other TikTok One pages are rejected.

Discovery uses only Microsoft Edge's existing user-data root and profile
directory `Profile 7`, attached in `existing_profile_attach` mode. The visible
Edge profile label may be `Profile 1`; the directory identity is authoritative.
The runner starts or reuses Profile 7, verifies that the debugging connection,
profile, TikTok login, and active account are valid, keeps the shared browser
running, opens only a temporary page for discovery, and closes only that
temporary page when finished. It never substitutes another Edge profile, a
temporary Playwright profile, or the in-app browser.

The v1 collector captures the filters already selected on the page but does
not change them. Bounded filter provenance may include the displayed country,
period, content-tag selection, organic-only selection, and last-updated label.
The source URL, account binding, filter snapshot, ranking order, and resulting
ordered-link hash are frozen in the discovery run's durable state.

## Ranking and exact-count behavior

The collector reads rankings in this order:

1. Highest video views (`video_views`)
2. Highest engagement (`engagement`)
3. Highest 6-second views (`six_second_views`)

The currently observed, verified frontier is the complete Top 100 for each
ranking. The collector scrolls until that 100-card frontier is verified; a
blocked page, unstable pagination, or an unexplained stall below 100 is not
treated as source exhaustion.

Requests are not capped at 20, 50, or 100. For a request of `N` links, the
collector preserves ranking order and deduplicates across rankings by numeric
TikTok video ID:

- Requests up to 100 normally use the video-views ranking only.
- Requests above 100 continue through engagement and then 6-second views.
- A video repeated in another ranking is retained only once, with bounded
  ranking provenance for its discovery position.
- The run completes only after exactly `N` unique canonical video links are
  durably checkpointed.
- If all verified ranking frontiers contain fewer than `N` unique links, or
  access prevents a verified frontier, the run records
  `links_incomplete: X/N` with bounded reasons. It never silently lowers the
  request or invents replacement links.

The three observed Top 100 rankings provide at most 300 positions before
cross-ranking deduplication. They do not guarantee 300 unique videos.

## Data-minimization boundary

Discovery persists only what is required to identify and replay the frozen
link inventory:

- query-free canonical URLs in the form
  `https://www.tiktok.com/@<handle>/video/<numeric-id>`;
- numeric TikTok video IDs and the creator handle already present in the
  canonical URL;
- ordinal, ranking name, ranking position, bounded page/filter provenance,
  timestamps, terminal reasons, and deterministic hashes needed for resume and
  tamper detection; and
- child LISTEN job identifiers and terminal status when the bridge is used.

Discovery never persists or exports captions, comment or reply text, content
metrics beyond the selected rank identity, media, thumbnails, avatars, HTML,
DOM dumps, raw network responses, cookies, authorization headers, session
tokens, signed media URLs, or other authentication material. It does not use a
private TikTok One endpoint as a durable interface. Media requests in the
temporary discovery page are blocked where practical, and detail views are
opened only long enough to read the visible canonical **View on TikTok** link.

The runner must never click or invoke **Invite**, bookmark/favorite, contact,
message, publication, or any other outbound or account-mutating control. Link
discovery grants no permission to contact a creator or publish anything.

## Isolated durable state

Top Content discovery state is stored separately from canonical workflow state,
by default at:

```text
comments_data/tiktok_one/top_content_links.sqlite3
```

This database owns discovery runs, immutable scope, link checkpoints, failure
records, and child-job bookkeeping. Discovery does not insert harvested
records into the TikTok master registry or any per-project ENGAGE database.

The LISTEN bridge may read the workspace-global master registry to enforce its
existing `new_only` fence. A video ID already known to the master registry is
marked `skipped_known` in the isolated bridge state and counted separately; it
is not hydrated again and is not represented as a newly completed child. The
canonical direct-URL child writes its own evidence and master-registry
observation through the existing workflow only after that child succeeds.

## Bridge to canonical LISTEN

The `listen` command consumes a completed, frozen link-discovery run. For every
globally new URL, it invokes the existing direct-URL command sequentially:

```text
engage_tiktok.py music-audit --project <project> --url <canonical-url> --posts 1
```

Global database arguments are supplied to `engage_tiktok.py` before its
subcommand. Each child therefore remains an ordinary one-post canonical
MUSIC AUDIT/LISTEN run with `workflow=listen`, `collection_policy=new_only`, an
exact immutable URL, complete evidence collection, terminal TikTok music
declaration and configured-catalog outcomes, and the normal workspace-global
master-registry checks. It stops at collection storage. This bridge cannot run
AI analysis, draft or review a response, request approval, authorize a comment,
or publish anything.

Children run one at a time. Their per-project workflow databases remain
separate from the Top Content link database. The bridge records each child run
ID and terminal result without storing raw child output. An interrupted or
incomplete child retains its original run ID and is retried through the
existing `resume-collect --run-id <child-run-id>` path. A retry may repair only
that child's immutable one-URL inventory; it cannot rediscover, reorder, or
substitute links. Completed and `skipped_known` jobs are not rerun.

The discovery count and the new LISTEN completion count are deliberately
reported separately. For example, a frozen 50-link run containing five IDs
that became globally known before bridge execution remains a complete
50-link discovery run, while LISTEN reports 45 eligible children plus five
`skipped_known` jobs.

## CLI examples

Use the required workstation interpreter:

```powershell
$Python311 = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
```

Collect exactly 20, 50, 100, or more canonical links from the currently
selected page filters:

```powershell
& $Python311 tiktok_one_top_content.py collect-links --links 20
& $Python311 tiktok_one_top_content.py collect-links --links 50
& $Python311 tiktok_one_top_content.py collect-links --links 100
& $Python311 tiktok_one_top_content.py collect-links --links 175
```

Each command prints its discovery run ID. Inspect a durable run without
accessing TikTok:

```powershell
& $Python311 tiktok_one_top_content.py status --run-id '<link-run-id>'
```

Export the query-free link inventory from a completed run:

```powershell
& $Python311 tiktok_one_top_content.py export-links --run-id '<link-run-id>' --output 'top-content-links.json'
```

Feed globally new links from that frozen run into canonical LISTEN, then check
the parent/child summary:

```powershell
& $Python311 tiktok_one_top_content.py listen --run-id '<link-run-id>' --project 'top-content-listen'
& $Python311 tiktok_one_top_content.py listen-status --run-id '<link-run-id>'
```

`status`, `export-links`, and `listen-status` are local read-only operations.
`collect-links` accesses only the approved TikTok One page through Profile 7.
`listen` accesses the canonical TikTok post URLs through the existing LISTEN
collector and remains subject to all normal exact-count, evidence, music,
master-registry, and browser rules.

## Capability summary

| Capability | TikTok One discovery | LISTEN child |
| --- | --- | --- |
| Collect canonical video URL and ID | Yes | Uses frozen exact URL |
| Capture bounded filter/rank provenance | Yes | No |
| Collect caption, comments, metrics, transcript, music evidence | No | Yes, under the canonical evidence contract |
| Persist raw TikTok One page or network data | No | No |
| Use AI analysis or external AI | No | No |
| Draft, approve, contact, invite, or publish | No | No |
| Read the TikTok master known-ID fence | Only for bridge eligibility | Yes |
| Write canonical evidence/master observations | No | Yes, through existing LISTEN only |
