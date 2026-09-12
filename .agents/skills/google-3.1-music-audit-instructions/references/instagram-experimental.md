# Experimental Instagram Music-Audit Test

## Status and boundary

Use this reference only when the user explicitly requests an Instagram
music-audit test. It describes the currently demonstrated
`experimental_authenticated_web` metadata path. It is not an official Instagram
Graph API adapter, does not create a canonical cross-platform chat shortcut, and
does not alter the workspace's TikTok-only LISTEN/AUDIT/ENGAGE contract. This
reference documents a bounded capability test; it is not proof that a production
Instagram adapter is enabled.

The workflow may collect safe post identity, canonical URL, creator, caption,
public metrics, accessible comments, and Instagram-returned structured music
fields. It must not download media, extract audio, run speech-to-text, infer
lyrics, perform acoustic recognition, analyze content, draft a response, engage,
or publish.

Read `AGENTS.md` and `docs/contracts/CROSS_PLATFORM_MUSIC_AUDIT.md` before using this procedure.
Those files take precedence over this reference.

## Preconditions

1. Use the required interpreter:
   `C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe`.
2. Resolve the exact official Instagram handle from an authoritative brand
   source, then verify every returned record has that exact owner. A display name
   or keyword search is not an owner fence.
3. Use only the existing Edge user-data root and directory `Profile 7`. Attach to
   its existing debugging endpoint; never launch a temporary profile, force-kill
   Edge, close the shared context, or expose the endpoint UUID or cookie values.
4. Verify the real connection, observed profile directory, context index, and
   Instagram authentication. Until the public mock-stub regression in
   `social_browser.py` is removed and tested, do not use its public `start` or
   `status` success output as proof. Invoke the real validation path and print
   only a safe boolean summary.
5. Give every attempt a new session directory. Do not reuse or overwrite an
   earlier durable run or delete partial evidence to make a post appear new.

## Safe Profile 7 verification

Read `DevToolsActivePort` into a process-local variable and construct the endpoint
without echoing it. Call `social_browser._browser_status_real` with the expected
Edge user-data root and `Profile 7`, then require all of the following:

- `reachable=true`;
- verified observed directory exactly `Profile 7`;
- `context_index=0`; and
- Instagram authenticated.

Print only those safe fields. Never print cookie names or values, authorization
headers, the WebSocket path, or raw browser state. If verification fails, stop;
do not fall back to another profile or the in-app browser.

## Exact creator collection

This is a legacy experimental transport, not a canonical workflow command. Use it
only under the explicit experimental label described above.

```powershell
$Python = 'C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe'
$ProfileUrl = 'https://www.instagram.com/<exact-handle>/'
$Posts = 5
$Session = 'instagram_music_audit_<handle>_5_<timestamp>'
$DevTools = Get-Content -LiteralPath "$env:LOCALAPPDATA\Microsoft\Edge\User Data\DevToolsActivePort"
if ($DevTools.Count -lt 2) { throw 'DevToolsActivePort is incomplete' }

$env:INSTAGRAM_CDP_URL = "ws://127.0.0.1:$($DevTools[0])$($DevTools[1])"
$env:INSTAGRAM_CONCURRENCY = '1'
$env:INSTAGRAM_DIRECT_ONLY = '1'
$env:INSTAGRAM_BROWSER_TIMEOUT_SECONDS = '30'
$env:INSTAGRAM_SEARCH_TIMEOUT_SECONDS = '120'
$env:INSTAGRAM_POST_TIMEOUT_SECONDS = '180'

& $Python .\run_scraper.py `
  --platform instagram `
  --url $ProfileUrl `
  --videos $Posts `
  --discovery-videos $Posts `
  --comments 0 `
  --percentage 100 `
  --transport-mode api-only `
  --session-name $Session `
  --source-kind creator `
  --source-value '<exact-handle>' `
  --source-target-mode creator
```

Do not add `--auto-analyze`, `--match-music`, a persistent alternate profile, or
a comment cap. The expected raw artifact is
`comments_data/<session>/comments/instagram_comments.json`.

Before import, verify:

- requested and returned counts;
- unique content IDs and nonempty canonical URLs;
- every `content_creator` exactly matches the frozen handle;
- comments have provider IDs and attribution;
- no collector error or local truncation;
- a terminal comment frontier for each counted post; and
- an attempted, provenance-bearing platform music outcome.

If an output contains unrelated captions, menu strings, ads, missing-attribution
objects, or more alleged top-level comments than the endpoint returned, stop and
treat the collector as contaminated. The hardened parser must accept only the
Instagram comments REST endpoint or `PolarisPostCommentsPaginationQuery`, require
a provider comment ID plus attribution, and exclude generic text-bearing API
objects.

## Durable exact-count import

Create a separate platform-neutral state database inside the session directory:

```powershell
$Database = "comments_data\$Session\social_music.sqlite"
$RunId = "ig_<handle>_${Posts}_<timestamp>"
$Raw = "comments_data\$Session\comments\instagram_comments.json"
$Export = "comments_data\$Session\instagram_music_audit.jsonl"

& $Python .\social_music_audit.py --database $Database create `
  --platform instagram --source-mode creator --target '@<exact-handle>' `
  --posts $Posts --run-id $RunId --adapter-version offline-json-ingest-v1

& $Python .\social_music_audit.py --database $Database ingest `
  --run-id $RunId --file $Raw
```

The importer must remain fail-closed:

- a post counts only when owner, content ID, canonical URL, comments, music
  attempt/provenance, evidence hash, and adapter-bound manifest validate;
- a displayed-comment count above the accessible terminal result remains
  `comments_partial`; do not clamp, fabricate, or mark it complete;
- keep the run in `collecting` while it is below `N`; finalizing early makes the
  run terminal;
- checkpoint partial attempts immutably, exclude their IDs from later profile
  discovery, deepen the same exact creator profile, and append later exact-owner
  candidates until exactly `N` evidence-ready records exist or a real source
  frontier is exhausted; and
- the legacy collector does not perform this replacement loop automatically.

The commands in this reference do not define a general retry/deepening runner.
If the first collection returns fewer than `N` ready rows, do not improvise a
manual browser loop, overwrite checkpoints, reuse the session, or finalize it as
complete. Preserve the partial run and report `collection_incomplete: X/N` unless
a separately reviewed and tested replacement command is available for the exact
collector version. The Harlan example below records a validated one-off repair;
it is not general authority to invent a replacement procedure.

When exactly `N` records are ready:

```powershell
& $Python .\social_music_audit.py --database $Database finalize --run-id $RunId
& $Python .\social_music_audit.py --database $Database export `
  --run-id $RunId --file $Export
```

Verify the export has exactly `N` rows, every row has the exact creator and a
canonical post URL bound to its content ID, every comment frontier is complete,
and every evidence hash validates. Preserve partial checkpoints but exclude them
from the exact-count export.

## Evidence interpretation

- `available` structured music means Instagram returned enough declared identity
  metadata for the closed block. It is not acoustic verification.
- `partial` means some declared fields were returned but identity is incomplete.
- `not_provided` means the attempted platform response supplied no structured
  music declaration. It does not prove the post has no audible music.
- Instagram transcript and subtitle collection is currently `unsupported` in
  this adapter. The written post caption is not a transcript or subtitle file.
- Acoustic verification and lyrics remain `not_attempted`. No catalog correlation
  is implied unless a separately implemented provider block says so.
- Do not infer a creator-wide music-use rate from a small test sample.

## Validated example — 2026-08-20

The exact coffee-brand account `@harlanholden.coffee` was verified through the
brand's official ordering-app listing and live owner binding. A requested
five-post test required ten exact-owner attempts: five became evidence-ready and
five remained immutable partial checkpoints because accessible comments were
below Instagram's displayed counts.

The final five-row sample contained two structured licensed-music declarations:

- `Let Me Think About It` — Ida Corr, Fedde Le Grand; and
- `luther` — Kendrick Lamar, SZA.

The other three rows were `not_provided`, which is not evidence of silence. All
five counted rows had captions, exact-owner binding, canonical URLs, complete
comment frontiers, and unsupported transcript/subtitle outcomes. The final run
was `collection_complete: 5/5`; no audio, AI, engagement, or publication occurred.

Validated workspace artifact:
`comments_data/instagram_music_audit_harlanholden_5_20260820_01/instagram_music_audit_corrected.jsonl`.

## Completion report

Report the platform, exact handle, run ID, `X/N` status, counted post links,
comment coverage, music status/title/artist where returned, explicit unsupported
or not-provided outcomes, export path/hash, test results, and final Profile 7
status. State clearly that the adapter is experimental authenticated web rather
than official Instagram Graph API access.
